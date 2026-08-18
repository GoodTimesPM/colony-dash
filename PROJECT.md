# Colony Dash — Master Memory

**Status:** **M0 shipped, M1 mostly shipped.** Priority: **High.**
**Created:** 2026-08-15 (as "PO Dashboard"; renamed **Colony Dash** 2026-08-17)

The orchestrator loop and dashboard where Jordan acts as Product Owner over a colony of
Claude agents run by Ordis as Scrum Master. Intended to become the **main dashboard for
Jordan's integration with Claude as a whole** — not a side tool.

- `ARCHITECTURE.md` — the full backbone: state model, pulse mechanics, budget, dashboard, forge.
- `ROSTER.md` — where agents come from (agency-agents) and how they get hired.
- `colony/` — the code. `python -m colony status` is the dashboard until M2 exists.
- This file — status and decisions only.

## Running it

```
python -m colony init                 create + seed the ledger, scan the roster
python -m colony status               sprint, board, colony, inbox, pulse log
python -m colony pulse                one heartbeat — zero tokens
python -m colony roster "database"    search all 270 personas
python -m colony agents               who is on the books and what they may touch
python -m colony sql "SELECT ..."     SELECT-only console
python -m colony mirror               full refresh into MySQL (needs .env + PyMySQL)
pulse.cmd                             what the scheduled task runs; logs to .colony/pulse.log
```

Ledger: `colony-dash/.colony/ledger.db` (gitignored). Config: `colony-dash/.env` (gitignored)
holds `NOTION_TOKEN`, `NOTION_DATABASE_ID`, and `COLONY_MYSQL_*`.

## What this is

