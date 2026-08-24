"""The wake tier — the only part of the pulse that costs anything.

A tick decides *whether* this hour is worth a model. This module is what happens
when the answer is yes. M1 gives it exactly one job: **groom** (ARCHITECTURE.md
§4.2). Read a story, decide whether there is enough there to build, and either
draft acceptance criteria or say precisely what's missing.

Grooming is the right first job for three reasons. It is read-only, so nothing it
does can damage a project. It ends at a human gate — Ordis never marks its own
criteria `ready` — so the loop cannot run away with the backlog. And it exercises
the whole spawn → harvest → record path that staffing and dispatch will reuse, on
work whose worst failure is a bad paragraph.

Since M3 it has a second job: **build**. A story the PO accepted and dispatched
runs here too, in a git worktree, and comes back as a patch nobody has applied.
That path lives in `build.py`; this module decides whether the hour can afford
it and in what order the two jobs run. Grooming goes first — it is cheaper, and
a story groomed this hour can be dispatched before the next one.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3

from . import (agent, attachments as attach, build as build_mod, control, db,
               forge as forge_mod, pulse as pulse_mod, roster as roster_mod)

# How many stories one wake may groom. A wake is coalescing — an hour with six
# new stories is one wake — so this is the throttle that keeps a bulk Notion
# import from becoming a bulk token spend.
GROOM_LIMIT = int(os.environ.get("COLONY_GROOM_LIMIT", "2"))

GROOM_TIMEOUT_S = int(os.environ.get("COLONY_GROOM_TIMEOUT", "420"))

# How many skill drafts one wake will write. One is almost always right: a draft
# is a PO request, and a PO who queued four of them still wants to read the first
# before paying for the rest.
FORGE_DRAFT_LIMIT = int(os.environ.get("COLONY_FORGE_LIMIT", "1"))


def contract(conn: sqlite3.Connection, role: str) -> dict | None:
    """Load an agent's terms of employment. No contract, no spawn."""
    row = conn.execute(
        "SELECT * FROM agents WHERE role = ? AND project IS NULL AND status = 'active'",
        (role,),
    ).fetchone()
    if not row:
        return None
    out = dict(row)
    for key in ("tools_allowed", "tools_denied", "read_scope", "skills", "definition_of_done"):
        if out.get(key):
            out[key] = json.loads(out[key])
    return out


def budget_state(conn: sqlite3.Connection, usage: dict | None) -> dict:
    """Is there room in the week's allowance to spend anything at all?

    The sprint budget is a share of the 7-day window (§6.1), so the question is
    always "how much of the week is gone", never a dollar figure. With no usage
    sample we decline to dispatch: spending blind is the one thing the budget
    model exists to prevent.
    """
    # base + whatever the PO boosted it to for a heavy sprint (§6.1). The boost
    # is stored separately from the sprint so the baseline the colony was
    # designed around stays visible next to the exception.
    band = control.effective_allowance(conn)
    allowance = band["effective"]

    if not usage or usage.get("seven_day") is None:
        return {"ok": False, "allowance": allowance, "used": None,
                "why": "no usage sample — refusing to dispatch blind"}
    used = float(usage["seven_day"])
    if used >= allowance:
        # base and delta, not a hardcoded 35 and a "+" that assumed the dial
        # only went up — the PO can now walk the allowance down as well.
        boosted = f" ({band['base']:g}% baseline {band['boost']:+.0f})" if band["boost"] else ""
        return {"ok": False, "allowance": allowance, "used": used,
                "why": f"week at {used:.0f}% is at or past the {allowance:.0f}% "
                       f"colony allowance{boosted}"}
    return {"ok": True, "allowance": allowance, "used": used, "why": None,
            "boost": band["boost"]}


# Which stories are waiting to be groomed. Kept as one string because the tick
# needs the same answer to decide whether this hour is worth waking for, and two
# copies of this predicate would drift into a loop that wakes and then finds
# nothing to do.
#
# `po-review` and `ready` are already past this stage, and `needs-info` waits on
# the PO, not on us — re-grooming it hourly would spend tokens to reproduce an
# answer we already have. MAX_ATTEMPTS is the stop on the other failure mode: a
# story the agent keeps failing to parse would otherwise be groomed again every
# hour, forever, at full price.
MAX_ATTEMPTS = 2

# `t.status <> 'wontfix'` is how the attempt budget resets. When a story's brief
# changes under an open question, M5 marks that story's old groom tickets
# wontfix — they answered a question about a version of the story that no longer
# exists — and the count drops back to zero. Without it, a story groomed twice
# early on could never be re-read no matter how much Jordan rewrote it, which is
# the same stale-prose failure the rest of M5 exists to fix, one layer down.
GROOMABLE_WHERE = """
    status IN ('backlog','needs-criteria')
    AND dropped_at IS NULL
    -- Filed by the PO: Done, Shipped, Shelved, New or Not started. A row he has
    -- not started is not a row he is waiting on, and grooming it produces a
    -- question about a decision he has deliberately not made yet.
    AND settled_as IS NULL
    AND (acceptance_criteria IS NULL OR acceptance_criteria = '')
    AND (SELECT COUNT(*) FROM tickets t
          WHERE t.story_id = stories.id AND t.title LIKE 'Groom:%'
            AND t.status <> 'wontfix') < :max_attempts
"""


def groomable_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        f"SELECT COUNT(*) n FROM stories WHERE {GROOMABLE_WHERE}",
        {"max_attempts": MAX_ATTEMPTS},
    ).fetchone()["n"]


