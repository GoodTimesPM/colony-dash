"""The skill forge: a run that went well becomes a reusable procedure.

    agent runs --> detect --> candidate --> Ordis drafts --> PO promotes --> active
                                                                              |
                                                    retired <-- win rate decays

  * Detection is free: `detect()` is SQL over runs already paid for.
  * Drafting is queued: the button records a request and the next wake
    writes it under the budget guard, since `control.py` never spends tokens.
  * Promotion is a human gate: `promote()` is the only disk write and only a
    PO action reaches it (ARCHITECTURE.md §7, §8.2).

Value is measured in tokens saved, the same unit as the budget.
"""

from __future__ import annotations

import json
import re
import sqlite3
import statistics
from pathlib import Path
from typing import Any

from . import agent, db, voice
from .prompt import render as render_prompt

# At the projects root so any Claude Code session there, Ordis included, can
# load it (§7 step 4).
SKILLS_DIR = db.PROJECTS_ROOT / ".claude" / "skills"

SLUG_OK = re.compile(r"^[a-z0-9][a-z0-9-]{1,60}$")

# Conservative: a false candidate costs the PO's attention, the scarce part.
REPEAT_MIN = 3          # same ticket class solved this many times = a procedure
SHORTCUT_SAMPLE = 4     # this many runs before a "low outlier" means anything
SHORTCUT_RATIO = 0.5    # <= half the class median is a shortcut worth writing down
CORRECTION_MIN = 3      # the PO correcting the same thing: the loudest signal there is

# Retirement. A skill has to have been used enough for a win rate to mean
# something before a bad run is held against it.
RETIRE_SAMPLE = 5
RETIRE_WIN_RATE = 0.5

DRAFT_TIMEOUT_S = 420


# ── reading the ledger ────────────────────────────────────────────────────────


def _slugify(text: str) -> str:
    out = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return out[:60] or "skill"


def _median(values: list[int]) -> int:
    return int(statistics.median(values)) if values else 0


def class_stats(conn: sqlite3.Connection) -> dict[tuple[str, str], dict]:
    """Success rate and typical cost per ticket class, keyed by (intent, role),
    since a skill is a procedure for a kind of work.
    """
    rows = conn.execute(
        """
        SELECT t.intent AS intent, r.agent_role AS role, r.id AS run_id, r.status AS status,
               COALESCE(r.chargeable_tokens, r.total_tokens, 0) AS tokens,
               r.ticket_id AS ticket_id
          FROM runs r JOIN tickets t ON t.id = r.ticket_id
         WHERE r.ended_at IS NOT NULL
         ORDER BY r.id
        """
    ).fetchall()

    out: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = (row["intent"], row["role"])
        stat = out.setdefault(key, {"ok": [], "failed": [], "tokens": [], "tickets": {}})
        # Over-budget runs still answered (§10.2) and are what a skill would
        # help most.
        if row["status"] in ("ok", "over-budget"):
            stat["ok"].append(row["run_id"])
            if row["tokens"]:
                stat["tokens"].append(row["tokens"])
        else:
            stat["failed"].append(row["run_id"])
        stat["tickets"].setdefault(row["ticket_id"], []).append(row)
    for stat in out.values():
        stat["median"] = _median(stat["tokens"])
    return out


def baseline_for(conn: sqlite3.Connection, intent: str, role: str) -> int:
    """What a class costs without a skill. The number savings are measured against."""
    stat = class_stats(conn).get((intent, role))
    return stat["median"] if stat else 0


# ── detection ─────────────────────────────────────────────────────────────────


def _propose(conn: sqlite3.Connection, *, slug: str, name: str, detector: str,
             summary: str, evidence: list[int], baseline: int) -> dict | None:
    """Insert a candidate or add evidence to a waiting one. Slugs in any other
    status are left alone, so a retired skill is not quietly proposed again.
    """
    existing = conn.execute("SELECT * FROM skills WHERE slug = ?", (slug,)).fetchone()
    if existing:
        if existing["status"] != "candidate":
            return None
        conn.execute(
            "UPDATE skills SET evidence_runs = ?, baseline_tokens = ?, summary = ? WHERE id = ?",
            (json.dumps(evidence), baseline, summary, existing["id"]),
        )
        return None

    cur = conn.execute(
        """INSERT INTO skills (name, slug, status, summary, evidence_runs, detector,
                               baseline_tokens)
           VALUES (?,?,'candidate',?,?,?,?)""",
        (name[:120], slug, summary, json.dumps(evidence), detector, baseline),
    )
    return {"id": cur.lastrowid, "slug": slug, "name": name, "detector": detector,
            "summary": summary}


