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
import sqlite3

from . import agent, build as build_mod, control, db, pulse as pulse_mod

# How many stories one wake may groom. A wake is coalescing — an hour with six
# new stories is one wake — so this is the throttle that keeps a bulk Notion
# import from becoming a bulk token spend.
GROOM_LIMIT = int(os.environ.get("COLONY_GROOM_LIMIT", "2"))

GROOM_TIMEOUT_S = int(os.environ.get("COLONY_GROOM_TIMEOUT", "420"))


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
        boosted = f" (35% + {band['boost']:.0f} boost)" if band["boost"] else ""
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

GROOMABLE_WHERE = """
    status IN ('backlog','needs-criteria')
    AND (acceptance_criteria IS NULL OR acceptance_criteria = '')
    AND (SELECT COUNT(*) FROM tickets t
          WHERE t.story_id = stories.id AND t.title LIKE 'Groom:%') < :max_attempts
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


def groom_prompt(story: sqlite3.Row, projects: list[str]) -> str:
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


def _event(conn, story_id, kind, summary, detail=None, ticket_id=None, tokens=0):
    conn.execute(
        """INSERT INTO story_events (story_id, ticket_id, kind, summary, detail, tokens)
           VALUES (?,?,?,?,?,?)""",
        (story_id, ticket_id, kind, summary[:400], detail, tokens),
    )


def groom_story(conn: sqlite3.Connection, story: sqlite3.Row, terms: dict,
                projects: list[str]) -> dict:
    """One story, one spawned agent, one outcome recorded."""
    prompt = groom_prompt(story, projects)
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
        # Gate 1. Ordis drafts; only the PO marks a story ready.
        conn.execute(
            """INSERT INTO escalations (story_id, ticket_id, kind, reason, recommendation, est_tokens)
               VALUES (?,?,'decision',?,?,?)""",
            (story["id"], ticket_id,
             f'"{story["title"]}" has draft acceptance criteria awaiting your approval.',
             criteria[:1000], result.chargeable_tokens),
        )
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
            (missing[:1000], story["id"]),
        )
        already = conn.execute(
            "SELECT 1 FROM escalations WHERE story_id = ? AND kind = 'needs-info' "
            "AND resolved_at IS NULL",
            (story["id"],),
        ).fetchone()
        if not already:
            conn.execute(
                """INSERT INTO escalations (story_id, ticket_id, kind, reason, recommendation, est_tokens)
                   VALUES (?,?,'needs-info',?,?,?)""",
                (story["id"], ticket_id, f'"{story["title"]}" cannot start yet.',
                 missing[:1000], result.chargeable_tokens),
            )
        _event(conn, story["id"], "blocked", missing[:400], None, ticket_id, result.chargeable_tokens)
        outcome["verdict"] = "needs info"

    if answer.get("learned"):
        _event(conn, story["id"], "learning", answer["learned"][:400], None, ticket_id, 0)

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
                 projects: list[str], history: list[dict]) -> str:
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
        msg["body"][:6000],
        "--- end ---",
        "",
        "Project folders that exist under D:\\ALL STUFF\\PROJECTS:",
        *(f"  {p}" for p in projects),
        "",
        "You may read files under D:\\ALL STUFF\\PROJECTS to check anything he",
        "refers to. Be frugal — a few targeted reads, not a survey.",
        "",
        "Answer him. Plainly, in your own voice, in a few sentences. If he told you",
        "something that changes what you recommended, say what changes. If he asked",
        "for something you cannot do, say so and say what you can do instead. Never",
        "decide the item yourself: approving, rejecting and confirming a project are",
        "his, and this reply does none of them.",
        "",
        "Reply with ONLY a JSON object:",
        "",
        "{",
        '  "answer": "what you are saying back to Jordan, under 1200 characters",',
        '  "project": "a folder from the list if his message settled which one, else null",',
        '  "new_project": "a folder name he asked you to treat as new work, else null",',
        '  "recommendation": "a revised one-line recommendation for the Inbox tile, or null",',
        '  "resolved": true only if his message means this item no longer needs him,',
        '  "learned": "one durable thing worth keeping, or null"',
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

        prompt = reply_prompt(msg, esc, story, projects, history)
        cur = conn.execute(
            """INSERT INTO tickets (story_id, title, intent, role, status, work_order, requires_po)
               VALUES (?,?,'research',?,'staffed',?,0)""",
            (msg["story_id"], f"Answer the PO: {msg['body'][:120]}", terms["role"], prompt),
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
        # with. The escalation itself stays open unless Ordis is confident the
        # question is gone — and even then it is closed as 'amend', never as an
        # approval nobody gave.
        if esc is not None:
            if answer.get("recommendation"):
                conn.execute("UPDATE escalations SET recommendation = ? WHERE id = ?",
                             (str(answer["recommendation"])[:1000], esc["id"]))
            if answer.get("resolved"):
                conn.execute(
                    "UPDATE escalations SET resolved_at = datetime('now','localtime'), "
                    "po_decision = 'amend' WHERE id = ? AND resolved_at IS NULL",
                    (esc["id"],),
                )

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
                _event(conn, msg["story_id"], "learning", str(answer["learned"])[:400],
                       None, ticket_id, 0)

        out.append({"message_id": msg["id"], "tokens": result.chargeable_tokens,
                    "verdict": "answered", "answer": text[:200]})
    return out


def unanswered_count(conn: sqlite3.Connection) -> int:
    """How many PO replies are waiting — the tick uses this to decide to wake."""
    return conn.execute(
        "SELECT COUNT(*) n FROM po_messages WHERE author = 'po' AND status = 'unread'"
    ).fetchone()["n"]


def run(conn: sqlite3.Connection, usage: dict | None) -> dict:
    """Do the wake. Returns what happened, for the pulse row and the printout."""
    report = {"groomed": [], "built": [], "answered": [], "tokens": 0, "skipped": None}

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
    stories = stories_to_groom(conn, GROOM_LIMIT) if terms else []
    if terms is None:
        report["skipped"] = "no active investigator contract — run `python -m colony init`"
    else:
        # Before anything else: whatever the PO said. Working a backlog he has
        # just re-scoped is the most expensive kind of wrong.
        projects = pulse_mod.candidate_projects()
        for outcome in answer_po(conn, terms, projects):
            report["answered"].append(outcome)
            report["tokens"] += outcome["tokens"]

    if terms is not None and stories:
        projects = pulse_mod.candidate_projects()
        for story in stories:
            outcome = groom_story(conn, story, terms, projects)
            report["groomed"].append(outcome)
            report["tokens"] += outcome["tokens"]

    # Then whatever the PO dispatched. `build.pending` is already narrowed to
    # tickets on a *confirmed* project, so nothing reaches a worktree on the
    # strength of an inference.
    for outcome in build_mod.run(conn):
        report["built"].append(outcome)
        report["tokens"] += outcome["tokens"]

    if (not report["groomed"] and not report["built"] and not report["answered"]
            and not report["skipped"]):
        report["skipped"] = "nothing to groom and nothing dispatched"
    return report
