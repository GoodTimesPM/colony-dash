# Colony Dash — Master Memory

**Status:** **M0 → M3 shipped.** The loop is live and grooming on the hour, there is a window
to watch it in, and as of 2026-08-18 that window can also *act*: hire, dispatch, approve a
patch, halt everything. Priority: **High.**
**Created:** 2026-08-15 (as "PO Dashboard"; renamed **Colony Dash** 2026-08-17)

The orchestrator loop and dashboard where Jordan acts as Product Owner over a colony of
Claude agents run by Ordis as Scrum Master. Intended to become the **main dashboard for
Jordan's integration with Claude as a whole** — not a side tool.

- `ARCHITECTURE.md` — the full backbone: state model, pulse mechanics, budget, dashboard, forge.
- `ROSTER.md` — where agents come from (agency-agents) and how they get hired.
- `colony/` — the code. `python -m colony dash` opens the window; `status` is the text view.
- This file — status and decisions only.

## Running it

```
python -m colony init                 create + seed the ledger, scan the roster
python -m colony dash                 open the dashboard window (pywebview)
python -m colony dash --serve         serve only, no window — browse 127.0.0.1:8787
python -m colony status               sprint, board, colony, inbox, pulse log
python -m colony pulse                one heartbeat — free unless it wakes to groom
python -m colony pulse --no-wake      tick only, guaranteed zero tokens
python -m colony pulse --dry-run      preview; writes nothing, spends nothing
python -m colony roster "database"    search all 270 personas
python -m colony agents               who is on the books and what they may touch
python -m colony sql "SELECT ..."     SELECT-only console
python -m colony mirror               full refresh into MySQL (needs .env + PyMySQL)
python -m colony halt "reason"        stop all spending now; resume lifts it
python -m colony allowance 10         +10 points of week for a high-volume sprint (0 clears)
python -m colony projects             what moved across the whole tree (--diff PROJECT)
python -m colony shortcut             (re)write the desktop shortcut + icon
python -m colony schedule             (re)install the hourly pulse task (--show, --remove)
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

**M0 Ledger ✅** → **M1 Pulse ✅** → **M2 Dashboard ✅** → **M3 Hiring + gates ✅** →
**M4 Skill forge ✅** → M5 Two-way Notion. Details in `ARCHITECTURE.md` §10.

**M0 is done and verified:** 11 tables plus an FTS5 roster index, hash-checked append-only
migrations, structural agents seeded, 270 personas scanned, the CLI, and the MySQL mirror.

**M1 is done.** The tick samples usage, syncs Notion, infers projects, writes the
`story_events` timeline and decides tick-vs-wake — all at **0 tokens**. The wake tier now
spawns a real read-only agent to groom a story: it either drafts acceptance criteria and
parks the story at `po-review` for your approval, or names the one decision only you can
make. Guards: HALT, the 35% weekly allowance, a per-run ceiling, 2 stories per wake, and a
2-attempt cap per story. `--no-wake` forces a free tick; `--dry-run` writes nothing.

**Live cost, measured:** one grooming run ≈ **50k chargeable tokens ≈ $0.50**. See
`ARCHITECTURE.md` §10.2 — the first wake found a genuine bug in `job-radar/score.py`
unprompted, and cost about half a dollar to do it.

**M2 is done.** `python -m colony dash` opens a real desktop window (pywebview over a
localhost-only FastAPI server) with every panel from `ARCHITECTURE.md` §9.2: the sprint band,
the colony rail with deterministic avatars, the board with click-through story drawers built
from `story_events`, the PO Inbox, the pulse log, the forge, and spend. Live updates arrive
by SSE. It is **read-only by construction** — every request opens the ledger with
`read_only=True`, so the window cannot be the reason state changed. The approve buttons came
with the M3 gate.

**M3 is done.** The dashboard can now change state — through exactly one module. Six gates,
four of them human: groomed → PO accepts the criteria → project confirmed → agent hired with
a write scope → PO dispatches → build runs in a worktree → **PO approves the patch**. The
write contract grants Edit/Write inside one throwaway worktree and **denies Bash outright**,
which is what makes "the colony cannot push" a capability statement rather than a promise.
Approved work lands **uncommitted** in the real folder — Jordan reads the diff in the drawer
and commits it himself. Nothing in `control.py` spends tokens: approving marks a story
dispatchable, and the next wake decides, so a mis-click is free. Every write records a
`po_actions` row in the same transaction as the effect it authorises.

Also shipped with M3, from the same session's asks:

- **HALT and the allowance boost** as one-click macros in a new side rail. HALT writes both
  `.colony/HALT` and a `controls` row, never stops the heartbeat (it keeps logging, syncing,
  reaping), and honestly promises **"no new work"** rather than implying it can kill a run
  in flight. The boost is stored separately from the sprint baseline and capped at
  **+25 points**; clearing it is the same call with 0.
- **The file-manager view.** `projects.py` runs one `git status` for the whole tree and
  buckets it by longest path prefix; the Projects panel lists what moved and a drawer shows
  the real diff. The pulse logs **movement, not dirtiness** — only folders whose counts
  differ from the last sample or that have commits in the window, because a log that repeats
  "75 untracked" every hour is a log nobody reads.
- **A broadened pulse log.** Each beat now carries a `detail` blob, and clicking a beat opens
  what actually happened that hour.
- **Ordis has a panel.** Beats, wakes, tokens spent, anomalies, what's groomable, what's
  queued, what's running, and the last thing it said — the loop is now a visible member of
  the colony instead of an invisible narrator.
- **Standby is browsable.** 270 personas as collapsible division dropdowns, and clicking one
  opens the **actual persona file from disk** — description, identity, mission, and every
  critical rule, parsed into sections. Read on click, never carried in the snapshot: what you
  see is what the agent gets handed, not a copy that drifted.
- **Its own face.** `icon.py` draws a lit longhouse with Pillow at nine sizes (no more
  sharing the Balatro mod manager's Python icon), `shortcut.py` writes a desktop `.lnk`
  pointing at `pythonw.exe`, and the window sets its taskbar icon via `WM_SETICON` because
  pywebview's `icon=` is GTK/Qt-only.
- **Eight themes** in a header dropdown — system, light, dark, ember, moss, slate, parchment,
  clay — persisted in localStorage.
- **The PO Inbox is a full-width tile strip**, and each tile now offers only the affordances
  its kind can actually use: a `needs-info` card asks for a folder and *doesn't* show
  approve/reject, because naming the folder is the only answer it has.

What guards the write door: every `/api/act/*` POST must carry an `X-Colony: 1` header (a
cross-origin form can POST to localhost but cannot set a custom header), the server binds
127.0.0.1 only, and `control.Refused` maps to **409 with the message intact** — every refusal
names the state and the next move.

## Next

- [ ] Answer the Inbox. Five stories are parked on "which project folder?" and
      "15 Part Job Search" is parked on a real decision: should the manual *Job & Internship
      Tracker* and the auto-written *Job Radar Tracker* merge, coexist with a defined
      handoff, or one retire?
- [ ] Let it run a week, then tune the escalation bar (§4.6) against real logs.
- [ ] **Promote the first real skill.** Three candidates are waiting in the FORGE panel;
      none has been drafted yet, because drafting costs tokens and that is the PO's call.
- [ ] **First live write-capable ticket.** M3 is verified against a copy of the ledger; it
      has not yet been pointed at a real story end-to-end.
- [ ] Update the published artifact — it still shows the pre-M1 design.

## Finished 2026-08-18 — M4, the skill forge

The compounding loop. A memory is a fact; a skill is a procedure — the forge only makes the
second kind, and only when the ledger already proves the procedure exists.

- [x] **Detection is free, so it runs on every tick.** `forge.detect()` is pure SQL over runs
      already paid for, so it lives in the free tier rather than the wake: a HALTed colony
      should still notice that a procedure is emerging, because noticing is not doing. Four
      signals — the same ticket class solved 3+ times, a ticket that failed and then succeeded
      (the recovery path *is* the lesson), a run that came in at or under half its class
      median, and the PO correcting the same kind of thing 3+ times. A ticket class is
      (intent, role), not title: a skill is a procedure for a kind of work.
- [x] **It found three real ones on the first pass** against the live ledger:
      `research-investigator-procedure` (5 clean runs, 31,944-token median),
      `research-investigator-shortcut` (run #10 came in under half that), and
      `po-correction-defer` (double-digit defers — the colony keeps raising something the PO keeps
      putting off, which is a procedure problem).
- [x] **Drafting is queued, not clicked.** `control.py` never spends tokens, so "ask for a
      draft" writes `draft_requested_at` and the next wake pays, behind the same budget guard
      as grooming. The draft may come back `worth_it: false`, which retires the candidate with
      its reason — the cheapest place a bad idea can die is before a file exists.
- [x] **Promotion is the third human gate**, and the only path in the whole system that writes
      a file to disk: `PROJECTS/.claude/skills/<slug>/SKILL.md`, plus an attachment to named
      roles. `ordis` is a legal role with no `agents` row — that is the "learned from the
      colony, handed to the Scrum Master" path. The drawer shows the entire draft before the
      button, because approving a procedure you have not read is what a gate exists to prevent.
- [x] **Measured in the budget's own currency.** `skill_uses` records one row per run that
      loaded a skill, with the baseline it was judged against, so `tokens_saved` can always be
      taken apart into the runs behind it. Savings may be negative. A skill loaded into a run
      that then failed counts as a loss, recorded before the early returns.
- [x] **Decay is flagged, never auto-retired.** Active skills under a 50% win rate on 5+ uses
      surface as `decaying`; the PO retires them. Retiring detaches the slug from every
      contract and keeps the row, because the long-run question is which *detector* keeps
      proposing failures.
- [x] `python -m colony forge --detect / --draft ID`, four gated API routes, and a real FORGE
      panel: status-coloured cards, the detector that proposed each one in plain words
      ("seen 3+ times", "you kept fixing it"), and what the active ones have earned.

Verified end-to-end on a **copy** of the ledger with the skills directory redirected into a
scratch tree: promote wrote the file and attached to `ordis` + `investigator`, the preamble
reached the work order, one run recorded **19,944 tokens saved** against a 31,944 baseline,
retire detached the slug everywhere, and `skill_path("../../evil")` raised. On the live
server: no `X-Colony` header → 403; promoting a candidate → 409 *"only a drafted skill can be
promoted"*; queueing the same draft twice → 409. Test residue was cleared from the live ledger
afterwards, so no wake spends tokens on a request the PO did not make.

## Finished 2026-08-18 — M3.1, the PO's own quality-of-life pass

Ten things Jordan asked for after living with M3 for a day. All shipped.

- [x] **No console windows, ever.** Two separate bugs wearing one costume. The flashing
      2-3x/minute was `PROJECT_TTL_S = 30.0` in `server.py`: every 30s the SSE snapshot
      re-ran `projects.scan()`, which shells out to `git` from a GUI process. The hourly
      window that never closed was the scheduled task running `cmd.exe`. The fix needs
      **both halves**: `colony/proc.py` wraps every subprocess in `CREATE_NO_WINDOW` +
      a hidden `STARTUPINFO` (kills the children — git, the claude CLI, WScript.Shell),
      and `colony/schedule.py` reinstalls the task under **pythonw.exe** (kills the
      parent). Suppressing one alone leaves the other on screen. `python -m colony
      schedule --show` reports `windowless` when both halves hold.
- [x] **Reply to Ordis.** Every Inbox card has a `REPLY TO ORDIS` button that opens a
      screen-wide composer with the proposal pinned above the box — the same thing as
      typing into Claude Code, but from the tile. Migration `005_po_replies.sql` adds
      `po_messages`; `control.reply()` queues a row, **spends nothing**, and leaves the
      escalation open. The next wake reads it (`wake.answer_po`, before grooming) and
      answers. A reply is not a decision: Ordis may *suggest* a project from one but never
      confirm it — `project_source` stays `inferred`, so a reply can never authorize a
      write (§8.2). An unanswered message stays `unread` so the question survives.
- [x] **New folders from the Inbox.** The project picker now carries a `+ new project
      folder...` option. `control.create_project()` whitelists each path segment
      (fails closed) *and* re-checks the resolved path. This was the "Full computer scan
      (Optimization)" case — no existing folder fit, and the dropdown had no answer.
- [x] **Dropdowns fit their tile** at any window size (`select.pick { max-width:100%;
      min-width:0 }` — the missing `min-width` was what let the grid child overflow).
- [x] **"Later" now moves something.** It writes `snoozed_until` (8h default); the tile
      dims and sorts last (`ORDER BY snoozed, ...`). Un-snooze is the same call with
      `snooze_hours: 0` — note `or 8` is wrong there, 0 is falsy. "story" is now
      `OPEN STORY`, which is all it ever did.
- [x] **The dark themes actually apply.** Pure CSS specificity: the system-dark guard
      `:root:not([data-theme="light"]):not([data-theme="parchment"])...` scored 0,4,0 and
      beat `:root[data-theme="ember"]` at 0,2,0, so on a dark OS every named dark theme
      lost to system-dark. The guard is now `:root:not([data-theme])` — a chosen theme is
      never "system", so the *absence* of the attribute is the whole condition.
- [x] **Parchment re-saturated** into real sepia (`--ground:#E4D5B7`, panels tinted rather
      than white-with-a-hint, accents as pigments: ochre / verdigris / vermilion).
      A sepia theme whose panel is `#FBF6EB` is a white theme standing near a candle.
- [x] **A real file tree** in the middle column. `scan()` answers *what moved*; `tree()`
      answers *what is there* — a panel built on `scan()` looks empty exactly when the
      tree is tidy, which is why the old one looked unimplemented. Lazy, one directory per
      request (the root has ~60 projects, some with `node_modules`). `safe_path()`
      validates the **resolved** path, so a symlink out of the tree fails like a `..`, and
      rejects hidden names and anything matching `is_secret()` (`.env*`, `credentials.json`,
      `id_rsa`, ...). Verified: `/api/tree?path=../../` -> 400, `/api/file?path=.../.env` -> 400.
- [x] `?theme=<name>` forces a theme for one load without touching localStorage.
- [x] `pulse.cmd` deleted — the task no longer references it.

## Finished 2026-08-17

- [x] Notion integration connected 2026-08-17. The board reads: **6 rows, 6 stories** —
      3 backlog (In Progress), 3 needs-criteria (Exploring), 5 Inbox cards awaiting a project.
- [x] Tray app restarted 2026-08-17 21:10 — `usage.json` is publishing; the pulse now reads
      `5h 32% · 7d 18%` from it.
- [x] Hourly Task Scheduler job **"Colony Dash Pulse"** registered 2026-08-17. Runs
      `pythonw.exe -m colony pulse` (interactive user, on battery too, `StartWhenAvailable` so a missed hour
      fires on wake), appending to `.colony/pulse.log`. Verified: exit code 0.
- [x] Wake-tier dispatch built and run live 2026-08-17. Four bugs it exposed — the token
      ceiling counting cache reads, a budget breach discarding paid-for work, the wake never
      firing for work already in the ledger, and duplicate structural agents — are written up
      in `ARCHITECTURE.md` §10.2 and migrations 002/003.

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

### Bug fixed 2026-08-17: the dashboard's first job was to expose a phantom agent

The Colony panel came up showing `investigator` running for over an hour. It wasn't. The
22:00 pulse had been killed mid-groom by the scheduled task's own `ExecutionTimeLimit` —
**PT10M, shorter than the two 7-minute grooms the same task was authorised to run** — and
because `run_ticket` opens the `runs` row *before* spawning (so cost survives a crash), the
kill left a row saying `running` forever. Two fixes: the limit is now `PT30M`, and every
pulse calls `reap_orphaned_runs()`, closing any run older than 20 minutes as `timeout` with
the verdict `orphaned: parent process died mid-run` and blocking its ticket. Tokens already
spent stay recorded — an orphan is an unknown ending, not a refund. It counts as an anomaly,
not a reason to wake: paying a model to look at a process that no longer exists buys nothing.

A dashboard reporting live work that isn't happening is worse than no dashboard, and this
one found that out about itself within a minute of first rendering.

### Bug fixed 2026-08-17: the windowed launch died with no way to say why

`python -m colony dash` worked; launching it with `pythonw.exe` — no console, which is how a
desktop app is meant to start — exited 1 instantly and silently. Cause: `cli.py` computed
`_COLOR = sys.stdout.isatty()` at **import** time, and under `pythonw` `sys.stdout` is None.
The one stream that would have reported the problem *was* the problem. `_COLOR` is now
None-safe, `_force_utf8()` swaps a missing or unwritable stream for `os.devnull`, and the
window keeps its own `.colony/dash.log` so a silent GUI death is never debugged by guessing.

## Why it matters beyond the tooling

From the Notion idea: it is a portfolio piece (a working multi-role agent ecosystem), it
builds real scrum fluency from the PO seat rather than the team seat, it maps cleanly to IAM
thinking (what permissions each role needs — see `ROSTER.md` §2, which is that argument in
miniature), and every skill the forge promotes makes the next sprint cheaper than the last.