The in-practice execution of the Notion idea **[Mimic Scrum Environment](https://app.notion.com/p/3a6e98f012738047bccdf1671a127948)**
(Priority: High, Status: In Progress). That page holds the *why*; this folder holds the build.

- Jordan = **Product Owner** — owns backlog, priority, approvals.
- Ordis = **Scrum Master** — the heartbeat loop that grooms, staffs, dispatches, reports.
- The colony = **specialist agents** — hired from the roster, spawned per ticket, stateless,
  they die after one run.
- SQLite ledger = the continuity. Notion = where intent is planned.

The name: the agents are a colony with different functions, and the skill **forge** is where
what they learn gets made permanent.

## Reference studied

**Lloyd**, from u/croovies' r/ClaudeAI post "Example of a real working loop orchestrator"
(431 upvotes) — [analysis](https://explainx.ai/blog/claude-code-loop-orchestrator-heartbeat-ticket-memory-august-2026).
Runs on scape.work (Mac-only), so we rebuild the pattern rather than adopt the product.

Four things taken from it: heartbeat pulses that log even when clean; SQLite as the memory
instead of the context window; read-only investigation before any write; escalation as a
proposal, not a decision. The Colony panel is a direct descendant of Lloyd's Sessions panel.

## Decisions locked

### 2026-08-15
- **Hermes is parked.** Claude Code alone drives this. Revisit only if always-on delivery
  becomes the bottleneck.
- **Skills are a first-class subsystem**, not a byproduct. Learned procedure is mined from
  agent runs, drafted into real `SKILL.md` files, and promoted back to the colony *and to
  Ordis*. Memory files hold facts; skills hold procedure. Do not conflate them.
- **Dashboard is a local read-only web app**, not a TUI. The only write control is PO
  approval in the Inbox.
- **Three human gates:** grooming, writing, skill promotion. Everything between is autonomous.
- **Write-capable runs execute in a git worktree.** The patch is what surfaces for approval.

### 2026-08-17 — the PO answered the four open questions
- **Renamed PO Dashboard → Colony Dash.**
- **Cadence: hourly**, resolved with a **two-tier pulse**. A "tick" is pure Python and costs
  **zero tokens**; it only wakes Ordis when something actually changed. Idle hours are free,
  which is what makes hourly affordable. (`ARCHITECTURE.md` §4.0)
- **Budget unit is TOKENS, not dollars.** The USD figure still renders, grayed, beside it.
  On a Pro plan nothing is metered in money, so tokens are the only honest unit.
- **Sprint budget = the 7-day usage window.** Sourced from the *existing*
  `personal-desktop-projects/token-usage-in-tray` app, which already reads
  `api.anthropic.com/api/oauth/usage`. Colony default: **35% of the week**, leaving the rest
  for interactive work. Amber at 30%, dispatch stops at 35%.
- **One usage poller, not two.** That endpoint allows ~5 req/5 min per account and Claude
  Code shares the bucket. The tray app stays the only poller and writes a shared cache file;
  Colony Dash reads the file. (`ARCHITECTURE.md` §6.2)
- **Blast radius: all of `D:\ALL STUFF\PROJECTS`, read.** Write is only ever the one project
  folder named by the active ticket, in a worktree, after approval. Never `.env`, never
  `.git/` internals, never outside the directory, never `push`/`amend`/force-push.
  **Priority weight to `job-search/` and the Job Radar pipeline.**
- **Notion is the intake.** The [Project Ideas/To-Do](https://app.notion.com/p/1d23280aadd341cbbd1467c771ee6d88)
  board is where the PO posts intent from anywhere. **`Status = "In Progress"` is the trigger
  to work on a row**; the page body is the brief. Thin briefs go `needs-info` and raise an
  Inbox card + a Notion comment naming the specific missing decision — the colony never
  guesses. (`ARCHITECTURE.md` §5)
- **The escalation bar is approved as written** (`ARCHITECTURE.md` §4.6), to be tuned against
  real pulse logs.
- **agency-agents is a hiring pool, not a fleet.** 255 personas at `C:\Users\jtbal\.agency-agents`.
  None of them carry `tools:` or `model:` frontmatter, so they supply persona and zero
  governance. Do **not** run `install.sh --tool claude-code` unfiltered. Hiring is a PO
  action. (`ROSTER.md`)
- **SQLite stays**, over the local MySQL server: no service that can be down when the pulse
  fires, the ledger travels inside the project folder, one writer only, same dialect the
  Notion MCP already speaks. Optional nightly mirror into MySQL for reporting/practice if
  wanted. (`ARCHITECTURE.md` §3.3)
- **Build the dashboard, don't adopt one.** FastAPI + SSE + one HTML page, wrapped in
  **pywebview** so it's a real desktop window. Grafana/Metabase can't do the approval gate;
  Retool/Appsmith still need our backend and add a hosting dependency; Streamlit is the
  fallback fast path. The published artifact already *is* the front end. (`ARCHITECTURE.md` §9.1)

### 2026-08-17 (later) — M0 built, three questions closed
- **No `Project` property on the Notion board.** The board stays a plain idea board; the
  colony adapts. Ordis infers the folder by matching story text against the real directory
  tree two levels deep (`job-search/job-radar`, not `job-search`) and confirms once via the
  Inbox. **An inference never earns write scope** — `project_source` must be `confirmed`
  first. (`ARCHITECTURE.md` §5.2)
- **MySQL mirror: yes, and built.** `python -m colony mirror` full-refreshes the ledger into
  MySQL for reporting and SQL practice. One-way; SQLite stays the system of record; nothing
  in the colony reads back. (`ARCHITECTURE.md` §3.4)
- **Hiring is per need, per project.** No standing team. `agents` is keyed
  `UNIQUE (role, project)` so the same persona can hold different contracts on different
  projects. Nobody is pre-hired. (`ROSTER.md` §4.1)
- **Roster is 270 personas, not 255.** The earlier count missed `game-development/`'s five
  engine subfolders. `python -m colony roster` is the authority now.
- **Four UI features taken from the Lloyd sidebar:** per-agent avatars (deterministic pixel
  sprite from `agents.avatar_seed`, tinted with the persona's own color, emoji as badge),
  an active/standby colony rail whose progress bar is tokens-against-ceiling, a searchable
  standby browser over all 270 personas grouped by division, an artifacts panel (Lloyd's
  file manager, ours being run patches and reports), and click-through story detail built
  from the `story_events` timeline. All are backed by tables that now exist.
  (`ARCHITECTURE.md` §9.3)
- **The tray app now publishes the usage cache** to `%LOCALAPPDATA%\claude-usage\usage.json`
  (atomic replace, best-effort). It must be **restarted once** for this to take effect.

## Build order

**M0 Ledger ✅** → **M1 Pulse ◐** → M2 Dashboard (read view) → M3 Hiring + gates →
M4 Skill forge → M5 Two-way Notion. Details in `ARCHITECTURE.md` §10.

**M0 is done and verified:** 11 tables plus an FTS5 roster index, hash-checked append-only
migrations, structural agents seeded, 270 personas scanned, the CLI, and the MySQL mirror.

**M1 is done except dispatch:** the tick runs, samples usage, syncs Notion, infers projects,
raises needs-info escalations, writes the `story_events` timeline, and decides tick-vs-wake.
Every pulse so far has cost **0 tokens**. The wake tier records its reasons but does not yet
spawn Ordis or an investigator — held deliberately until a week of logs calibrates the bar.

## To finish M1

- [x] Notion integration connected 2026-08-17. The board reads: **6 rows, 6 stories** —
      3 backlog (In Progress), 3 needs-criteria (Exploring), 5 Inbox cards awaiting a project.
- [x] Tray app restarted 2026-08-17 21:10 — `usage.json` is publishing; the pulse now reads
      `5h 32% · 7d 18%` from it.
- [x] Hourly Task Scheduler job **"Colony Dash Pulse"** registered 2026-08-17. Runs
      `pulse.cmd` (interactive user, on battery too, `StartWhenAvailable` so a missed hour
      fires on wake), appending to `.colony/pulse.log`. Verified: exit code 0.
- [ ] Run a week, then tune the escalation bar (§4.6) against real logs.
- [ ] Then: wake-tier dispatch (Ordis + investigator), which is where the first tokens go.

### Bug fixed 2026-08-17: inference matched source folders, not projects

First contact with the real board sent "Run Hermes Agent alongside Claude Code" to
`balatro-mod-loader/build` and "Mimic Scrum Environment" to `balatro-mod-loader/data`. Two
causes, both now closed:

1. **Candidate list was every child directory.** `build/` and `data/` are source folders, and
   generic names collide with ordinary English forever. `candidate_projects()` now counts a
   folder as a project only if it holds a **`PROJECT.md`** — the master CLAUDE.md's own
   definition. 15 real candidates, no blacklist to maintain.
2. **Soft match accepted a subset of the name's words.** It dropped words ≤3 chars, so
   `job-search` could match on the bare word "search" — which is how a Hermes story landed in
   job-search on the second attempt. It now requires *every* word of the name. A single-word
   project can't reach that branch anyway, since the phrase pass already caught it.

Result: all five known-good titles still resolve confidently
(`job-search/job-radar`, `resume-engine`, `token-usage-in-tray`, `terraria-sequel`,
`assisted-apply`), and the five genuinely ambiguous board rows return **no match** and raise
an Inbox card instead of inventing a folder. A wrong project is worse than no project — it is
the value that would later authorize a write.

### Bug fixed 2026-08-17: `--dry-run` wasn't dry

The dry run ingested all six stories, so the next *real* pulse reported "0 new" and hid them.
The connection runs in autocommit, and only the `pulses` insert was behind the `dry_run`
guard — `sample_usage()` and `sync_notion()` had already written. The pulse is now a single
explicit transaction that `run()` rolls back when dry.

### Bug fixed 2026-08-17: a clean tick "failed" because of its own output

The first scheduled run exited 1 with `UnicodeEncodeError: '→'`. Redirected stdout on
Windows is cp1252, which cannot encode the `→` and `·` the pulse prints. The ledger row had
already been committed — only the printing died — so the loop was healthy and the exit code
said otherwise. `cli.main()` now calls `_force_utf8()` before anything else. Formatting must
never decide whether a run succeeded.

## Why it matters beyond the tooling

From the Notion idea: it is a portfolio piece (a working multi-role agent ecosystem), it
builds real scrum fluency from the PO seat rather than the team seat, it maps cleanly to IAM
thinking (what permissions each role needs — see `ROSTER.md` §2, which is that argument in
miniature), and every skill the forge promotes makes the next sprint cheaper than the last.
