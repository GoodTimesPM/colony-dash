"""The wake tier: the only part of the pulse that spends tokens.

A tick decides whether this hour is worth a model; this module does the
work. Its jobs, in order: answer PO replies, draft requested skills,
**groom** stories (read-only, ending at the PO's criteria gate,
ARCHITECTURE.md §4.2), propose staffing, then **build** dispatched stories
via `build.py`.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3

from . import (agent, attachments as attach, build as build_mod, control, db,
               forge as forge_mod, pulse as pulse_mod, roster as roster_mod,
               usage as usage_mod, voice)

# Stories groomed per wake, so a bulk Notion import is not a bulk spend.
GROOM_LIMIT = int(os.environ.get("COLONY_GROOM_LIMIT", "2"))

GROOM_TIMEOUT_S = int(os.environ.get("COLONY_GROOM_TIMEOUT", "420"))

# Skill drafts per wake; the PO reads the first before paying for more.
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
    """Is there room in the week's allowance to spend anything? Measured as a
    share of the 7-day window (§6.1). No usage sample means no dispatch.
    """
    # Base plus any PO boost (§6.1), stored apart so the baseline stays
    # visible.
    band = control.effective_allowance(conn)
    allowance = band["effective"]

    if not usage or usage.get("seven_day") is None:
        return {"ok": False, "allowance": allowance, "used": None,
                "why": "no usage sample. Refusing to dispatch blind"}
    used = float(usage["seven_day"])
    if used >= allowance:
        # base and delta, not a hardcoded 35 and a "+" that assumed the dial
        # only went up. The PO can now walk the allowance down as well.
        boosted = f" ({band['base']:g}% baseline {band['boost']:+.0f})" if band["boost"] else ""
        return {"ok": False, "allowance": allowance, "used": used,
                "why": f"week at {used:.0f}% is at or past the {allowance:.0f}% "
                       f"colony allowance{boosted}"}
    return {"ok": True, "allowance": allowance, "used": used, "why": None,
            "boost": band["boost"]}


# The groom-queue predicate, shared with the tick so it never wakes for
# nothing. `po-review`, `ready` and `needs-info` are excluded. MAX_ATTEMPTS
# stops a story that keeps failing from being groomed at full price forever.
MAX_ATTEMPTS = 2

# `t.status <> 'wontfix'` resets the attempt count: when a brief changes, its
# old groom tickets are marked wontfix so the rewrite can be groomed again.
GROOMABLE_WHERE = """
    status IN ('backlog','needs-criteria')
    AND dropped_at IS NULL
    -- Filed by the PO: Done, Shipped, Shelved, New or Not started. A row the
    -- PO has not started is not a row they are waiting on, and grooming it
    -- produces a question about a decision they have deliberately not made.
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
    """What the story already has, stated before what is left, so finished
    items are not raised again. About forty tokens.
    """
    done, todo = _checklist(story, "done_items"), _checklist(story, "open_items")
    if not done and not todo:
        return ""
    out = ["", "--- progress, as of the last Notion sync ---"]
    if done:
        out.append(f"ALREADY DONE ({len(done)}). Treat these as closed. Do not"
                   " re-raise them, do not ask about them, do not put them in criteria:")
        out += [f"  [x] {i}" for i in done[:40]]
    if todo:
        out.append(f"STILL OPEN ({len(todo)}). This is the actual scope:")
        out += [f"  [ ] {i}" for i in todo[:40]]
    out.append("--- end progress ---")
    return "\n".join(out)


def _settled_section(story: sqlite3.Row) -> str:
    """Decisions the PO made in Inbox threads, stated as standing fact so the
    agent does not ask again.
    """
    try:
        raw = (story["po_answers"] or "").strip()
    except (IndexError, KeyError):
        return ""
    if not raw:
        return ""
    return ("\n--- what the PO has already settled, in the Inbox ---\n"
            "These are decisions, not suggestions. Do not ask about them again.\n"
            + raw[:4000] + "\n--- end settled ---")