def detect(conn: sqlite3.Connection) -> list[dict]:
    """Scan the ledger for the four signals in §7 step 1. Spends nothing."""
    found: list[dict] = []
    stats = class_stats(conn)

    for (intent, role), stat in sorted(stats.items()):
        ok, median = stat["ok"], stat["median"]

        # 1. The same class of work, solved the same way, three times over.
        if len(ok) >= REPEAT_MIN:
            hit = _propose(
                conn,
                slug=_slugify(f"{intent}-{role}-procedure"),
                name=f"{intent.title()} as {role}",
                detector="repeat",
                summary=(f"{len(ok)} successful {intent} runs by {role}, median "
                         f"{median:,} chargeable tokens. The same steps are being "
                         f"re-derived from scratch every time."),
                evidence=ok[-8:],
                baseline=median,
            )
            if hit:
                found.append(hit)

        # 2. Failed, then succeeded. The recovery path is the lesson. It is the
        #    part no transcript of the successful run alone would ever show.
        for ticket_id, runs in stat["tickets"].items():
            bad = [r for r in runs if r["status"] not in ("ok", "over-budget")]
            good = [r for r in runs if r["status"] in ("ok", "over-budget")]
            if bad and good and max(r["run_id"] for r in good) > min(r["run_id"] for r in bad):
                hit = _propose(
                    conn,
                    slug=_slugify(f"{intent}-{role}-recovery"),
                    name=f"Recovering a failed {intent} run",
                    detector="recovery",
                    summary=(f"Ticket #{ticket_id} failed {len(bad)} time(s) before it "
                             f"succeeded. Whatever the second attempt did differently is "
                             f"the procedure."),
                    evidence=[r["run_id"] for r in runs],
                    baseline=median,
                )
                if hit:
                    found.append(hit)
                break

        # 3. One run came in far under what its class usually costs. It found a
        #    shortcut, and a shortcut nobody wrote down is a shortcut taken once.
        if len(stat["tokens"]) >= SHORTCUT_SAMPLE and median:
            cheap = [r for r, tok in zip(ok, stat["tokens"]) if tok <= median * SHORTCUT_RATIO]
            if cheap:
                cited = ", ".join("#" + str(r) for r in cheap)
                hit = _propose(
                    conn,
                    slug=_slugify(f"{intent}-{role}-shortcut"),
                    name=f"The cheap path through {intent}",
                    detector="shortcut",
                    summary=(f"Run(s) {cited} solved a {intent} at or under "
                             f"{int(median * SHORTCUT_RATIO):,} tokens against a class "
                             f"median of {median:,}."),
                    evidence=cheap + ok[-4:],
                    baseline=median,
                )
                if hit:
                    found.append(hit)

    # 4. The PO correcting the same thing repeatedly: the strongest signal.
    corrections = {
        "reject": "rejecting what the colony proposed",
        "confirm-project": "naming the project folder by hand",
        "defer": "deferring what the colony raised",
    }
    for row in conn.execute(
        """SELECT action, COUNT(*) AS n FROM po_actions
            WHERE action IN ('reject','confirm-project','defer')
            GROUP BY action HAVING n >= ?""",
        (CORRECTION_MIN,),
    ).fetchall():
        verb = corrections[row["action"]]
        hit = _propose(
            conn,
            slug=_slugify(f"po-correction-{row['action']}"),
            name=f"Stop needing the PO: {row['action']}",
            detector="po-correction",
            summary=(f"The PO has spent {row['n']} actions {verb}. That is the colony "
                     f"being wrong in a way a procedure could get right the first time."),
            evidence=[],
            baseline=0,
        )
        if hit:
            found.append(hit)

    return found


# ── drafting (the only part that spends) ──────────────────────────────────────


def pending_drafts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM skills WHERE status = 'candidate' AND draft_requested_at IS NOT NULL "
        "ORDER BY draft_requested_at"
    ).fetchall()