def stories_to_groom(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    """Ungroomed stories, highest priority first."""
    return conn.execute(
        f"SELECT * FROM stories WHERE {GROOMABLE_WHERE} ORDER BY priority ASC, id ASC LIMIT :limit",
        {"max_attempts": MAX_ATTEMPTS, "limit": limit},
    ).fetchall()


def _checklist(story: sqlite3.Row, column: str) -> list[str]:
    """One half of the story's checklist, or nothing if the column predates M5."""
    try:
        raw = story[column]
    except (IndexError, KeyError):
        return []
    try:
        items = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [str(i) for i in items if str(i).strip()]


def _progress_section(story: sqlite3.Row) -> str:
    """What is already inside the story, stated before what is left of it.

    The single most expensive mistake this loop made in its first week was
    re-raising work Jordan had already finished, because the brief and the
    checkboxes were flattened into the same wall of text. Naming the finished
    items separately, and telling the agent in one blunt sentence that they are
    closed, costs about forty tokens and buys back a whole class of stale Inbox
    cards.
    """
    done, todo = _checklist(story, "done_items"), _checklist(story, "open_items")
    if not done and not todo:
        return ""
    out = ["", "--- progress, as of the last Notion sync ---"]
    if done:
        out.append(f"ALREADY DONE ({len(done)}) — treat these as closed. Do not"
                   " re-raise them, do not ask about them, do not put them in criteria:")
        out += [f"  [x] {i}" for i in done[:40]]
    if todo:
        out.append(f"STILL OPEN ({len(todo)}) — this is the actual scope:")
        out += [f"  [ ] {i}" for i in todo[:40]]
    out.append("--- end progress ---")
    return "\n".join(out)


def _settled_section(story: sqlite3.Row) -> str:
    """Decisions Jordan has already made in a thread, stated as standing fact.

    These came out of the Inbox rather than out of Notion, and before 013 they
    lived only in `po_messages` — a transcript nothing grooms from. An agent
    re-reading this story would ask the question again, which is how a board of
    eight stories ended up parked on answers that had all been given.
    """
    try:
        raw = (story["po_answers"] or "").strip()
    except (IndexError, KeyError):
        return ""
    if not raw:
        return ""
    return ("\n--- what Jordan has already settled, in the Inbox ---\n"
            "These are decisions, not suggestions. Do not ask about them again.\n"
            + raw[:4000] + "\n--- end settled ---")


def groom_prompt(story: sqlite3.Row, projects: list[str],
                 attached: list[dict] | None = None) -> str:
    """The work order. Explicit about the gate, so the agent can't overstep it."""
    body = (story["description"] or "").strip() or "(the Notion page body is empty)"
    return f"""You are Ordis, Scrum Master of a colony of Claude agents. You are grooming one
backlog story for Jordan, who is the Product Owner. You are READ-ONLY: you have
Read, Grep and Glob and nothing else. Do not attempt to modify anything.

STORY #{story['id']}: {story['title']}
Notion status: {story['notion_status'] or 'unknown'}
Current guess at project folder: {story['project'] or 'none — unknown'}

--- brief from the Notion page body ---
{body[:6000]}
--- end brief ---
{_progress_section(story)}
{_settled_section(story)}
{attach.evidence(attached or [])}

Project folders that exist under D:\\ALL STUFF\\PROJECTS (a story belongs to one
of these, or to none if it is new work):
{chr(10).join('  ' + p for p in projects)}

You may read files under D:\\ALL STUFF\\PROJECTS to understand context. Each
project has a PROJECT.md at its root that states its current status — read the
relevant one before deciding anything. Be frugal: a few targeted reads, not a
survey.

Your job is to answer one question: **is there enough here to build?**

- If NO, say precisely what decision is missing. Not "needs more detail" — name
  the specific thing only Jordan can decide (a target platform, a scope
  boundary, which of two approaches). One missing decision is enough.
- If YES, draft acceptance criteria: 3-6 concrete, checkable statements. Each
  one must be something you could later verify as done or not done. No vague
  quality words.

You do NOT decide that this story is ready to work on. Jordan does. You are
drafting for his approval.

Reply with ONLY a JSON object, no prose around it:

{{
  "enough_info": true or false,
  "missing": "the specific decision Jordan must make, or null if enough_info",
  "project": "one folder from the list above, or null if you cannot tell",
  "project_confidence": "high" or "low",
  "criteria": ["...", "..."],
  "summary": "one sentence for the dashboard, under 140 characters",
  "learned": "one thing you learned reading the repo that is worth keeping, or null"
}}"""


def _gist(text: str, limit: int = 220) -> str:
    """The opening of something long, cut at a word rather than mid-syllable."""
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    return (cut[:space] if space > limit * 0.6 else cut).rstrip(" ,;:-") + "\u2026"


def _said(conn, story_id, kind, text, ticket_id=None, tokens=0):
    """Record something a colonist wrote, without losing the end of it.

    `_event` truncates `summary` at 400 characters, which is right — the summary
    is a line in a timeline. What was wrong was calling it with the whole thought
    and no `detail`, because then the 401st character did not exist anywhere: the
    learnings in the drawer ended mid-word ("...surfacing the direct ATS apply
    URL over th") and there was nothing to expand to, because nothing had been
    kept. The gist goes in the summary and the whole thing goes in the detail,
    and the page decides how much of it to show.
    """
    text = str(text).strip()
    gist = _gist(text)
    _event(conn, story_id, kind, gist, text if len(text) > len(gist) else None,
           ticket_id, tokens)


def _event(conn, story_id, kind, summary, detail=None, ticket_id=None, tokens=0):
    conn.execute(
        """INSERT INTO story_events (story_id, ticket_id, kind, summary, detail, tokens)
           VALUES (?,?,?,?,?,?)""",
        (story_id, ticket_id, kind, _gist(str(summary), 400), detail, tokens),
    )


def groom_story(conn: sqlite3.Connection, story: sqlite3.Row, terms: dict,
                projects: list[str]) -> dict:
    """One story, one spawned agent, one outcome recorded."""
    # Whatever the forge has promoted for this role rides in front of the work
    # order. This is the only place a skill has any effect at all — an active
    # skill that no run loads is a file, not a capability.
    skills = forge_mod.active_for(conn, terms["role"])
    prompt = forge_mod.preamble(skills) + groom_prompt(
        story, projects, attach.for_story(conn, story["id"]))
    cur = conn.execute(
        """INSERT INTO tickets (story_id, title, intent, role, status, work_order, requires_po)
           VALUES (?,?,'research',?,'staffed',?,0)""",
        (story["id"], f"Groom: {story['title']}"[:200], terms["role"], prompt),
    )
    ticket_id = cur.lastrowid

    result = agent.run_ticket(
        conn,
        ticket_id=ticket_id,
        role=terms["role"],
        prompt=prompt,
        model=terms["model"],
        tools_allowed=terms["tools_allowed"],
        tools_denied=terms.get("tools_denied"),
        cwd=db.PROJECTS_ROOT,
        timeout_s=GROOM_TIMEOUT_S,
        max_tokens=terms.get("max_tokens_run"),
    )

    outcome = {"story_id": story["id"], "title": story["title"],
               "tokens": result.chargeable_tokens, "raw_tokens": result.total_tokens,
               "status": result.status, "over_budget": result.over_budget, "verdict": None}

    # Recorded whatever the outcome, and *before* the early returns below: a
    # skill that was loaded into a run which then failed has to count as a loss,
    # or the win rate only ever measures the runs the skill was already winning.
    forge_mod.record_uses(
        conn, skills=skills, run_id=result.raw.get("run_id"),
        tokens=result.chargeable_tokens,
        ok=result.status in ("ok", "killed-over-budget"),
    )

    if result.over_budget:
        # Report the breach, keep the work. We have already paid for it; throwing
        # the answer away would mean paying twice for the same question.
        conn.execute(
            """INSERT INTO escalations (story_id, ticket_id, kind, reason, recommendation, est_tokens)
               VALUES (?,?,'cost',?,?,?)""",
            (story["id"], ticket_id,
             f"Grooming \"{story['title']}\" spent {result.chargeable_tokens:,} chargeable tokens "
             f"against a {terms.get('max_tokens_run'):,} ceiling.",
             "Raise the investigator's ceiling or narrow the work order — the run itself "
             "completed and its answer was kept.",
             result.chargeable_tokens),
        )

    # A completed run is harvested even when it breached the ceiling. Only a run
    # that produced nothing is a failure.
    if result.status not in ("ok", "killed-over-budget"):
        conn.execute(
            "UPDATE tickets SET status = 'blocked', findings = ? WHERE id = ?",
            (result.error or result.status, ticket_id),
        )
        _event(conn, story["id"], "note", f"grooming did not complete: {result.status}",
               result.error, ticket_id, result.chargeable_tokens)
        outcome["verdict"] = f"run {result.status}"
        return outcome

    answer = result.json_payload()
    if not answer:
        conn.execute(
            "UPDATE tickets SET status = 'blocked', findings = ? WHERE id = ?",
            (result.text[:2000], ticket_id),
        )
        _event(conn, story["id"], "note", "grooming returned no usable JSON",
               result.text[:2000], ticket_id, result.chargeable_tokens)
        outcome["verdict"] = "unparseable answer"
        return outcome

    # A suggested project is still only a suggestion. `project_source` stays
    # 'inferred', so this can never be what authorizes a write later.
    suggested = answer.get("project")
    if suggested and suggested in projects and story["project_source"] != "confirmed":
        conn.execute("UPDATE stories SET project = ? WHERE id = ?", (suggested, story["id"]))

    conn.execute(
        "UPDATE tickets SET status = 'done', findings = ?, closed_at = datetime('now','localtime') "
        "WHERE id = ?",
        (json.dumps(answer, indent=2)[:8000], ticket_id),
    )

    if answer.get("enough_info") and answer.get("criteria"):
        criteria = "\n".join(f"- {c}" for c in answer["criteria"])
        conn.execute(
            """UPDATE stories SET acceptance_criteria = ?, status = 'po-review',
                      blocked_reason = NULL, updated_at = datetime('now','localtime')
                WHERE id = ?""",
            (criteria, story["id"]),
        )
        # Gate 1. Ordis drafts; only the PO marks a story ready. `raised_hash`
        # stamps the version of the story this was drafted against, so the next
        # sync can tell whether it is still an answer to a live question.
        conn.execute(
            """INSERT INTO escalations (story_id, ticket_id, kind, reason, recommendation,
                                        est_tokens, raised_hash)
               VALUES (?,?,'decision',?,?,?,?)""",
            (story["id"], ticket_id,
             f'"{story["title"]}" has draft acceptance criteria awaiting your approval.',
             control.card_text(criteria), result.chargeable_tokens, story["notion_hash"]),
        )
        # The story just stopped being blocked — it has criteria and it is
        # waiting on the PO, which is a different thing and a different colour.
        control.clear_needs_info(conn, story["id"])
        _event(conn, story["id"], "groomed",
               answer.get("summary") or "acceptance criteria drafted",
               criteria, ticket_id, result.chargeable_tokens)
        outcome["verdict"] = "criteria drafted → po-review"
    else:
        missing = answer.get("missing") or "the brief does not say enough to start"
        conn.execute(
            """UPDATE stories SET status = 'needs-info', blocked_reason = ?,
                      updated_at = datetime('now','localtime')
                WHERE id = ?""",
            (control.card_text(missing), story["id"]),
        )
        # `question_settled` covers two cases. A stale card is an open row that
        # is explicitly no longer trusted, and letting it suppress a fresh
        # question would mean the Inbox keeps showing the outdated wording
        # forever — so stale rows do not count. A dismissed card does count,
        # for as long as the brief it was dismissed against stays put.
        if not control.question_settled(conn, story["id"], "needs-info",
                                        story["notion_hash"]):
            conn.execute(
                """INSERT INTO escalations (story_id, ticket_id, kind, reason, recommendation,
                                            est_tokens, raised_hash)
                   VALUES (?,?,'needs-info',?,?,?,?)""",
                (story["id"], ticket_id, f'"{story["title"]}" cannot start yet.',
                 control.card_text(missing), result.chargeable_tokens, story["notion_hash"]),
            )
            # The old wording is superseded, not merely accompanied.
            conn.execute(
                """UPDATE escalations SET resolved_at = datetime('now','localtime'),
                          po_decision = 'amend'
                    WHERE story_id = ? AND kind = 'needs-info' AND resolved_at IS NULL
                      AND stale_at IS NOT NULL""",
                (story["id"],),
            )
        _said(conn, story["id"], "blocked", missing, ticket_id, result.chargeable_tokens)
        outcome["verdict"] = "needs info"

    if answer.get("learned"):
        _said(conn, story["id"], "learning", answer["learned"], ticket_id, 0)

    return outcome


# ── answering the PO ──────────────────────────────────────────────────────────
#
# The Inbox got a reply box, so the wake got a job that runs before every other
# job: read what Jordan typed and answer it. It goes first for the same reason a
# standup starts with blockers — an hour spent grooming a story the PO has just
# redefined is an hour spent on the wrong story.
#
# This is capped hard. A reply is a short question about one item, so the answer
# is a short read and a short paragraph; if a message needs more than this it
# needs to be a story, and saying so is a legitimate answer.

REPLY_LIMIT = int(os.environ.get("COLONY_REPLY_LIMIT", "3"))
REPLY_TIMEOUT_S = int(os.environ.get("COLONY_REPLY_TIMEOUT", "300"))


def reply_prompt(msg: sqlite3.Row, esc: sqlite3.Row | None, story: sqlite3.Row | None,
                 projects: list[str], history: list[dict],
                 attached: list[dict] | None = None) -> str:
    """The work order for one PO reply."""
    lines = [
        "You are Ordis, Scrum Master of a colony of Claude agents. Jordan is the",
        "Product Owner. He has written to you about one item in his PO Inbox, and",
        "you are answering him directly. You are READ-ONLY: Read, Grep and Glob.",
        "",
    ]
    if esc is not None:
        lines += [
            "--- the Inbox item he is replying to ---",
            f"kind: {esc['kind']}",
            f"raised: {esc['raised_at']}",
            f"reason: {esc['reason']}",
            f"your recommendation was: {esc['recommendation'] or '(none)'}",
            "--- end item ---",
            "",
        ]
    if story is not None:
        lines += [
            f"STORY #{story['id']}: {story['title']}",
            f"status: {story['status']}   project: {story['project'] or 'unknown'} "
            f"({story['project_source'] or 'unset'})",
            "--- brief ---",
            (story["description"] or "(empty)")[:3000],
            "--- end brief ---",
            "",
        ]
    if history:
        lines.append("--- the conversation so far ---")
        for h in history[-8:]:
            who = "JORDAN" if h["author"] == "po" else "YOU"
            lines.append(f"{who} ({h['at']}): {h['body'][:1200]}")
        lines += ["--- end conversation ---", ""]

    lines += [
        "--- what he just said ---",
        msg["body"][:6000] or "(nothing written — see the attachments)",
        "--- end ---",
        "",
    ]
    # By path, not by base64. `.colony/` is already inside the read scope, an
    # image costs the same either way, and a prompt that carries its evidence by
    # reference is one you can still read in the ticket a week later.
    if attached:
        lines += [attach.evidence(attached), ""]

    lines += [
        "Project folders that exist under D:\\ALL STUFF\\PROJECTS:",
        *(f"  {p}" for p in projects),
        "",
        "You may read files under D:\\ALL STUFF\\PROJECTS to check anything he",
        "refers to. Be frugal — a few targeted reads, not a survey.",
        "",
        "What you can establish, and what you cannot. Your tools are Read, Grep",
        "and Glob. You have no Bash, you cannot run a script, and you cannot call",
        "an API. So you can establish what a file contains, and you can establish",
        "nothing whatever about whether code works. Never write that something is",
        "live, running, working, fixed, verified or no longer failing. You have",
        "no way to see any of that, and an agent downstream will read the line as",
        "a finding and build on it.",
        "",
        "When he tells you he has done something, check it and name the file you",
        "checked. A `.env` file is outside your read scope and always will be. A",
        "`.env.example` is a committed template: a value in it says nothing about",
        "the `.env` sitting next to it, and reporting one as the other is how this",
        "rule came to be written. If the claim rests on a file you cannot read,",
        "say so — \"I cannot read .env, so I am taking your word for it\" is a",
        "useful sentence and a false confirmation is not.",
        "",
        "Your job is NOT to have a conversation. A reply that produces only prose",
        "leaves this story exactly where it was, and a story that sits still while",
        "the two of you talk about it is the failure this loop exists to prevent.",
        "Every reply must move the ledger, and there are only two ways to do that:",
        "",
        "  settled          — he told you something the work needed. Write it down",
        "                     as standing fact and the story goes back in the groom",
        "                     queue, where an agent turns it into build tasks.",
        "  still_blocked_on — something is STILL missing. Name the one decision,",
        "                     as a direct question, and it becomes a card in his",
        "                     Inbox rather than a sentence in a thread he has to",
        "                     remember to re-read.",
        "",
        "Both at once is normal and is the most useful answer you can give: he",
        "answered part of it, and here is precisely the next thing you need.",
        "Neither is a last resort — use it only when he asked you a question that",
        "was purely informational and nothing about the work changed.",
        "",
        "Do not write \"next step is scoping this as a real build task\" and stop.",
        "Putting it in `settled` IS how you scope it: the next wake grooms it.",
        "",
        "Still yours to refuse: approving, rejecting and confirming a project are",
        "his decisions, and this reply makes none of them.",
        "",
        "Reply with ONLY a JSON object:",
        "",
        "{",
        '  "answer": "what you are saying back to Jordan, under 1200 characters",',
        '  "settled": "what he decided, written as fact for an agent who was not in',
        '              this conversation and will read only this line. If you could not',
        '              check it yourself, begin the line with `Jordan says` — or null",',
        '  "still_blocked_on": "the ONE specific decision that now blocks this work,',
        '              phrased as a question only he can answer — or null if nothing',
        '              is blocking and the work can proceed",',
        '  "project": "a folder from the list if his message settled which one, else null",',
        '  "new_project": "a folder name he asked you to treat as new work, else null",',
        '  "recommendation": "a revised one-line recommendation for the Inbox tile, or null",',
        '  "learned": "one durable thing worth keeping, or null",',
        '  "checked": ["the files you actually opened to support `settled`, by path.',
        '              Empty if you opened none — that is a fine answer and a far',
        '              better one than a path you did not read"]',
        "}",
    ]
    return "\n".join(lines)


def answer_po(conn: sqlite3.Connection, terms: dict, projects: list[str]) -> list[dict]:
    """Read the unread PO messages and write answers into the same threads."""
    out: list[dict] = []
    for msg in control.unread_messages(conn, REPLY_LIMIT):
        esc = story = None
        if msg["escalation_id"]:
            esc = conn.execute("SELECT * FROM escalations WHERE id = ?",
                               (msg["escalation_id"],)).fetchone()
        if msg["story_id"]:
            story = conn.execute("SELECT * FROM stories WHERE id = ?",
                                 (msg["story_id"],)).fetchone()
        history = [h for h in control.thread(conn, escalation_id=msg["escalation_id"],
                                             story_id=msg["story_id"])
                   if h["id"] != msg["id"]]

        prompt = reply_prompt(msg, esc, story, projects, history,
                              attach.for_story(conn, msg["story_id"]))

        # `control.reply` already opened the ticket the PO has been watching in
        # the Queue. Claim that one — staffing it and filling in the work order
        # it could not know an hour ago — rather than opening a second. A reply
        # written before 010 has no ticket, so one is made here; that branch is
        # for the rows already in the ledger, not a second way to do this.
        row = conn.execute(
            "SELECT id FROM tickets WHERE po_message_id = ? ORDER BY id DESC LIMIT 1",
            (msg["id"],),
        ).fetchone()
        if row:
            ticket_id = row["id"]
            conn.execute(
                "UPDATE tickets SET status = 'staffed', role = ?, work_order = ? WHERE id = ?",
                (terms["role"], prompt, ticket_id),
            )
        else:
            cur = conn.execute(
                """INSERT INTO tickets (story_id, title, intent, role, status,
                                        work_order, requires_po, po_message_id)
                   VALUES (?,?,'research',?,'staffed',?,0,?)""",
                (msg["story_id"], f"Reply to Ordis: {msg['body'][:120]}", terms["role"],
                 prompt, msg["id"]),
            )
            ticket_id = cur.lastrowid

        result = agent.run_ticket(
            conn, ticket_id=ticket_id, role=terms["role"], prompt=prompt,
            model=terms["model"], tools_allowed=terms["tools_allowed"],
            tools_denied=terms.get("tools_denied"), cwd=db.PROJECTS_ROOT,
            timeout_s=REPLY_TIMEOUT_S, max_tokens=terms.get("max_tokens_run"),
        )
        answer = result.json_payload() if result.status in ("ok", "killed-over-budget") else None

        if not answer:
            # The message is *not* marked read. An answer that never arrived is
            # a question still waiting, and silently swallowing it would be the
            # one failure mode this whole feature exists to prevent.
            conn.execute("UPDATE tickets SET status = 'blocked', findings = ? WHERE id = ?",
                         ((result.error or result.text or result.status)[:2000], ticket_id))
            out.append({"message_id": msg["id"], "tokens": result.chargeable_tokens,
                        "verdict": f"no answer ({result.status})"})
            continue

        text = (answer.get("answer") or "").strip() or "(no answer given)"
        conn.execute(
            "INSERT INTO po_messages (escalation_id, story_id, author, body, status, tokens) "
            "VALUES (?,?,'ordis',?,'read',?)",
            (msg["escalation_id"], msg["story_id"], text[:4000], result.chargeable_tokens),
        )
        conn.execute(
            "UPDATE po_messages SET status = 'answered', read_at = datetime('now','localtime') "
            "WHERE id = ?",
            (msg["id"],),
        )
        conn.execute(
            "UPDATE tickets SET status = 'done', findings = ?, closed_at = datetime('now','localtime') "
            "WHERE id = ?",
            (json.dumps(answer, indent=2)[:8000], ticket_id),
        )

        # A revised recommendation replaces the one on the tile, so the Inbox
        # shows the current advice rather than the advice the PO just argued
        # with.
        if esc is not None and answer.get("recommendation"):
            conn.execute("UPDATE escalations SET recommendation = ? WHERE id = ?",
                         (control.card_text(answer["recommendation"]), esc["id"]))

        # ── what the reply changed ───────────────────────────────────────────
        #
        # Before this, a reply could write prose and nothing else, and that is
        # what it mostly did: Ordis would work out the right next step, say so
        # in the thread, close the card as 'amend' and leave the story parked in
        # `needs-info` where nothing grooms it. The answer existed, in a
        # transcript, where no agent reads.
        #
        # So a reply now lands in one of two places. `settled` becomes standing
        # fact on the story and puts it back in the groom queue. `still_blocked`
        # becomes a card in the Inbox naming the one thing missing. Both may
        # happen at once. If neither does, the old card stays open on purpose —
        # an exchange that moved nothing has not answered anything, and letting
        # it close would be the loop agreeing that talking counted as progress.
        settled = str(answer.get("settled") or "").strip()
        blocked_on = str(answer.get("still_blocked_on") or "").strip()

        # A settled line becomes standing fact: the groom agent reads it, writes
        # acceptance criteria on top of it, and never sees this conversation. So
        # the basis has to travel with the claim. Ordis has no Bash and cannot
        # observe a running program, and the reply that forced this said
        # "NOTION_OG_TRACKER_DB is set in .env.example — it's live now, not just
        # logging 'not set'", having read a committed template and nothing else.
        # The key really is set, but in `.env`, which he cannot read; and "live
        # now" was something he had no way to observe and which was not true. The
        # next groom wrote acceptance criteria on top of both. So if he names no
        # file he actually opened, the line goes down as Jordan's word.
        raw_checked = answer.get("checked") or []
        if isinstance(raw_checked, str):
            raw_checked = [raw_checked]
        checked = [str(p).strip() for p in raw_checked if str(p).strip()]
        if settled and not checked and not settled.lower().startswith("jordan says"):
            settled = f"Jordan says: {settled} (Ordis opened no file to check this.)"
        acted: list[str] = []

        # Closed first, and only on action. The fresh card below checks for an
        # open question before raising one, so an old card left standing here
        # would suppress the sharper one that replaces it.
        if esc is not None and (settled or blocked_on):
            conn.execute(
                "UPDATE escalations SET resolved_at = datetime('now','localtime'), "
                "po_decision = 'amend' WHERE id = ? AND resolved_at IS NULL",
                (esc["id"],),
            )

        if story is not None and settled:
            entry = f"[PO, {msg['at']}] {settled}"
            conn.execute(
                """UPDATE stories SET po_answers = COALESCE(po_answers || ?, ?),
                          updated_at = datetime('now','localtime')
                    WHERE id = ?""",
                ("\n\n" + entry, entry, story["id"]),
            )
            _said(conn, story["id"], "decided",
                  settled + ("\n\nOrdis read: " + ", ".join(checked) if checked else ""),
                  ticket_id, 0)
            if story["status"] == "needs-info":
                conn.execute(
                    """UPDATE stories SET status = 'backlog', blocked_reason = NULL,
                              updated_at = datetime('now','localtime')
                        WHERE id = ? AND status = 'needs-info'""",
                    (story["id"],),
                )
                # The runs that raised the answered question describe a version
                # of the story that is gone; without this the story re-enters
                # the queue already at its attempt ceiling and never groomed.
                control.regroom_budget(conn, story["id"])
                # Every card that said it could not start, not just the one he
                # happened to reply to.
                control.clear_needs_info(conn, story["id"])
                acted.append("unblocked → back in the groom queue")
            else:
                acted.append("recorded on the story")

        if story is not None and blocked_on:
            conn.execute(
                """UPDATE stories SET status = 'needs-info', blocked_reason = ?,
                          updated_at = datetime('now','localtime')
                    WHERE id = ?""",
                (control.card_text(blocked_on), story["id"]),
            )
            if not control.question_settled(conn, story["id"], "needs-info",
                                            story["notion_hash"]):
                conn.execute(
                    """INSERT INTO escalations (story_id, ticket_id, kind, reason,
                                                recommendation, est_tokens, raised_hash)
                       VALUES (?,?,'needs-info',?,?,?,?)""",
                    (story["id"], ticket_id,
                     f'"{story["title"]}" is blocked on one decision.',
                     control.card_text(blocked_on), result.chargeable_tokens, story["notion_hash"]),
                )
                acted.append("new blocker raised in your Inbox")

        # Ordis may *suggest* a folder from a reply, never confirm one. Naming
        # the project is a PO action and stays one (§8.2).
        suggested = answer.get("project")
        if (story is not None and suggested and suggested in projects
                and story["project_source"] != "confirmed"):
            conn.execute("UPDATE stories SET project = ? WHERE id = ?",
                         (suggested, story["id"]))

        if msg["story_id"]:
            _event(conn, msg["story_id"], "note", "Ordis answered the PO",
                   text[:2000], ticket_id, result.chargeable_tokens)
            if answer.get("learned"):
                _said(conn, msg["story_id"], "learning", answer["learned"], ticket_id, 0)

        out.append({"message_id": msg["id"], "tokens": result.chargeable_tokens,
                    "verdict": " · ".join(acted) if acted else "answered (nothing moved)",
                    "answer": text[:200]})
    return out


def unanswered_count(conn: sqlite3.Connection) -> int:
    """How many PO replies are waiting — the tick uses this to decide to wake."""
    return conn.execute(
        "SELECT COUNT(*) n FROM po_messages WHERE author = 'po' AND status = 'unread'"
    ).fetchone()["n"]


# -- staffing: the Scrum Master's job, not the PO's ----------------------------
#
# "The point for this is so that I, the product owner, does not have to pick the
# agents for the job. The scrum master (ordis) should know all capabilities of
# each persona through context of title then digging deeper and seeing if they
# are a right fit OR just remembering the performance they had in a previous
# project WITHOUT BIAS."
#
# What stood there before was an Inbox tile reading "nobody is hired to write in
# personal-desktop-projects - open Standby, pick a persona and hire them with
# write scope on it". Every word of that is the PO doing the Scrum Master's job,
# on a roster of 270 people he has never read, and it is why a story that had
# cleared every other gate still had not started.
#
# So the colony proposes the name and he answers yes or no. `propose_hire` and
# the `hire` escalation kind have both existed since M3 and nothing had ever
# called them; this is the caller they were waiting for.

STAFF_LIMIT = int(os.environ.get("COLONY_STAFF_LIMIT", "1"))
STAFF_TIMEOUT_S = int(os.environ.get("COLONY_STAFF_TIMEOUT", "420"))

# Where the persona files themselves live. The digest carries a one-line
# description; the file is the resume, and reading two or three of them is the
# "digging deeper" half of what the PO asked for.
PERSONA_ROOT = roster_mod.DEFAULT_ROSTER_DIR


def stories_to_staff(conn: sqlite3.Connection, limit: int = STAFF_LIMIT) -> list[sqlite3.Row]:
    """Stories that have cleared every gate and are waiting on a person.

    Ready, confirmed folder, nobody hired to write there, and no hire already
    sitting in the Inbox - proposing a second name for the same story while the
    first is undecided turns one question into a queue of them.
    """
    return conn.execute(
        """SELECT s.* FROM stories s
            WHERE s.status = 'ready' AND s.dropped_at IS NULL AND s.settled_as IS NULL
              AND s.project IS NOT NULL AND s.project_source = 'confirmed'
              AND NOT EXISTS (SELECT 1 FROM agents a
                               WHERE a.project = s.project AND a.write_capable = 1
                                 AND a.status <> 'retired')
              AND NOT EXISTS (SELECT 1 FROM escalations e
                               WHERE e.story_id = s.id AND e.kind = 'hire'
                                 AND e.resolved_at IS NULL)
            ORDER BY s.updated_at LIMIT ?""",
        (limit,),
    ).fetchall()


def staff_prompt(story: sqlite3.Row, digest: str, attached: list[dict] | None = None) -> str:
    """The work order for one hiring decision."""
    criteria = (story["acceptance_criteria"] or "").strip() or "(none recorded)"
    brief = (story["description"] or "").strip() or "(the Notion page body is empty)"
    return f"""You are Ordis, Scrum Master of a colony of Claude agents. Jordan is the Product
Owner. You are READ-ONLY: Read, Grep and Glob.

One of his stories has cleared the criteria gate and has a confirmed project
folder, so the only thing between it and real work is that nobody is hired to do
it. Choosing who does the work is YOUR job. He picks nobody here; he reads the
name you bring him and says yes or no.

STORY #{story['id']}: {story['title']}
project: {story['project']}   (the write scope will be {story['project']}/ and nothing else)

--- brief ---
{brief[:4000]}
--- end brief ---

--- acceptance criteria, which Jordan has already approved ---
{criteria[:3000]}
--- end criteria ---
{attach.evidence(attached or [])}

Read D:\\ALL STUFF\\PROJECTS\\{story['project']}\\PROJECT.md and enough of that
tree to know what the work actually is. You cannot choose who should do a job
you have not looked at. Be frugal - a few targeted reads.

--- the roster: every persona available, by division ---
{digest}
--- end roster ---

The persona files are under {PERSONA_ROOT}, one per slug. Open the two or three
you are seriously considering. The line in the list above is a title; the file
is the resume, and the gap between them is where most wrong hires happen.

How to choose. These are rules, not advice:

  * Fit is to the WORK IN THE CRITERIA, not to the sound of the story's title.
    A story about a job-search tool is not automatically an engineering story,
    and a story about a scanner is not automatically a security one.
  * "<<hired Nx>>" means that persona already holds N contracts in this colony.
    Treat it as a reason to look harder at everybody else. It is never on its
    own a reason to pick someone. In Jordan's words: "I do not want to only see
    one agent being chosen over and over again just because we found one that
    works. This environment needs to be diverse."
  * Consider candidates from more than one division, genuinely. If your three
    finalists all come from the same division you narrowed too early - go back
    to the roster and read a part of it you skipped.
  * The only past performance that counts is work a persona actually produced
    here, on a previous ticket. Not familiarity, not that you can picture them,
    not that the name surfaced first. If there is no record of them working
    here, say so plainly - an unproven persona who fits the criteria beats a
    proven one who does not.
  * There is no penalty for hiring someone new. Nearly every persona on that
    roster has never been picked once.

Show your work: name the two finalists you did NOT choose and what separated
them. A choice you cannot account for is one Jordan has no way to check.

Reply with ONLY a JSON object:

{{
  "roster_slug": "exactly one slug from the roster above",
  "role": "short-kebab-case name for how the colony refers to them on this project",
  "why": "what in the acceptance criteria this persona is for, under 400 characters",
  "finalists": [
    {{"slug": "...", "why_not": "what separated them from your pick"}},
    {{"slug": "...", "why_not": "..."}}
  ],
  "read": ["persona files you actually opened"],
  "model": "claude-sonnet-5",
  "max_tokens_run": 400000
}}"""


_ROLE_OK = re.compile(r"[^a-z0-9-]+")


def _role_name(raw: str, slug: str) -> str:
    """A role the `agents` table will accept, out of what the agent asked for."""
    name = _ROLE_OK.sub("-", (raw or "").strip().lower()).strip("-")
    if not name:
        name = _ROLE_OK.sub("-", slug.split("/")[-1].lower()).strip("-")
    return name[:60] or "builder"


def _diversity_note(conn: sqlite3.Connection, pick: sqlite3.Row,
                    finalists: list[dict]) -> str:
    """What the ledger says about this choice, for the PO to read beside it.

    Written by Python rather than by the agent, on purpose. The rule exists
    because the chooser has a preference it cannot see, so the audit of the
    choice must not be the chooser's own account of it.
    """
    bits: list[str] = []
    if pick["times_hired"]:
        bits.append(f"already hired {pick['times_hired']}x here "
                    f"(last {pick['last_hired_at'] or 'unknown'})")
    else:
        bits.append("first contract in this colony")

    divisions = {pick["division"]}
    for f in finalists:
        row = conn.execute("SELECT division FROM roster WHERE slug = ?",
                           (str(f.get("slug") or ""),)).fetchone()
        if row:
            divisions.add(row["division"])
    if len(divisions) < 2:
        bits.append(f"every finalist came from {pick['division']} - the search "
                    f"stayed inside one division")
    else:
        bits.append("finalists spanned " + ", ".join(sorted(divisions)))
    return " - ".join(bits)


def staff_stories(conn: sqlite3.Connection, terms: dict) -> list[dict]:
    """Propose one hire per ready-but-unstaffed story. Hires nothing."""
    out: list[dict] = []
    stories = stories_to_staff(conn, STAFF_LIMIT)
    if not stories:
        return out
    digest = roster_mod.digest(conn)
    if not digest:
        return out

    for story in stories:
        prompt = staff_prompt(story, digest, attach.for_story(conn, story["id"]))
        cur = conn.execute(
            """INSERT INTO tickets (story_id, title, intent, role, status, work_order,
                                    requires_po)
               VALUES (?,?,'research',?,'staffed',?,0)""",
            (story["id"], f"Staff: {story['title']}"[:200], terms["role"], prompt),
        )
        ticket_id = cur.lastrowid

        result = agent.run_ticket(
            conn, ticket_id=ticket_id, role=terms["role"], prompt=prompt,
            model=terms["model"], tools_allowed=terms["tools_allowed"],
            tools_denied=terms.get("tools_denied"), cwd=db.PROJECTS_ROOT,
            timeout_s=STAFF_TIMEOUT_S, max_tokens=terms.get("max_tokens_run"),
        )
        answer = result.json_payload() if result.status in ("ok", "killed-over-budget") else None
        if not answer or not answer.get("roster_slug"):
            conn.execute(
                "UPDATE tickets SET status = 'blocked', findings = ?, "
                "closed_at = datetime('now','localtime') WHERE id = ?",
                ((result.error or result.text or "no usable answer")[:2000], ticket_id),
            )
            out.append({"story_id": story["id"], "tokens": result.chargeable_tokens,
                        "verdict": "no pick returned"})
            continue

        slug = str(answer["roster_slug"]).strip()
        pick = conn.execute(
            "SELECT slug, name, division, times_hired, last_hired_at FROM roster WHERE slug = ?",
            (slug,),
        ).fetchone()
        if not pick:
            conn.execute(
                "UPDATE tickets SET status = 'blocked', findings = ?, "
                "closed_at = datetime('now','localtime') WHERE id = ?",
                (f"picked {slug!r}, which is not a slug in the roster", ticket_id),
            )
            out.append({"story_id": story["id"], "tokens": result.chargeable_tokens,
                        "verdict": f"invalid pick {slug!r}"})
            continue

        finalists = [f for f in (answer.get("finalists") or []) if isinstance(f, dict)][:3]
        role = _role_name(str(answer.get("role") or ""), slug)
        # A role name already taken on this project would be refused at approval
        # time, which is the worst possible moment to find out.
        taken = conn.execute(
            "SELECT 1 FROM agents WHERE role = ? AND project IS ?", (role, story["project"])
        ).fetchone()
        if taken:
            role = f"{role}-2"[:60]

        why = " ".join(str(answer.get("why") or "").split())[:400]
        note = _diversity_note(conn, pick, finalists)
        losers = "; ".join(
            f"{f.get('slug')}: {' '.join(str(f.get('why_not') or '').split())[:140]}"
            for f in finalists if f.get("slug")
        )
        recommendation = f"{why}\n\n{note}."
        if losers:
            recommendation += f"\nAlso considered - {losers}"

        esc_id = control.propose_hire(
            conn, roster_slug=slug, role=role, project=story["project"],
            reason=control.card_text(recommendation),
            model=str(answer.get("model") or terms["model"]),
            write_capable=True,
            max_tokens_run=int(answer.get("max_tokens_run") or 400000),
            story_id=story["id"],
        )
        conn.execute(
            "UPDATE tickets SET status = 'done', findings = ?, "
            "closed_at = datetime('now','localtime') WHERE id = ?",
            (f"proposed {pick['name']} ({slug}) as {role}. {note}."[:2000], ticket_id),
        )
        conn.execute(
            """INSERT INTO story_events (story_id, kind, summary, detail, ticket_id, tokens)
               VALUES (?,'staffed',?,?,?,?)""",
            (story["id"],
             f"Ordis proposed {pick['name']} ({pick['division']}) as {role}"[:400],
             recommendation[:2000], ticket_id, result.chargeable_tokens),
        )
        out.append({"story_id": story["id"], "escalation_id": esc_id,
                    "tokens": result.chargeable_tokens, "slug": slug,
                    "name": pick["name"], "division": pick["division"], "role": role,
                    "verdict": f"proposed {pick['name']} as {role} - waiting on you"})
    return out


def run(conn: sqlite3.Connection, usage: dict | None) -> dict:
    """Do the wake. Returns what happened, for the pulse row and the printout."""
    report = {"groomed": [], "built": [], "answered": [], "forged": [], "candidates": [],
              "staffed": [], "tokens": 0, "skipped": None}

    if control.is_halted():
        # Belt-and-braces: the tick already refuses to escalate to a wake while
        # HALT is present. This is the second reader of the same file, because
        # the one control that must never fail open is the stop switch.
        report["skipped"] = "HALT — dispatch disabled"
        return report

    budget = budget_state(conn, usage)
    if not budget["ok"]:
        report["skipped"] = budget["why"]
        return report

    # Grooming first: it is the cheaper of the two jobs, and a story groomed in
    # this hour can be dispatched before the next one comes round.
    terms = contract(conn, "investigator")
    if terms is None:
        report["skipped"] = "no active investigator contract — run `python -m colony init`"
    else:
        # Before anything else: whatever the PO said. Working a backlog he has
        # just re-scoped is the most expensive kind of wrong.
        projects = pulse_mod.candidate_projects()
        for outcome in answer_po(conn, terms, projects):
            report["answered"].append(outcome)
            report["tokens"] += outcome["tokens"]

        # Then any skill draft the PO asked for. Ahead of grooming because it is
        # bounded — one draft per requested candidate, and the request had to be
        # made by hand — while the groom queue is however long Notion made it.
        for skill in forge_mod.pending_drafts(conn)[:FORGE_DRAFT_LIMIT]:
            outcome = forge_mod.draft(conn, skill["id"], terms)
            report["forged"].append(outcome)
            report["tokens"] += outcome["tokens"]

    # Read the queue *after* the replies, not before. A reply that supplies the
    # missing decision puts its story straight back into this queue, and asking
    # an hour early would mean the answer waits a full pulse to become work —
    # which is most of what "nothing is happening" felt like.
    stories = stories_to_groom(conn, GROOM_LIMIT) if terms is not None else []
    if terms is not None and stories:
        projects = pulse_mod.candidate_projects()
        for story in stories:
            outcome = groom_story(conn, story, terms, projects)
            report["groomed"].append(outcome)
            report["tokens"] += outcome["tokens"]

    # Staffing goes after grooming and before building, because grooming is what
    # produces the stories that need staffing and the PO has to approve a name
    # before a build can use it. A hire proposed this hour is approvable the
    # moment he looks at the Inbox, and dispatchable the hour after.
    if terms is not None:
        for outcome in staff_stories(conn, terms):
            report["staffed"].append(outcome)
            report["tokens"] += outcome["tokens"]

    # Then whatever the PO dispatched. `build.pending` is already narrowed to
    # tickets on a *confirmed* project, so nothing reaches a worktree on the
    # strength of an inference.
    for outcome in build_mod.run(conn):
        report["built"].append(outcome)
        report["tokens"] += outcome["tokens"]

    if (not report["groomed"] and not report["built"] and not report["answered"]
            and not report["forged"] and not report["staffed"] and not report["skipped"]):
        report["skipped"] = "nothing to groom and nothing dispatched"
    return report