def groom_prompt(story: sqlite3.Row, projects: list[str],
                 attached: list[dict] | None = None) -> str:
    """The work order. Explicit about the gate, so the agent can't overstep it."""
    body = (story["description"] or "").strip() or "(the Notion page body is empty)"
    return f"""You are Ordis, Scrum Master of a colony of Claude agents. You are grooming one
backlog story for the Product Owner. You are READ-ONLY: you have
Read, Grep and Glob and nothing else. Do not attempt to modify anything.

STORY #{story['id']}: {story['title']}
Notion status: {story['notion_status'] or 'unknown'}
Current guess at project folder: {story['project'] or 'none, unknown'}

--- brief from the Notion page body ---
{body[:6000]}
--- end brief ---
{_progress_section(story)}
{_settled_section(story)}
{attach.evidence(attached or [])}

Project folders that exist under {db.PROJECTS_ROOT} (a story belongs to one
of these, or to none if it is new work):
{chr(10).join('  ' + p for p in projects)}

You may read files under {db.PROJECTS_ROOT} to understand context. Each
project has a PROJECT.md at its root that states its current status. Read the
relevant one before deciding anything. Be frugal: a few targeted reads, not a
survey.

Your job is to answer one question: **is there enough here to build?**

- If NO, say precisely what decision is missing. Not "needs more detail". Name
  the specific thing only the PO can decide (a target platform, a scope
  boundary, which of two approaches). One missing decision is enough.
- If YES, draft acceptance criteria: 3-6 concrete, checkable statements. Each
  one must be something you could later verify as done or not done. No vague
  quality words.

You do NOT decide that this story is ready to work on. The PO does. You are
drafting for their approval.

{voice.STYLE}

Reply with ONLY a JSON object, no prose around it:

{{
  "enough_info": true or false,
  "missing": "the specific decision the PO must make, or null if enough_info",
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
    """Record a colonist's text: the gist in `summary` (truncated at 400) and
    the whole text in `detail`, so nothing is cut mid-word.
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
    # Active skills for this role go in front of the work order; this is the
    # only place a skill takes effect.
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

    # Recorded before the early returns, so a failed run counts against the
    # skill.
    forge_mod.record_uses(
        conn, skills=skills, run_id=result.raw.get("run_id"),
        tokens=result.chargeable_tokens,
        ok=result.status == "ok",
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
             "Raise the investigator's ceiling or narrow the work order. The run itself "
             "completed and its answer was kept.",
             result.chargeable_tokens),
        )

    # A completed run is harvested even when it breached the ceiling. Only a run
    # that produced nothing is a failure.
    if result.status != "ok":
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
        # Gate 1: Ordis drafts, only the PO marks ready. `raised_hash` records
        # which version of the story this answers.
        conn.execute(
            """INSERT INTO escalations (story_id, ticket_id, kind, reason, recommendation,
                                        est_tokens, raised_hash)
               VALUES (?,?,'decision',?,?,?,?)""",
            (story["id"], ticket_id,
             f'"{story["title"]}" has draft acceptance criteria awaiting your approval.',
             control.card_text(criteria), result.chargeable_tokens, story["notion_hash"]),
        )
        # The story just stopped being blocked. It has criteria and it is
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
        # Stale cards do not suppress a fresh question; a dismissed card does,
        # while its brief is unchanged.
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
# Replies run before every other job: grooming a story the PO just redefined
# wastes the hour. Capped hard; anything bigger should be a story.

ARROW = "\u2192"

REPLY_LIMIT = int(os.environ.get("COLONY_REPLY_LIMIT", "3"))
REPLY_TIMEOUT_S = int(os.environ.get("COLONY_REPLY_TIMEOUT", "300"))