def _evidence_brief(conn: sqlite3.Connection, skill: sqlite3.Row) -> str:
    """What the drafting agent sees: work order, findings and verdict, not raw
    transcripts, which would cost more than the skill saves.
    """
    runs = json.loads(skill["evidence_runs"] or "[]")
    if not runs:
        return "(no run evidence. This candidate came from the PO's own corrections)"

    placeholders = ",".join("?" * len(runs))
    lines: list[str] = []
    for row in conn.execute(
        f"""SELECT r.id, r.status, r.verdict, r.chargeable_tokens, t.title, t.intent,
                   t.work_order, t.findings
              FROM runs r JOIN tickets t ON t.id = r.ticket_id
             WHERE r.id IN ({placeholders}) ORDER BY r.id""",
        runs,
    ).fetchall():
        lines.append(
            f"--- run #{row['id']} ({row['status']}, {row['chargeable_tokens'] or 0:,} tok)\n"
            f"ticket: {row['title']} [{row['intent']}]\n"
            f"work order:\n{(row['work_order'] or '')[:1200]}\n"
            f"findings:\n{(row['findings'] or '')[:800]}\n"
            f"verdict:\n{(row['verdict'] or '')[:800]}"
        )
    return "\n\n".join(lines)


def draft_prompt(conn: sqlite3.Connection, skill: sqlite3.Row) -> str:
    return render_prompt("forge_draft", detector=skill["detector"],
                         summary=skill["summary"],
                         evidence=_evidence_brief(conn, skill), style=voice.STYLE)


def draft(conn: sqlite3.Connection, skill_id: int, terms: dict) -> dict:
    """Spend tokens turning a candidate into a drafted SKILL.md. Writes no files."""
    skill = conn.execute("SELECT * FROM skills WHERE id = ?", (skill_id,)).fetchone()
    if not skill:
        return {"skill_id": skill_id, "verdict": "no such skill", "tokens": 0}

    prompt = draft_prompt(conn, skill)
    cur = conn.execute(
        """INSERT INTO tickets (title, intent, role, status, work_order, requires_po)
           VALUES (?, 'research', ?, 'staffed', ?, 1)""",
        (f"Draft skill: {skill['name']}"[:200], terms["role"], prompt),
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
        timeout_s=DRAFT_TIMEOUT_S,
        max_tokens=terms.get("max_tokens_run"),
    )

    out = {"skill_id": skill_id, "slug": skill["slug"], "name": skill["name"],
           "tokens": result.chargeable_tokens, "raw_tokens": result.total_tokens,
           "verdict": None}

    if result.status != "ok":
        conn.execute("UPDATE tickets SET status='blocked', findings=? WHERE id=?",
                     (result.error or result.status, ticket_id))
        out["verdict"] = f"run {result.status}"
        return out

    answer = result.json_payload()
    if not answer:
        conn.execute("UPDATE tickets SET status='blocked', findings=? WHERE id=?",
                     (result.text[:2000], ticket_id))
        out["verdict"] = "unparseable answer"
        return out

    conn.execute(
        "UPDATE tickets SET status='done', closed_at=datetime('now','localtime'), "
        "findings=? WHERE id=?",
        (result.text[:4000], ticket_id),
    )

    if not answer.get("worth_it", True):
        # The cheapest place to drop a bad candidate: before a file or a PO
        # read.
        why = (answer.get("why_not") or "the evidence held no procedure")[:400]
        conn.execute(
            "UPDATE skills SET status='retired', retired_at=datetime('now','localtime'), "
            "retire_reason=?, draft_requested_at=NULL WHERE id=?",
            (f"declined at draft: {why}", skill_id),
        )
        out["verdict"] = f"declined. {why}"
        return out

    markdown = (answer.get("markdown") or "").strip()
    if not markdown:
        out["verdict"] = "draft came back empty"
        return out

    conn.execute(
        """UPDATE skills SET status='drafted', name=?, trigger_when=?, draft_md=?,
                             draft_requested_at=NULL WHERE id=?""",
        ((answer.get("name") or skill["name"])[:120], (answer.get("trigger") or "")[:400],
         markdown, skill_id),
    )
    out["verdict"] = "drafted"
    return out


# ── promotion (the file-writing gate) ─────────────────────────────────────────


def skill_path(slug: str) -> Path:
    if not SLUG_OK.match(slug or ""):
        raise ValueError(f"unsafe skill slug: {slug!r}")
    return SKILLS_DIR / slug / "SKILL.md"