def reply_prompt(msg: sqlite3.Row, esc: sqlite3.Row | None, story: sqlite3.Row | None,
                 projects: list[str], history: list[dict],
                 attached: list[dict] | None = None) -> str:
    """The work order for one PO reply."""
    lines = [
        "You are Ordis, Scrum Master of a colony of Claude agents. The Product",
        "Owner has written to you about one item in their PO Inbox, and you are",
        "answering them directly. You are READ-ONLY: Read, Grep and Glob.",
        "",
    ]
    if esc is not None:
        lines += [
            "--- the Inbox item they are replying to ---",
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
            who = "THE PO" if h["author"] == "po" else "YOU"
            lines.append(f"{who} ({h['at']}): {h['body'][:1200]}")
        lines += ["--- end conversation ---", ""]

    lines += [
        "--- what they just said ---",
        msg["body"][:6000] or "(nothing written, see the attachments)",
        "--- end ---",
        "",
    ]
    # By path, not base64 (see attachments.py).
    if attached:
        lines += [attach.evidence(attached), ""]

    lines += [
        f"Project folders that exist under {db.PROJECTS_ROOT}:",
        *(f"  {p}" for p in projects),
        "",
        f"You may read files under {db.PROJECTS_ROOT} to check anything the PO",
        "refers to. Be frugal. A few targeted reads, not a survey.",
        "",
        "What you can establish, and what you cannot. Your tools are Read, Grep",
        "and Glob. You have no Bash, you cannot run a script, and you cannot call",
        "an API. So you can establish what a file contains, and you can establish",
        "nothing whatever about whether code works. Never write that something is",
        "live, running, working, fixed, verified or no longer failing. You have",
        "no way to see any of that, and an agent downstream will read the line as",
        "a finding and build on it.",
        "",
        f"You are running INSIDE pulse pid {os.getpid()}, right now. So:",
        f"  - `.colony/pulse.lock` holds pid {os.getpid()}. That is you. A lock",
        "    file is not evidence of a stuck process; it is evidence that a pulse",
        "    is running, and the pulse that is running is the one reading you this.",
        "  - The last block in `.colony/pulse.log` is a header with no result",
        "    under it. That is also you. The result line is written when the beat",
        "    finishes, which cannot have happened yet.",
        "Never report the newest pulse as stuck, crashed, hung or silently failed,",
        "and never ask the PO to kill it. If you want to say something about the",
        "heartbeat, read the entries BEFORE the last one.",
        "",
        "When they tell you they have done something, check it and name the file",
        "checked. A `.env` file is outside your read scope and always will be. A",
        "`.env.example` is a committed template: a value in it says nothing about",
        "the `.env` sitting next to it, and reporting one as the other is how this",
        "rule came to be written. If the claim rests on a file you cannot read,",
        "say so. \"I cannot read .env, so I am taking your word for it\" is a",
        "useful sentence and a false confirmation is not.",
        "",
        "Your job is NOT to have a conversation. A reply that produces only prose",
        "leaves this story exactly where it was, and a story that sits still while",
        "the two of you talk about it is the failure this loop exists to prevent.",
        "Every reply must move the ledger, and there are only three ways to do that:",
        "",
        "  settled:           they told you something the work needed. Write it",
        "                     as standing fact and the story goes back in the groom",
        "                     queue, where an agent turns it into build tasks.",
        "  still_blocked_on:  something is STILL missing. Name the one decision,",
        "                     as a direct question, and it becomes a card in",
        "                     their Inbox rather than a sentence in a thread",
        "                     remember to re-read.",
        "  rescope:           they changed WHAT THE WORK IS. Not a fact the work",
        "                     needed: a different job.",
        "",
        "Both of the first two at once is normal and is the most useful answer you",
        "can give: the answered part of it, and here is precisely the next thing",
        "you need. Neither is a last resort. Use it only when they asked a",
        "question that was purely informational and nothing about the work",
        "changed.",
        "",
        "`rescope` is the one to get right, because getting it wrong is invisible",
        "and expensive. The acceptance criteria on a story are written once and",
        "are the ONLY thing the build agent treats as the job. `settled` adds a",
        "line of history under them; it does not touch them. So if the PO has",
        "narrowed, widened, replaced or abandoned the work and you file that as",
        "`settled`, the next build reads the old criteria, builds the old thing,",
        "finds it already shipped, and hands back an empty build. While the PO",
        "watches the colony ignore what they just said. That has happened, more",
        "than once, on this exact story.",
        "",
        "Put it in `rescope` when the PO says any of: only do X, drop Y, forget",
        "what you were working on, do Z instead, that part is done, start on the",
        "next thing. Anything that changes which items are in play. Write it as",
        "the new scope in full, the whole job as it stands now, not the delta, ",
        "because the agent that grooms it reads that line and nothing else about",
        "what changed. Naming the items the way the PO names them is right; if",
        "they said items 12, 14 and 15, say items 12, 14 and 15 and say where",
        "the list of items lives.",
        "",
        "A rescope clears the criteria and sends the story back to be groomed",
        "against the new scope. That is the point. Nothing already built is",
        "touched or undone. Do not withhold it to protect work in flight, and do",
        "not use it for a fact that leaves the job the same. That is `settled`.",
        "",
        "Do not write \"next step is scoping this as a real build task\" and stop.",
        "Putting it in `settled` IS how you scope it: the next wake grooms it.",
        "",
        "Still yours to refuse: approving, rejecting and confirming a project are",
        "their decisions, and this reply makes none of them.",
        "",
        voice.STYLE,
        "",
        "Reply with ONLY a JSON object:",
        "",
        "{",
        '  "answer": "what you are saying back to the PO, under 1200 characters",',
        '  "settled": "what they decided, written as fact for an agent not in',
        '              this conversation and will read only this line. If you could not',
        '              check it yourself, begin the line with `the PO says`, or null",',
        '  "still_blocked_on": "the ONE specific decision that now blocks this work,',
        '              phrased as a question only they can answer, or null if nothing',
        '              is blocking and the work can proceed",',
        '  "rescope": "the whole job as it now stands, if they changed what the work',
        '              is. The criteria are cleared and rewritten from this line, so',
        '              it has to stand alone. Null if the job is unchanged",',
        '  "project": "a folder from the list if their message settled which one, else null",',
        '  "new_project": "a folder name they asked you to treat as new work, else null",',
        '  "recommendation": "a revised one-line recommendation for the Inbox tile, or null",',
        '  "learned": "one durable thing worth keeping, or null",',
        '  "checked": ["the files you actually opened to support `settled`, by path.',
        '              Empty if you opened none, that is a fine answer and a far',
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

        # Claim the ticket `control.reply` opened. Older replies without one
        # get one.
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
        answer = result.json_payload() if result.status == "ok" else None

        if not answer:
            # Not marked read: an unanswered message is still waiting.
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

        # A revised recommendation replaces the one on the tile.
        if esc is not None and answer.get("recommendation"):
            conn.execute("UPDATE escalations SET recommendation = ? WHERE id = ?",
                         (control.card_text(answer["recommendation"]), esc["id"]))

        # ── what the reply changed ────────────────────────────────────────────
        # A reply lands in one or both places: `settled` becomes standing fact
        # and requeues the groom; `still_blocked` becomes an Inbox card naming
        # what is missing. If neither, the old card stays open; talking is not
        # progress.
        settled = str(answer.get("settled") or "").strip()
        blocked_on = str(answer.get("still_blocked_on") or "").strip()
        rescope = str(answer.get("rescope") or "").strip()

        # A settled line becomes fact the groom agent builds on, so its basis
        # travels with it. Ordis cannot run code or read `.env`, so if no file
        # was opened the line is recorded as the PO's word.
        raw_checked = answer.get("checked") or []
        if isinstance(raw_checked, str):
            raw_checked = [raw_checked]
        checked = [str(p).strip() for p in raw_checked if str(p).strip()]
        if settled and not checked and not settled.lower().startswith("the po says"):
            settled = f"The PO says: {settled} (Ordis opened no file to check this.)"
        acted: list[str] = []

        # Close the old card first, and only on action, so it cannot suppress
        # the new.
        if esc is not None and (settled or blocked_on or rescope):
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
            if rescope:
                # Handled below; rescope picks the lane itself.
                pass
            elif story["status"] == "needs-info":
                # Which lane depends on the criteria: approved goes back to
                # `ready`, drafted but unapproved is cleared for regrooming,
                # none goes to `backlog`. A groomed story in `backlog` with
                # criteria would sit unread.
                has_criteria = bool((story["acceptance_criteria"] or "").strip())
                approved = conn.execute(
                    "SELECT 1 FROM escalations WHERE story_id = ? AND kind = 'decision' "
                    "AND po_decision = 'approve' LIMIT 1", (story["id"],)).fetchone()
                if has_criteria and approved:
                    # Approved criteria survive the answer; resume at `ready`.
                    conn.execute(
                        """UPDATE stories SET status = 'ready', blocked_reason = NULL,
                                  updated_at = datetime('now','localtime')
                            WHERE id = ? AND status = 'needs-info'""",
                        (story["id"],),
                    )
                    lane = "unblocked " + ARROW + " back to ready, dispatchable"
                elif has_criteria:
                    # Unapproved criteria predate the answer; clear them and
                    # regroom the brief.
                    conn.execute(
                        """UPDATE stories
                              SET status = 'needs-criteria', acceptance_criteria = NULL,
                                  blocked_reason = NULL,
                                  updated_at = datetime('now','localtime')
                            WHERE id = ? AND status = 'needs-info'""",
                        (story["id"],),
                    )
                    _said(conn, story["id"], "note",
                          "The draft criteria on this story were written before "
                          "you answered, so they are cleared and the next wake "
                          "re-reads the whole brief. Nothing already built was "
                          "touched.", ticket_id, 0)
                    lane = "unblocked " + ARROW + " criteria redrafted from scratch"
                else:
                    conn.execute(
                        """UPDATE stories SET status = 'backlog', blocked_reason = NULL,
                                  updated_at = datetime('now','localtime')
                            WHERE id = ? AND status = 'needs-info'""",
                        (story["id"],),
                    )
                    lane = "unblocked " + ARROW + " back in the groom queue"
                # Reset the attempt budget, or the story re-enters at its
                # ceiling.
                control.regroom_budget(conn, story["id"])
                # Every card that said it could not start, not just the one the PO
                # happened to reply to.
                control.clear_needs_info(conn, story["id"])
                acted.append(lane)
            else:
                acted.append("recorded on the story")

        # Before the blocker: a rescope makes it moot, and `rescope_story`
        # closes it.
        if story is not None and rescope:
            entry = f"[PO, {msg['at']}] Scope now: {rescope}"
            conn.execute(
                """UPDATE stories SET po_answers = COALESCE(po_answers || ?, ?)
                    WHERE id = ?""",
                ("\n\n" + entry, entry, story["id"]),
            )
            acted.append(control.rescope_story(conn, story["id"], rescope))
            blocked_on = ""

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
    """How many PO replies are waiting. The tick uses this to decide to wake."""
    return conn.execute(
        "SELECT COUNT(*) n FROM po_messages WHERE author = 'po' AND status = 'unread'"
    ).fetchone()["n"]


# -- staffing: the Scrum Master's job, not the PO's ----------------------------
# The colony proposes who to hire and the PO answers yes or no, so the PO never
# has to read the roster to pick an agent.

STAFF_LIMIT = int(os.environ.get("COLONY_STAFF_LIMIT", "1"))
STAFF_TIMEOUT_S = int(os.environ.get("COLONY_STAFF_TIMEOUT", "420"))

# Most seats one staffing run may propose per story. A ceiling, not a target;
# each seat is an approval card with its own token ceiling. 1 means solo hires.
STAFF_TEAM_MAX = max(1, int(os.environ.get("COLONY_STAFF_TEAM_MAX", "3")))

# Persona files, which the staffing agent reads for a closer look at a pick.
PERSONA_ROOT = roster_mod.DEFAULT_ROSTER_DIR


def stories_to_staff(conn: sqlite3.Connection, limit: int = STAFF_LIMIT) -> list[sqlite3.Row]:
    """Stories past every gate and waiting on a hire: ready, confirmed folder,
    no writer hired, and no hire already pending.
    """
    return conn.execute(
        """SELECT s.* FROM stories s
            WHERE s.status = 'ready' AND s.dropped_at IS NULL AND s.settled_as IS NULL
              AND s.project IS NOT NULL AND s.project_source = 'confirmed'
              -- Staffed means "this story has somebody", not "this folder has
              -- somebody". The old check was the second one, and it is why a
              -- project could hold exactly one write-capable agent for its
              -- whole life: the first hire on any folder silenced staffing for
              -- every story that folder would ever have.
              --
              -- An agent carrying no story at all used to count here too, and
              -- that put the folder-wide rule back in through the side door.
              -- `og-tracker-sync-verifier` was hired onto `job-search` with no
              -- story, which made every story in `job-search` look staffed, so
              -- the PO's "hire a specialist for each of items 12, 14 and 15"
              -- could not be carried out: every dispatch re-used the one
              -- verifier hired for a different question weeks earlier. A hire
              -- with no story is a writer available to the folder, which is a
              -- fine thing to dispatch to as a fallback (`control.team`
              -- still ranks it last), and no evidence at all that THIS story
              -- has the specialist it needs.
              AND NOT EXISTS (SELECT 1 FROM agents a
                               WHERE a.write_capable = 1 AND a.status <> 'retired'
                                 AND a.project = s.project AND a.story_id = s.id)
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
    return f"""You are Ordis, Scrum Master of a colony of Claude agents. The Product
Owner. You are READ-ONLY: Read, Grep and Glob.

One of their stories has cleared the criteria gate and has a confirmed project
folder, so the only thing between it and real work is that nobody is hired to do
it. Choosing who does the work is YOUR job. The PO picks nobody here; they
read the name you bring and say yes or no.

STORY #{story['id']}: {story['title']}
project: {story['project']}   (the write scope will be {story['project']}/ and nothing else)

--- brief ---
{brief[:4000]}
--- end brief ---

--- acceptance criteria, which the PO has already approved ---
{criteria[:3000]}
--- end criteria ---
{attach.evidence(attached or [])}

Read {db.PROJECTS_ROOT / story['project']}\\PROJECT.md and enough of that
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
    own a reason to pick someone. In the PO's words: "I do not want to only see
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
them. A choice you cannot account for is one the PO has no way to check.

How many to hire. You may propose up to {STAFF_TEAM_MAX}. The default is one and
one is very often right, so read these before proposing more:

  * Propose a second seat only when the criteria contain work the first person
    is genuinely the wrong hire for. Not work they would find harder - work
    outside what they do. A backend engineer who also has to write the release
    note does not need a technical writer beside them.
  * Every seat costs the PO an approval and a token ceiling of its own, and
    they can approve some and refuse others. A seat you cannot justify on its
    own is a seat that gets refused on its own.
  * Seat 0 is the LEAD and is listed first. The implement ticket goes to the
    lead and nobody else writes on it. The other seats are on the story for the
    work that comes after: the review pass, the follow-up ticket, the second
    story in the same folder. Hiring a specialist parks them on this story so
    the next piece of work has them already contracted.
  * Every seat needs a distinct `role`. Two people cannot hold the same role
    name on one project.
  * Do not pad the crew to look thorough. One right hire beats three defensible
    ones, and the PO reads all of them.

{voice.STYLE}

Reply with ONLY a JSON object. `team` is ordered - first entry is the lead:

{{
  "team": [
    {{
      "roster_slug": "exactly one slug from the roster above",
      "role": "short-kebab-case name for how the colony refers to them here",
      "why": "what in the acceptance criteria this persona is for, under 400 characters",
      "model": "claude-sonnet-5",
      "max_tokens_run": 400000
    }}
  ],
  "finalists": [
    {{"slug": "...", "why_not": "what separated them from your pick"}},
    {{"slug": "...", "why_not": "..."}}
  ],
  "read": ["persona files you actually opened"]
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
    """What the ledger says about this pick, written by Python so the chooser
    does not audit itself.
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


# -- the second opinion -------------------------------------------------------
# `_diversity_note` can only count. For judgement, the PO can ask
# `specialized/agents-orchestrator` to audit a pending pick. It writes one
# paragraph beside the card and cannot hire, reject or raise anything
# (ROSTER.md). Only on the PO's button, since it costs a full roster digest.

SECOND_OPINION_SLUG = "specialized/agents-orchestrator"
SECOND_OPINION_TIMEOUT_S = int(os.environ.get("COLONY_SECOND_OPINION_TIMEOUT", "420"))


def second_opinion_prompt(story: sqlite3.Row, esc: sqlite3.Row, pick: sqlite3.Row,
                          persona_path: str, digest: str) -> str:
    """The work order for auditing one hire that has already been proposed."""
    criteria = (story["acceptance_criteria"] or "").strip() or "(none recorded)"
    brief = (story["description"] or "").strip() or "(the Notion page body is empty)"
    return f"""Read {persona_path} and answer as that persona.

You are being asked for a SECOND OPINION on a hiring decision somebody else has
already made. You are READ-ONLY: Read, Grep and Glob. You are not the Scrum
Master here and you are not hiring anyone. Ordis made this pick; the Product
Owner is about to approve or refuse it; your paragraph is the only other thing
they will have in front of them when they do.

What that means in practice:

  * You cannot hire, reject, or change anything. Nothing you write is executed.
  * Agreeing is a real answer and a common one. Do not manufacture a
    disagreement to look useful. "This is the right pick, and here is the one
    thing I would watch" is worth more than a contrarian alternative.
  * If you do disagree, name a specific slug from the roster below and say what
    that persona would do differently on THIS story. "Consider a specialist" is
    not an answer.

STORY #{story['id']}: {story['title']}
project: {story['project']}   (write scope would be {story['project']}/ and nothing else)

--- brief ---
{brief[:3000]}
--- end brief ---

--- acceptance criteria, already approved by the PO ---
{criteria[:3000]}
--- end criteria ---

--- the proposal you are auditing ---
{esc['reason']}

Ordis's reasoning:
{(esc['recommendation'] or '(none recorded)')[:2000]}

The persona picked: {pick['name']} ({pick['slug']}), division {pick['division']},
hired {pick['times_hired']}x in this colony{f", last {pick['last_hired_at']}" if pick['last_hired_at'] else ""}.
Their file is {PERSONA_ROOT / (pick['slug'] + '.md')}
--- end proposal ---

Read the picked persona's file and enough of
{db.PROJECTS_ROOT / story['project']} to know what the work is. Be frugal - a
few targeted reads. Then read the roster below before you agree, because
agreeing without having looked at the alternatives is not a second opinion.

--- the roster: every persona available, by division ---
{digest}
--- end roster ---

{voice.STYLE}

Reply with ONLY a JSON object:

{{
  "verdict": "agree" | "agree-with-caveat" | "disagree",
  "opinion": "your reasoning, under 900 characters, addressed to the PO",
  "instead": "a slug from the roster, or null if you agree",
  "watch": "the one thing most likely to go wrong with this hire, under 200 characters",
  "read": ["files you actually opened"]
}}"""


def second_opinion(conn: sqlite3.Connection, esc_id: int, terms: dict) -> dict:
    """Audit one pending hire. Decides nothing. Returns a dict the endpoint
    hands back, and writes only `escalations.second_opinion` on that card.
    """
    esc = conn.execute("SELECT * FROM escalations WHERE id = ?", (esc_id,)).fetchone()
    if not esc:
        raise control.Refused("no such escalation")
    if esc["kind"] != "hire":
        raise control.Refused("a second opinion is about a hire. This card is a "
                              f"{esc['kind']}")
    if esc["resolved_at"]:
        raise control.Refused("this hire is already decided. A second opinion now "
                              "would cost a run and change nothing")
    if esc["second_opinion"]:
        raise control.Refused("this hire already has a second opinion. Reading it "
                              "again would cost another full roster digest for the "
                              "same answer")

    story = conn.execute("SELECT * FROM stories WHERE id = ?", (esc["story_id"],)).fetchone()
    if not story:
        raise control.Refused("this hire is not attached to a story, so there is no "
                              "work to judge the pick against")

    proposal = json.loads(esc["proposal"] or "{}")
    pick = conn.execute(
        "SELECT slug, name, division, times_hired, last_hired_at FROM roster WHERE slug = ?",
        (proposal.get("roster_slug"),),
    ).fetchone()
    if not pick:
        raise control.Refused(f"the proposed persona {proposal.get('roster_slug')!r} is "
                              f"no longer in the roster. Refuse this card and let the "
                              f"next pulse propose someone who is")

    persona_file = PERSONA_ROOT / f"{SECOND_OPINION_SLUG}.md"
    if not persona_file.exists():
        raise control.Refused(
            f"{SECOND_OPINION_SLUG} is not on disk at {persona_file}. It ships with the "
            f"agency roster clone. Without the file there is no second opinion to give, "
            f"only a generic one")

    digest = roster_mod.digest(conn)
    prompt = second_opinion_prompt(story, esc, pick, str(persona_file), digest)
    cur = conn.execute(
        """INSERT INTO tickets (story_id, title, intent, role, status, work_order,
                                requires_po)
           VALUES (?,?,'research',?,'staffed',?,0)""",
        (story["id"], f"Second opinion: {esc['reason']}"[:200], terms["role"], prompt),
    )
    ticket_id = cur.lastrowid

    result = agent.run_ticket(
        conn, ticket_id=ticket_id, role=terms["role"], prompt=prompt,
        model=terms["model"], tools_allowed=terms["tools_allowed"],
        tools_denied=terms.get("tools_denied"), cwd=db.PROJECTS_ROOT,
        timeout_s=SECOND_OPINION_TIMEOUT_S, max_tokens=terms.get("max_tokens_run"),
    )
    answer = result.json_payload() if result.status == "ok" else None
    if not answer or not str(answer.get("opinion") or "").strip():
        conn.execute(
            "UPDATE tickets SET status = 'blocked', findings = ?, "
            "closed_at = datetime('now','localtime') WHERE id = ?",
            ((result.error or result.text or "no usable answer")[:2000], ticket_id),
        )
        return {"ok": False, "escalation_id": esc_id, "tokens": result.chargeable_tokens,
                "verdict": "the second opinion came back unreadable. The hire card is "
                           "unchanged and you can ask again"}

    verdict = str(answer.get("verdict") or "").strip().lower()
    if verdict not in ("agree", "agree-with-caveat", "disagree"):
        verdict = "unclear"
    opinion = " ".join(str(answer.get("opinion") or "").split())[:900]
    watch = " ".join(str(answer.get("watch") or "").split())[:200]

    # An alternative only goes on the card if it is real. A slug the auditor
    # invented would read to the PO exactly like one they could act on.
    instead = str(answer.get("instead") or "").strip()
    alt = conn.execute("SELECT name, division FROM roster WHERE slug = ?",
                       (instead,)).fetchone() if instead else None

    text = f"[{verdict}] {opinion}"
    if alt:
        text += f"\n\nWould take instead: {alt['name']} ({instead}, {alt['division']})."
    elif instead:
        text += f"\n\nNamed {instead!r} as an alternative, which is not a slug in the "
        text += "roster. Treat that half of the answer as noise."
    if watch:
        text += f"\n\nWatch: {watch}"

    conn.execute(
        "UPDATE escalations SET second_opinion = ?, "
        "second_opinion_at = datetime('now','localtime') WHERE id = ?",
        (text, esc_id),
    )
    conn.execute(
        "UPDATE tickets SET status = 'done', findings = ?, "
        "closed_at = datetime('now','localtime') WHERE id = ?",
        (f"second opinion on hire #{esc_id}: {verdict}"[:2000], ticket_id),
    )
    conn.execute(
        """INSERT INTO story_events (story_id, kind, summary, detail, ticket_id, tokens)
           VALUES (?,'staffed',?,?,?,?)""",
        (story["id"],
         f"Second opinion on {pick['name']}: {verdict}"[:400],
         text[:2000], ticket_id, result.chargeable_tokens),
    )
    return {"ok": True, "escalation_id": esc_id, "verdict": verdict,
            "second_opinion": text, "tokens": result.chargeable_tokens}


def _crew_from(answer: dict) -> list[dict]:
    """The proposed seats from `team`, or a bare top-level `roster_slug` (the
    older answer shape, still accepted).
    """
    raw = answer.get("team")
    if not isinstance(raw, list) or not raw:
        if answer.get("roster_slug"):
            raw = [answer]
        else:
            return []
    seats: list[dict] = []
    for m in raw:
        if isinstance(m, dict) and str(m.get("roster_slug") or "").strip():
            seats.append(m)
    return seats[:STAFF_TEAM_MAX]


def staff_stories(conn: sqlite3.Connection, terms: dict) -> list[dict]:
    """Propose a crew per ready-but-unstaffed story. Hires nothing. One
    escalation per seat, so the PO can accept some and refuse others.
    """
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
        answer = result.json_payload() if result.status == "ok" else None
        seats = _crew_from(answer or {})
        if not seats:
            conn.execute(
                "UPDATE tickets SET status = 'blocked', findings = ?, "
                "closed_at = datetime('now','localtime') WHERE id = ?",
                ((result.error or result.text or "no usable answer")[:2000], ticket_id),
            )
            out.append({"story_id": story["id"], "tokens": result.chargeable_tokens,
                        "verdict": "no pick returned"})
            continue

        finalists = [f for f in (answer.get("finalists") or []) if isinstance(f, dict)][:3]

        # Resolve every seat first, so invented slugs block one ticket with a
        # list rather than half-filling the Inbox.
        picks: list[tuple[dict, sqlite3.Row]] = []
        bad: list[str] = []
        for member in seats:
            slug = str(member.get("roster_slug")).strip()
            row = conn.execute(
                "SELECT slug, name, division, times_hired, last_hired_at "
                "FROM roster WHERE slug = ?", (slug,),
            ).fetchone()
            if row is None:
                bad.append(slug)
            elif any(r["slug"] == slug for _, r in picks):
                bad.append(f"{slug} (proposed twice)")
            else:
                picks.append((member, row))

        if not picks:
            conn.execute(
                "UPDATE tickets SET status = 'blocked', findings = ?, "
                "closed_at = datetime('now','localtime') WHERE id = ?",
                (f"picked {', '.join(repr(b) for b in bad)}, which is not a slug "
                 f"in the roster", ticket_id),
            )
            out.append({"story_id": story["id"], "tokens": result.chargeable_tokens,
                        "verdict": f"invalid pick {bad[0]!r}"})
            continue

        # The diversity note is about the lead. It is the seat that receives the
        # implement ticket, so it is the one the concentration rule is about.
        note = _diversity_note(conn, picks[0][1], finalists)
        losers = "; ".join(
            f"{f.get('slug')}: {' '.join(str(f.get('why_not') or '').split())[:140]}"
            for f in finalists if f.get("slug")
        )

        # Role names must be unique per project; check the batch against itself
        # and the table now, not at approval.
        claimed: set[str] = set()
        raised: list[dict] = []
        for seat_no, (member, pick) in enumerate(picks):
            role = _role_name(str(member.get("role") or ""), pick["slug"])
            base, n = role, 2
            while role in claimed or conn.execute(
                    "SELECT 1 FROM agents WHERE role = ? AND project IS ?",
                    (role, story["project"])).fetchone():
                role = f"{base}-{n}"[:60]
                n += 1
            claimed.add(role)

            why = " ".join(str(member.get("why") or "").split())[:400]
            if seat_no == 0:
                seat_line = "lead - receives the implement ticket"
            else:
                seat_line = (f"seat {seat_no} - hired onto this story alongside "
                             f"{raised[0]['role']}, does not receive the implement ticket")
            recommendation = f"{why}\n\n{seat_line}."
            if seat_no == 0:
                recommendation += f"\n{note}."
                if losers:
                    recommendation += f"\nAlso considered - {losers}"

            esc_id = control.propose_hire(
                conn, roster_slug=pick["slug"], role=role, project=story["project"],
                reason=control.card_text(recommendation),
                model=str(member.get("model") or terms["model"]),
                write_capable=True,
                max_tokens_run=int(member.get("max_tokens_run") or 400000),
                story_id=story["id"], seat=seat_no,
            )
            raised.append({"escalation_id": esc_id, "slug": pick["slug"],
                           "name": pick["name"], "division": pick["division"],
                           "role": role, "seat": seat_no})

        crew = ", ".join(f"{r['name']} as {r['role']}" for r in raised)
        blocked = f" Ignored {', '.join(bad)} - not in the roster." if bad else ""
        conn.execute(
            "UPDATE tickets SET status = 'done', findings = ?, "
            "closed_at = datetime('now','localtime') WHERE id = ?",
            (f"proposed {crew}. {note}.{blocked}"[:2000], ticket_id),
        )
        conn.execute(
            """INSERT INTO story_events (story_id, kind, summary, detail, ticket_id, tokens)
               VALUES (?,'staffed',?,?,?,?)""",
            (story["id"],
             (f"Ordis proposed {len(raised)} for this story: {crew}" if len(raised) > 1
              else f"Ordis proposed {raised[0]['name']} ({raised[0]['division']}) "
                   f"as {raised[0]['role']}")[:400],
             f"{note}.{blocked}"[:2000], ticket_id, result.chargeable_tokens),
        )
        lead = raised[0]
        out.append({"story_id": story["id"], "escalation_id": lead["escalation_id"],
                    "tokens": result.chargeable_tokens, "slug": lead["slug"],
                    "name": lead["name"], "division": lead["division"],
                    "role": lead["role"], "crew": raised,
                    "verdict": (f"proposed {crew} - waiting on you" if len(raised) > 1
                                else f"proposed {lead['name']} as {lead['role']} "
                                     f"- waiting on you")})
    return out


# Tokens one wake may spend before it stops, whatever the weekly allowance
# says. 0 turns the cap off and leaves only the weekly check.
WAKE_TOKEN_CAP = int(os.environ.get("COLONY_WAKE_TOKEN_CAP", "0"))


def _room(conn: sqlite3.Connection, usage: dict | None, report: dict, after: str) -> bool:
    """Whether the wake may take its next step, recording why not once. Rereads
    the usage cache, which the tray app refreshes mid-wake.
    """
    if report.get("stopped"):
        return False
    fresh = usage_mod.read()
    if not fresh or fresh.get("seven_day") is None:
        fresh = usage
    budget = budget_state(conn, fresh)
    why = None if budget["ok"] else budget["why"]
    if why is None and WAKE_TOKEN_CAP and report["tokens"] >= WAKE_TOKEN_CAP:
        why = f"this wake spent {report['tokens']:,} of its {WAKE_TOKEN_CAP:,} token cap"
    if why is None:
        return True
    report["stopped"] = f"budget reached after {after}: {why}"
    report["skipped"] = report["stopped"]
    return False


def run(conn: sqlite3.Connection, usage: dict | None) -> dict:
    """Do the wake and return what happened. Budget is checked before every
    spending step.
    """
    report = {"groomed": [], "built": [], "answered": [], "forged": [], "candidates": [],
              "staffed": [], "tokens": 0, "skipped": None, "stopped": None}

    if control.is_halted():
        # The tick already refuses to wake under HALT; this is the second
        # check.
        report["skipped"] = "HALT. Dispatch disabled"
        return report

    budget = budget_state(conn, usage)
    if not budget["ok"]:
        report["skipped"] = budget["why"]
        return report

    # Grooming first: it is the cheaper of the two jobs, and a story groomed in
    # this hour can be dispatched before the next one comes round.
    terms = contract(conn, "investigator")
    if terms is None:
        report["skipped"] = "no active investigator contract. Run `python -m colony init`"
    else:
        # Before anything else: whatever the PO said. Working a backlog they have
        # just re-scoped is the most expensive kind of wrong.
        projects = pulse_mod.candidate_projects()
        for outcome in answer_po(conn, terms, projects):
            report["answered"].append(outcome)
            report["tokens"] += outcome["tokens"]

        # Requested skill drafts before grooming: bounded and asked for by
        # hand.
        for skill in forge_mod.pending_drafts(conn)[:FORGE_DRAFT_LIMIT]:
            if not _room(conn, usage, report, "the PO replies"):
                break
            outcome = forge_mod.draft(conn, skill["id"], terms)
            report["forged"].append(outcome)
            report["tokens"] += outcome["tokens"]

    # Read the groom queue after replies, so an answer becomes work this hour.
    stories = stories_to_groom(conn, GROOM_LIMIT) if terms is not None else []
    if terms is not None and stories:
        projects = pulse_mod.candidate_projects()
        for story in stories:
            if not _room(conn, usage, report,
                         f"groom {len(report['groomed'])}" if report["groomed"] else "the replies"):
                break
            outcome = groom_story(conn, story, terms, projects)
            report["groomed"].append(outcome)
            report["tokens"] += outcome["tokens"]

    # Staff after grooming (which creates the need) and before building (which
    # needs an approved hire).
    if terms is not None and _room(conn, usage, report, "grooming"):
        for outcome in staff_stories(conn, terms):
            report["staffed"].append(outcome)
            report["tokens"] += outcome["tokens"]

    # Dispatched builds. `build.pending` only returns confirmed projects.
    for ticket in build_mod.pending(conn, build_mod.BUILD_LIMIT):
        if not _room(conn, usage, report,
                     f"build {len(report['built'])}" if report["built"] else "staffing"):
            break
        outcome = build_mod.run_one(conn, ticket)
        report["built"].append(outcome)
        report["tokens"] += outcome["tokens"]

    if (not report["groomed"] and not report["built"] and not report["answered"]
            and not report["forged"] and not report["staffed"] and not report["skipped"]):
        report["skipped"] = "nothing to groom and nothing dispatched"
    return report