def write_skill_file(slug: str, body: str) -> Path:
    """The forge's only disk write. The slug is re-validated here, where a
    string becomes a path.
    """
    path = skill_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def attach(conn: sqlite3.Connection, slug: str, roles: list[str]) -> list[str]:
    """Attach the skill to each named contract. 'ordis' has no `agents` row;
    for it, the file under `.claude/skills/` is the attachment.
    """
    touched: list[str] = []
    for role in roles:
        if role == "ordis":
            touched.append("ordis")
            continue
        for row in conn.execute(
            "SELECT id, skills FROM agents WHERE role = ? AND status = 'active'", (role,)
        ).fetchall():
            current = json.loads(row["skills"] or "[]")
            if slug not in current:
                current.append(slug)
                conn.execute("UPDATE agents SET skills = ? WHERE id = ?",
                             (json.dumps(current), row["id"]))
            touched.append(role)
    return touched


# ── measurement ───────────────────────────────────────────────────────────────


def active_for(conn: sqlite3.Connection, role: str) -> list[sqlite3.Row]:
    """Every active skill this role carries, in promotion order."""
    return conn.execute(
        """SELECT * FROM skills
            WHERE status = 'active' AND (roles IS NULL OR roles LIKE ?)
            ORDER BY promoted_at""",
        (f'%"{role}"%',),
    ).fetchall()


def preamble(skills: list[sqlite3.Row]) -> str:
    """What gets prepended to a work order. Empty string when there is nothing."""
    if not skills:
        return ""
    parts = ["The colony has learned these procedures. Follow them where they apply.\n"]
    for row in skills:
        body = (row["draft_md"] or "").strip()
        if not body and row["path"]:
            try:
                body = Path(row["path"]).read_text(encoding="utf-8").strip()
            except OSError:
                body = ""
        parts.append(f"### {row['name']}\n{body or row['summary'] or ''}")
    return "\n\n".join(parts) + "\n\n---\n\n"


def record_uses(conn: sqlite3.Connection, *, skills: list[sqlite3.Row], run_id: int | None,
                tokens: int, ok: bool) -> None:
    """One `skill_uses` row per skill per run, with its baseline. The skill's
    `tokens_saved` is only their sum, and savings can be negative.
    """
    for row in skills:
        baseline = row["baseline_tokens"] or 0
        saved = (baseline - tokens) if baseline else 0
        conn.execute(
            """INSERT INTO skill_uses (skill_id, run_id, verdict, tokens, baseline_tokens, saved)
               VALUES (?,?,?,?,?,?)""",
            (row["id"], run_id, "win" if ok else "loss", tokens, baseline, saved),
        )
        conn.execute(
            """UPDATE skills SET times_used = times_used + 1,
                      wins = wins + ?, losses = losses + ?,
                      tokens_saved = tokens_saved + ?,
                      last_used_at = datetime('now','localtime')
                WHERE id = ?""",
            (1 if ok else 0, 0 if ok else 1, saved, row["id"]),
        )


def decaying(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Active skills whose win rate fell below the bar on a real sample.
    Flagged for the PO, never auto-retired.
    """
    return conn.execute(
        """SELECT *, (wins * 1.0 / NULLIF(wins + losses, 0)) AS win_rate
             FROM skills
            WHERE status = 'active' AND (wins + losses) >= ?
              AND (wins * 1.0 / NULLIF(wins + losses, 0)) < ?""",
        (RETIRE_SAMPLE, RETIRE_WIN_RATE),
    ).fetchall()


def board(conn: sqlite3.Connection) -> dict[str, Any]:
    """What the FORGE panel shows: candidates, drafts, and what the active ones earned."""
    rows = conn.execute(
        """SELECT id, name, slug, status, summary, detector, trigger_when, roles, path,
                  times_used, wins, losses, tokens_saved, baseline_tokens,
                  draft_requested_at, promoted_at, retire_reason,
                  (draft_md IS NOT NULL) AS has_draft,
                  json_array_length(COALESCE(evidence_runs,'[]')) AS evidence
             FROM skills WHERE status != 'retired'
            ORDER BY CASE status WHEN 'drafted' THEN 0 WHEN 'candidate' THEN 1 ELSE 2 END,
                     id DESC"""
    ).fetchall()
    saved = conn.execute("SELECT COALESCE(SUM(saved),0) AS s FROM skill_uses").fetchone()["s"]
    return {"skills": [dict(r) for r in rows], "tokens_saved": int(saved),
            "decaying": [r["slug"] for r in decaying(conn)]}
