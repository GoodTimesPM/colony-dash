# Colony Dash design

The full design: the state model, the pulse step by step, the budget, the forge, the
execution model and the dashboard. `ARCHITECTURE.md` at the repo root is the one-page
version. Code and migrations cite this file by section number (§4.2, §8.3). Section 10,
the build log, moved to `docs/history/build-log.md`.

Reference implementation studied: **Lloyd**, posted to r/ClaudeAI by u/croovies
("Example of a real working loop orchestrator", 431 upvotes). Lloyd runs on scape.work
(Mac-only) — we are rebuilding the *pattern* on Claude Code + Windows, reshaped around
scrum roles.

---

## 0. The one-paragraph version

The PO is the **Product Owner**. Ordis is the **Scrum Master** — a loop that wakes on a
heartbeat, reads the Notion board, breaks stories into tickets, staffs them to specialist
agents drawn from a 270-persona roster, spends a bounded **token** budget, and escalates only
what genuinely needs a human. Every fact about execution lives in a **SQLite ledger**, not
in a context window, so the loop is resumable and the dashboard is just a read view over
that ledger. Finished work is mined for repeated procedure, distilled into **Skills** that
get promoted back into the colony — so the system gets better at its own job over time.

---

## 1. Why the four ideas from Lloyd matter

These are the load-bearing lessons from the reference. Everything else is our own.

| Lloyd's idea | Why it's the right call | How we apply it |
| --- | --- | --- |
| **Heartbeat, not cron-as-fire-and-forget** | A pulse carries a standing checklist and writes a row *even when nothing happened*. "Clean" is data. A silent cron job is indistinguishable from a broken one. | `pulses` table gets a row every tick, findings or not. A gap in that table is an alarm. |
| **SQLite is the continuity** | Context windows die. 600+ tickets survived because they were never in a context window. | The ledger is authoritative; agents read and write it, they don't remember. |
| **Read-only investigation first** | An autonomous agent that can write is an autonomous agent that can wreck a repo at 3am. | Investigation agents get no write tools. Write capability unlocks only against a ticket a human has staffed. |
| **Escalate as a proposal, not a decision** | Lloyd tags findings "to be prioritized with you." | Escalations land in a PO Inbox with a recommendation and a cost estimate. Ordis never merges. |

**The bar.** Lloyd only tickets things worth looking at, and skips expected noise. That bar
is the difference between a useful loop and a notification firehose. Ours is defined in
§4.6.

---

## 2. Roles

The scrum mapping, from the Notion "Mimic Scrum Environment" idea.

- **Product Owner — the PO (human).** Owns the backlog and priority. Reviews escalations.
  Promotes skills. Accepts or rejects finished work. The only actor who can approve a
  write to a real repo or an outward-facing action.
- **Scrum Master — Ordis (the loop).** Owns the pulse. Grooms, estimates, staffs,
  dispatches, harvests, reports. Never does the specialist work itself and never approves
  its own output.
- **The colony — specialist agents.** Hired from the roster (`ROSTER.md`), spawned per
  ticket, live for one run, die. Stateless by design: everything they learn goes to the
  ledger or a skill, not to memory.

Two roles are structural rather than hired, and exist from day one:

- **`investigator`** — read-only, no write tools, ever. The only agent the loop may spawn
  without a human in the path. Produces tickets, never changes.
- **`reviewer`** — reads a completed run's diff and votes. Cheap insurance against an agent
  grading its own homework. Its verdict is advisory to the PO, not binding.

### 2.1 The shape of a hired agent

The roster supplies the *persona* (who this specialist is, how they think). Colony Dash
supplies the *governance* (what they may touch, what they may spend). One is useless
without the other — see `ROSTER.md` §2 for why.

```yaml
# agents/backend-dev.yaml — the shape every hired agent shares
role:            backend-dev
persona:         engineering/engineering-backend-architect.md   # from the roster
model:           claude-sonnet-5      # cheap default; escalates to opus for hard tickets
write_capable:   true                 # requires a PO-staffed ticket to actually use
tools_allowed:   [Read, Grep, Glob, Edit, Write, Bash]
tools_denied:    [WebFetch, Artifact] # no outward-facing actions
read_scope:      ["<projects root>/**"]
write_scope:     ["<ticket project folder only>"]
skills:          [repo-conventions, migration-checklist]
max_tokens_run:  120_000              # hard per-run ceiling — the real currency
definition_of_done:
  - "Tests pass"
  - "Diff is under 400 lines or the ticket was split"
  - "A human-readable summary is written to the ticket"
```

---

## 3. The state model

SQLite, one file, WAL mode. This is the whole system's memory. If the ledger is intact,
everything is recoverable; if it isn't, nothing else matters. §3.3 explains why SQLite and
not the MySQL server already on this machine.

### 3.1 Schema

```sql
-- A week of intent. The unit the PO plans in. One sprint = one 7-day usage window.
CREATE TABLE sprints (
  id            INTEGER PRIMARY KEY,
  name          TEXT NOT NULL,
  goal          TEXT,                       -- one sentence, set by the PO
  starts_on     DATE NOT NULL,
  ends_on       DATE NOT NULL,              -- aligned to the Anthropic 7-day reset
  budget_pct    REAL NOT NULL DEFAULT 35.0, -- max % of the weekly limit the colony may burn
  budget_tokens INTEGER,                    -- calibrated equivalent; see §6
  status        TEXT NOT NULL               -- planning|active|closed
);

-- A backlog item, mirrored from Notion. Intent lives here.
CREATE TABLE stories (
  id                  INTEGER PRIMARY KEY,
  notion_page_id      TEXT UNIQUE,          -- the sync key; NULL for loop-authored stories
  sprint_id           INTEGER REFERENCES sprints(id),
  title               TEXT NOT NULL,        -- Notion "Idea"
  description         TEXT,                 -- the Notion page body — this is the brief
  acceptance_criteria TEXT,                 -- Ordis drafts, PO approves
  project             TEXT,                 -- which folder under the projects root
  priority            INTEGER NOT NULL DEFAULT 3,   -- 1 High, 2 Medium, 3 Low (from Notion)
  est_tokens          INTEGER,              -- Ordis's estimate, in tokens
  est_cost_usd        REAL,                 -- shadow figure, derived from est_tokens
  status              TEXT NOT NULL,        -- backlog|needs-info|needs-criteria|ready|in-progress|po-review|accepted|rejected
  settled_as          TEXT,                 -- NULL = live; done|shelved|not-started = the PO filed it, colony asks nothing
  blocked_reason      TEXT,                 -- why the colony can't start (surfaces in the Inbox)
  created_at          TIMESTAMP NOT NULL,
  updated_at          TIMESTAMP NOT NULL,
  notion_synced_at    TIMESTAMP
);

-- A unit of agent work. Stories fan out into tickets.
CREATE TABLE tickets (
  id                INTEGER PRIMARY KEY,
  story_id          INTEGER REFERENCES stories(id),
  parent_ticket_id  INTEGER REFERENCES tickets(id),   -- investigation -> implementation chains
  title             TEXT NOT NULL,
  intent            TEXT NOT NULL,        -- investigate|implement|review|research|chore
  role              TEXT,                 -- which hired agent should take it
  status            TEXT NOT NULL,        -- open|staffed|running|blocked|done|wontfix
  severity          TEXT,                 -- trivial|minor|major|critical
  work_order        TEXT,                 -- the literal prompt handed to the agent
  findings          TEXT,                 -- what came back
  artifact_path     TEXT,                 -- diff, report, or file the run produced
  write_scope       TEXT,                 -- the one folder this ticket may modify
  requires_po       BOOLEAN NOT NULL DEFAULT 0,   -- write-capable work always sets this
  est_tokens        INTEGER,
  created_at        TIMESTAMP NOT NULL,
  closed_at         TIMESTAMP
);

-- One agent invocation. The cost record. Tokens are the unit; USD is derived.
CREATE TABLE runs (
  id             INTEGER PRIMARY KEY,
  ticket_id      INTEGER NOT NULL REFERENCES tickets(id),
  agent_role     TEXT NOT NULL,
  model          TEXT NOT NULL,
  session_id     TEXT,                    -- Claude Code session, for replay
  transcript_path TEXT,
  started_at     TIMESTAMP NOT NULL,
  ended_at       TIMESTAMP,
  status         TEXT NOT NULL,           -- running|ok|failed|timeout|over-budget
  input_tokens         INTEGER,
  output_tokens        INTEGER,
  cache_read_tokens    INTEGER,           -- cheap, but counted — cache hits are the win
  cache_write_tokens   INTEGER,
  total_tokens   INTEGER,                 -- the headline number on the dashboard
  cost_usd       REAL,                    -- notional; a Pro plan is not metered in dollars
  verdict        TEXT                     -- agent's own summary of outcome
);

-- The heartbeat log. One row per tick, findings or not.
CREATE TABLE pulses (
  id            INTEGER PRIMARY KEY,
  pulse_at      TIMESTAMP NOT NULL,
  tier          TEXT NOT NULL,            -- 'tick' (free, no LLM) or 'wake' (spent tokens)
  window_start  TIMESTAMP NOT NULL,       -- what time range this pulse examined
  window_end    TIMESTAMP NOT NULL,
  actions       TEXT,                     -- JSON: what it did this tick
  finding       TEXT,                     -- human-readable; literally "clean" when nothing
  anomalies     INTEGER NOT NULL DEFAULT 0,
  tokens        INTEGER NOT NULL DEFAULT 0,   -- 0 for a tick, by design
  next_pulse_at TIMESTAMP
);

-- Sampled every 5 min from the shared usage cache. The sprint burndown's real source.
CREATE TABLE usage_samples (
  id              INTEGER PRIMARY KEY,
  sampled_at      TIMESTAMP NOT NULL,
  five_hour_pct   REAL,
  seven_day_pct   REAL,
  seven_day_resets_at TIMESTAMP
);

-- Things that need the PO. The only table that demands attention.
CREATE TABLE escalations (
  id            INTEGER PRIMARY KEY,
  ticket_id     INTEGER REFERENCES tickets(id),
  story_id      INTEGER REFERENCES stories(id),   -- needs-info escalations have no ticket yet
  kind          TEXT NOT NULL,           -- needs-info|write-approval|decision|cost|skill
  reason        TEXT NOT NULL,
  recommendation TEXT,                   -- Ordis's proposed action
  est_tokens    INTEGER,
  raised_at     TIMESTAMP NOT NULL,
  resolved_at   TIMESTAMP,
  po_decision   TEXT                     -- approve|reject|defer|amend
);

-- Learned procedure. See §7.
CREATE TABLE skills (
  id             INTEGER PRIMARY KEY,
  name           TEXT NOT NULL,
  slug           TEXT UNIQUE NOT NULL,
  status         TEXT NOT NULL,           -- candidate|drafted|active|retired
  evidence_runs  TEXT,                    -- JSON array of run ids that motivated it
  path           TEXT,                    -- .claude/skills/<slug>/SKILL.md once promoted
  times_used     INTEGER NOT NULL DEFAULT 0,
  wins           INTEGER NOT NULL DEFAULT 0,
  losses         INTEGER NOT NULL DEFAULT 0,
  tokens_saved   INTEGER NOT NULL DEFAULT 0,  -- vs. the pre-skill baseline for its ticket class
  created_at     TIMESTAMP NOT NULL,
  promoted_at    TIMESTAMP
);
```

### 3.2 Why these boundaries

- **`stories` vs `tickets`.** A story is what the PO wants; a ticket is what an agent does.
  Keeping them separate is what lets one story spawn an investigation, then an
  implementation, then a review — three tickets, one intent — without the backlog turning
  into task soup.
- **`runs` separate from `tickets`.** A ticket can be attempted more than once. Cost and
  failure attach to the attempt, not the intent. This is also what makes the burn chart
  honest: retries show up as spend.
- **Estimates in tokens, not hours or dollars.** Agents don't have hours, and a Pro
  subscription doesn't bill dollars. Tokens are the only unit that is simultaneously
  measurable, budgetable, and real. See §6.
- **`requires_po` on the ticket, not the agent.** Capability is a property of the work, not
  the worker. An agent that *can* write still can't until a specific ticket says so.
- **`write_scope` on the ticket.** The colony reads the whole projects directory but writes
  to exactly one folder per ticket — the one its story names. See §8.

### 3.3 Why SQLite, and not the MySQL server

This question deserves a real answer, because the instinct that "a real database is better"
is usually right — just not here.

**What SQLite actually is.** Not a server. It's a C library that reads and writes one file
using SQL. `import sqlite3` is in the Python standard library; there is nothing to install,
start, or log into. The entire ledger is `colony-dash/.colony/ledger.db`. It's fully ACID —
transactions, rollback, crash safety — and in WAL mode it allows one writer plus many
simultaneous readers, which is exactly our access pattern: the pulse writes, the dashboard
reads.

**Why it wins for this system specifically:**

1. **Nothing to be down.** The pulse fires at 3am from Task Scheduler. If MySQL's service
   isn't running — Windows update, a failed start, a service set to Manual — the pulse dies.
   The single most important property of a heartbeat is that it can't fail quietly, and
   adding a required background service is adding a way to fail quietly. SQLite has no
   service; if Python runs, the database runs.
2. **The ledger travels with the project.** One file inside the project folder. It's
   covered by the same backup, the same copy, the same drive. Zip `colony-dash/` and the
   entire colony history — every ticket, run, and pulse — comes with it. A MySQL database
   lives somewhere else on the machine and silently isn't part of any project backup.
3. **We have exactly one writer.** MySQL's real advantage is many clients writing
   concurrently over a network. We have one process writing on one machine. We'd pay the
   full cost of the client/server model for none of its benefit.
4. **Same dialect end to end.** The Notion MCP already exposes Notion databases *as SQLite
   tables* — the sync layer queries Notion in the same dialect it writes the ledger in.
5. **Zero config surface.** No port, no user, no grant, no `my.ini`, no root password to
   store somewhere the agents can reach. Fewer secrets is a security property, and this
   system deliberately runs agents with scoped permissions.

**What we give up, honestly:** writes serialize (irrelevant at our volume — we write a few
rows a minute, and SQLite handles thousands per second); there's no network access, so the
dashboard must run on this machine; and there's no user-level permission model inside the
DB, so file permissions are the boundary.

**When MySQL would earn its place:** if the dashboard needs to be reachable from your phone
or another machine, if multiple pulses ever run concurrently on different boxes, or if the
ledger grows past a few GB. None of those are true on day one, and the migration is cheap
because everything is plain SQL with no SQLite-specific features.

### 3.4 The MySQL reporting mirror — approved, and built

Decided 2026-08-17: **yes.** `colony/mirror.py`, run as `python -m colony mirror`, does a
full one-way refresh of every data table into the MySQL server already on this machine.

- **SQLite stays the system of record.** Nothing in the colony ever reads back from MySQL,
  which is what makes the mirror safe to drop and rebuild — and means a MySQL outage can
  never touch the pulse.
- **Full refresh, not incremental.** At our row counts it takes a second, and a mirror that
  can't drift is worth more than one that's clever.
- **It's the SQL-practice surface.** Point DBeaver, Metabase, or plain `mysql` at
  `colony_dash` and write real joins against real work history — burn per story, tokens per
  accepted story, which roles run over their ceiling. Genuinely worth being able to describe
  in an analyst interview: an operational store and a reporting warehouse, one-way.
- **Config** lives in `colony-dash/.env` (`COLONY_MYSQL_*`), which is gitignored and on the
  never-read list for every agent. Driver (`PyMySQL`) is imported lazily — not installing it
  costs nothing until you run the mirror.

Nightly scheduling rides along with the pulse task once M1 is on the scheduler.

---

## 4. The pulse — the loop, tick by tick

### 4.0 Two tiers, so an idle hour is free

You asked for hourly, "as long as it doesn't use that many tokens to check." A pulse that
wakes a model just to discover nothing changed is pure waste, and at 24 wakeups a day it
would be the single largest line item in the budget. So the pulse is split:

| Tier | What it is | What it costs | When it runs |
| --- | --- | --- | --- |
| **Tick** | Pure Python. No model. Query the Notion API for rows changed since last sync, check for finished runs, sample usage, write the `pulses` row. | **Zero tokens.** A few HTTP calls. | Every hour, always. |
| **Wake** | Ordis actually runs — grooming, staffing, judging, learning. | Real tokens, budgeted. | Only when the tick found something: a changed Notion row, a finished run, a crossed threshold, or a PO decision in the Inbox. |

An hour where nothing happened writes `finding = 'clean'`, `tier = 'tick'`, `tokens = 0`.
On a quiet weekday the loop can log 20 heartbeats for nothing. **The heartbeat is free; only
the work costs.** That is the answer to the cadence worry — hourly is fine, and we could go
to 30 minutes without meaningfully changing spend.

A wake is also **coalescing**: three things changing in one hour is one wake, not three.

The pulse is **idempotent**. If it dies halfway, the next one picks up from ledger state —
never from remembered context. That property is what makes the whole thing survivable.

### 4.1 Sync — reconcile intent

Read the Notion board (§5). Upsert into `stories` by `notion_page_id`. Notion wins on
title, body, priority, and category; the ledger wins on execution status. Loop-authored
stories (from investigation findings) get pushed *up* to Notion so the PO sees them where
they already look.

### 4.2 Groom — make stories workable

For each newly-`ready` story, Ordis first asks: **is there enough here to build?**

- **No** → status `needs-info`, `blocked_reason` written, and a `needs-info` escalation
  raised. It shows in the PO Inbox as *"Story #N needs your attention — here's specifically
  what I can't determine."* Not a vague nag: it names the missing decision. The colony does
  not guess and does not start.
- **Yes** → Ordis drafts acceptance criteria, status moves to `po-review`. **Ordis never
  marks its own criteria `ready`** — that's the PO's, and it's the first of three human
  gates.

### 4.3 Staff — assign and estimate

For each `ready` story: decompose into tickets, hire a role per ticket from the roster,
pick a model (cheap by default — Opus only when the ticket is flagged hard), set
`write_scope` from the story's project, and write the `work_order`. Estimate `est_tokens`
from historical actuals for that ticket class. If the sprint's remaining budget can't cover
it, the ticket stays `open` and the shortfall is reported rather than silently dropped.

### 4.4 Dispatch — spawn the colony

Spawn agents for `staffed` tickets, respecting: max concurrent runs (default 3), per-run
token ceiling, and the write gate. Investigation tickets dispatch freely. **Write-capable
tickets dispatch only if `requires_po` has been satisfied** by a PO approval on the
dashboard.

### 4.5 Harvest — collect and record

For each finished run: write `findings`, `artifact_path`, the four real token counters from
`claude -p --output-format json`, the derived cost, and the agent's `verdict`. Diffs are
written to disk as patches — never applied. Optionally spawn a `reviewer` on anything above
a size threshold.

### 4.6 Judge — apply the bar

**What "the bar" means:** every pulse produces findings — an agent noticed a stale config,
a test is flaky, a JD scraper returned fewer rows than usual, a dependency is outdated. Most
of that is not worth your time. The bar is the rule deciding which findings get put in front
of you and which are just logged. Set it too low and the dashboard becomes a firehose you
learn to ignore; too high and it hides things you needed. It is one filter, and it is the
difference between the system being useful and being noise.

A finding escalates only if **at least one** holds:

1. It **blocks** a story you marked High or Medium priority.
2. It is **novel** — no `wontfix` or already-resolved ticket covers it.
3. It has **token consequences** — a retry loop or runaway that already burned past a threshold.
4. It needs **a decision only you can make** — scope, priority, or anything outward-facing.
5. It is a **write** waiting on approval.
6. A story **can't start for lack of information** (§4.2).

Concretely, with your current work: *"Job Radar's Himalayas scraper returned 0 rows three
pulses running"* clears the bar on #1 and #2 — it blocks a High story and it's new.
*"`job-search/` has 14 uncommitted files"* does not — it's true every day and you already
know. *"An agent wants to edit 6 files in `resume-engine/`"* clears on #5, always.

Everything else is logged to the pulse row and forgotten. Expected noise gets an explicit
skip list that grows over time — that list is itself a learned artifact.

### 4.7 Learn — mine for procedure

Scan recent successful runs for repeated shape. Candidate skills get written to the
`skills` table as `candidate`. Detail in §7.

### 4.8 Report — close the tick

Write the `pulses` row (including `finding = 'clean'`). Roll up token spend. If anything
crossed the bar, push a single batched notification — one per pulse maximum, never one per
finding.

```
PULSE 2026-08-17 14:00  ·  wake  ·  window 13:00–14:00
  synced     3 stories from Notion (1 new)
  groomed    #47 "Job Radar: fix Himalayas parser" -> criteria drafted, awaiting PO
             #48 "Dad Gig Scheduler" -> NEEDS INFO: no target platform named
  dispatched 2 tickets  (investigator x1, backend-dev x1)
  harvested  1 run       ok, 84.2k tok, diff -> .colony/artifacts/t112.patch
  judged     4 findings  -> 2 escalated, 2 below bar
  learned    1 candidate skill: "notion-jd-schema-drift"
  spend      91.4k tok this pulse  ·  week 22% of 35% allowance  ·  resets Mon 09:00
  next       15:00
```

---

## 5. Intake

Work reaches the ledger through two doors, and they are not the same door with
two skins.

**Notion** is the one with a workflow around it. It is somebody's board, it has
a Status column other people read, it is reachable from a machine that is not
this one, and it is synced on every tick. Everything in this section is about
that door, because it is the one with a contract to honour.

**The dashboard's `＋ story` button** is the other. It writes a row straight into
`stories` with a NULL `notion_page_id` and no round trip to anywhere. That
column has been nullable since migration 001 — the comment on it reads "NULL for
loop-authored stories" — so this needed no schema change and no flag; it needed
a form. The sync loop iterates the rows Notion returns and has no reaper for
ledger rows Notion has never heard of, which is why a locally-filed story is
invisible to intake rather than at risk from it. See §10.18.

Neither door is required. A colony with no `NOTION_TOKEN` set has a full board.

**The board:** [Project Ideas/To-Do](https://app.notion.com/p/1d23280aadd341cbbd1467c771ee6d88)
· database `1d23280a-add3-41cb-bd14-67c771ee6d88` · data source
`9b0a4ad3-29f5-41d3-9518-ee0c3ecc9481`.

This is where you write intent from anywhere — phone, laptop, away from this machine — and
the colony picks it up on the next tick. Live schema:

| Property | Type | How the colony reads it |
| --- | --- | --- |
| `Idea` | title | → `stories.title` |
| `Status` | select — New / Exploring / In Progress / Shipped / Shelved / Done / Not started | **the intake trigger**, below |
| `Priority` | status — Low / Medium / High | → `stories.priority` (High=1) and queue order |
| `Category` | multi-select | routing hint for which roster division to hire from |
| `Related Link` | url | context for the agent |
| *page body* | — | → `stories.description`. **This is the brief.** |

### 5.1 The intake contract

**Only two of the seven statuses are an instruction.** The other five are the PO
filing a row — finished, parked, or not begun — and **a filed row is the absence of a
request**, not a request of a different shape.

- `In Progress` — **in the colony's queue.** Ordered by `Priority`. The only status
  that can be groomed, staffed and worked.
- `Exploring` — the colony may run **read-only research** on it if budget allows, and
  attach findings to the Notion page. It will never write code for an `Exploring` row.
- `New` / `Not started` — captured, not thought through. Filed as `not-started`.
- `Done` / `Shipped` — filed as `done`. `Shelved` — filed as `shelved`.

Filing sets `stories.settled_as` and leaves `stories.status` exactly where it was. The
two are different facts: **`status` is where the colony had the story in its own
process; `settled_as` is whether the PO is asking for anything at all.** Keeping them
apart is what makes un-filing lossless — a story reopened after a month rejoins the
board at the status it actually had, instead of at a guess the colony would have to
spend a groom run re-deriving.

A filed story leaves the seven live columns for the **filed** shelf under the Board,
which toggles like the dropped shelf and is hidden when empty. Its open escalations
close as moot — `resolved_at` set, `po_decision` left NULL, so nothing downstream
mistakes the filing for an instruction — and its open tickets go `wontfix`.

This uses your board exactly as it already works: dragging a row to *In Progress* is how you
hand work to the colony, and it's a gesture you'd make anyway.

**The page body is the brief.** Whatever you write under `## Concept` / `## Why` /
`## Next steps` is what the agents get. Write it the way you'd brief a contractor. If it's
thin, the colony says so rather than guessing.

**When there isn't enough to start** (your explicit ask): the story goes `needs-info`, and
The PO Inbox shows a card naming the specific missing piece — *"#48 Dad Gig Scheduler: I
can't tell whether this schedules gigs for you or is a tool you're selling. Both are
buildable; they're different products."* Ordis also posts that question as a **comment on
the Notion page**, so the answer can be given from your phone without opening the dashboard.
Answering it flips the story back to `ready` on the next tick.

### 5.2 No `Project` property — the board stays a board

Decided 2026-08-17: **the Notion board is not modified.** It stays exactly what it is —
where ideas get captured — and the colony adapts to it rather than the reverse.

So Ordis infers the target folder (`pulse.infer_project`) by matching the story text against
the real directory tree two levels deep, which is what catches sub-projects: *"Job Radar: fix
the Himalayas parser"* resolves to `job-search/job-radar`, not `job-search`. Two outcomes:

- **Confident** (the folder's leaf name appears as a phrase in the story) → recorded as
  `project_source = 'inferred'` and used for read context immediately.
- **Unsure or no match** → an Inbox card before anything is staffed. *"Dad Gig Scheduler — I
  cannot tell which project folder this belongs to."*

**An inference never earns write scope.** `project_source` has to be `confirmed` — one click
in the Inbox, once per story — before a write-capable ticket can be staffed against it. The
guess costs you a click; being wrong about it would cost a repo.

---

## 6. Budget: tokens first, dollars grayed

You can read token counts as well as or better than dollar amounts, and on a Pro plan the
dollar figure is fiction anyway — nothing is metered in money. So **tokens are the unit**
everywhere: estimates, per-run ceilings, the sprint budget, the fleet panel. The USD figure
renders in muted gray beside it, as a familiarity anchor and nothing more.

```
  backend-dev   sonnet-5   running   t112   4m03s   84.2k tok   $0.34
                                                    ^^^^^^^^^   ^^^^^
                                                    primary     gray
```

### 6.1 The sprint budget is your 7-day window

You said the sprint budget should be your weekly limit. The exact number of tokens in that
limit is not published — but you already solved this problem. **The
`personal-desktop-projects/token-usage-in-tray` app** reads
`GET https://api.anthropic.com/api/oauth/usage` and gets `seven_day.utilization` (a real
percentage) plus `resets_at`. That's the true budget gauge, straight from Anthropic.

So the sprint burndown is **percent of the weekly window**, and per-run token counts are the
fine-grained unit underneath it. Over a few sprints the ledger correlates the two — "the
colony burned 4.1M tokens and 18% of the week" — and `sprints.budget_tokens` gets calibrated
from real data instead of guessed.

### 6.2 One poller, not two — a hard constraint

The tray app's `PROJECT.md` documents that this endpoint allows **~5 requests per rolling
5 minutes per account, and the Claude Code CLI spends from the same bucket.** A second
independent poller in the dashboard would trip 429s for both, and the tray app would start
showing stale numbers.

**Therefore:** the tray app remains the only poller. It gains one small change — writing
each good read to `%LOCALAPPDATA%\claude-usage\usage.json` (atomic replace, same technique
it already uses for credentials). Colony Dash reads that file and copies it into
`usage_samples`. Zero new API pressure, and the tray app's tooltip and the dashboard's
burndown can never disagree.

### 6.3 A reserve for you

The colony must not eat the week you need for your own interactive work. `budget_pct`
defaults to **35%** of the weekly window. At 30% consumed the dashboard turns amber and
Ordis switches to Haiku/Sonnet-only staffing; at 35% dispatch stops entirely, pulses keep
ticking (they're free), and the Inbox says so. The 5-hour window gets the same treatment as
a short-term guard — the colony pauses dispatch above 80% of the 5h window so a burst never
locks you out mid-session.

---

## 7. The skill forge

The piece that isn't in the reference, and what turns the loop from an automation into a
system that compounds.

**Memory vs skills.** A memory file is a *fact* — "the Notion Project Ideas DB was renamed."
A skill is a *procedure* — "here is how to reconcile a Notion schema drift, step by step,
including the two ways it usually fails." Memories are recalled; skills are executed. The
forge only produces the second kind.

```
  agent runs  ──▶  detect  ──▶  candidate  ──▶  Ordis drafts  ──▶  PO promotes  ──▶  active
                                                                                       │
                                                          retired  ◀── win rate decays ─┘
```

1. **Detect.** After each wake, scan successful runs for:
   - the **same tool sequence** used to solve the same class of ticket 3+ times;
   - a run that **failed N times then succeeded** — the recovery path is the lesson;
   - a run whose token count was a **large outlier low** for its ticket class — it found a
     shortcut, and that shortcut is worth writing down;
   - repeated **PO corrections** of the same kind — the highest-signal source of all.
2. **Candidate.** Insert into `skills` as `candidate` with `evidence_runs` pointing at the
   transcripts. Nothing is written to disk yet.
3. **Draft.** On PO request, Ordis reads the evidence transcripts and writes a real
   `SKILL.md` — trigger conditions, the procedure, known failure modes, and a link back to
   the runs that produced it. Status becomes `drafted`.
4. **Promote.** The PO approves. The file lands in `.claude/skills/<slug>/SKILL.md`, and the
   skill attaches to the roles it applies to — **and to Ordis**. This is the "learned from
   the agents, passed on to Ordis" path: the Scrum Master inherits the colony's procedural
   knowledge, which is what makes the next sprint's grooming and staffing better than the
   last one's.
5. **Measure.** Every run that loads a skill increments `times_used`; the verdict increments
   `wins` or `losses`; and `tokens_saved` tracks it against the pre-skill baseline for its
   ticket class. **A skill's value is measured in tokens it stops the colony from spending** —
   which is the same currency as the budget, so the forge pays for itself visibly.
6. **Retire.** A skill whose win rate drops below threshold over a meaningful sample gets
   flagged. Skills that stop earning their context window get removed.

**The gate matters.** Auto-promotion would let the system teach itself a bad habit and then
apply it colony-wide. Promotion is a PO decision — the third human gate.

---

## 8. Execution model on this machine

- **Runtime:** Python 3.11+, stdlib `sqlite3`, one DB at `colony-dash/.colony/ledger.db` (WAL).
- **Scheduler:** Windows Task Scheduler fires `pulse.py` hourly. Task Scheduler over the
  `/loop` skill because the pulse must run when no interactive session is open.
- **Agent spawning:** each agent is a `claude -p --output-format json` invocation with a
  scoped tool allowlist and its own working directory; the session id is recorded for replay
  and the JSON gives back real token counts.
- **Isolation:** write-capable runs execute in a **git worktree**, never the live tree. The
  patch is what surfaces for approval. This is what makes "read-only first" enforceable
  rather than aspirational.
- **Kill switch:** a `.colony/HALT` file. Present = no dispatch, pulses still log. First
  thing the pulse checks.
- **Budget enforcement:** checked three times — sprint remaining before staffing, estimate
  before dispatch, actual after the run. A run that breaches its per-run ceiling keeps its answer and is recorded with
  status `over-budget`, a billing fact rather than a failure.

### 8.1 Blast radius

You set it to the whole projects root. Accepted, with a read/write split,
because "may look at" and "may change" are different permissions:

| | Scope |
| --- | --- |
| **Read** | All of the projects root — the colony needs cross-project context to be worth anything. |
| **Write** | **Only the one project folder named by the active ticket**, and only in a worktree, and only after PO approval. |
| **Never, at any tier** | `.env` and any credential file; `.git/` internals; anything outside the projects root; `git push`, `git commit --amend`, force-push, branch deletion. |

That root was a hardcoded absolute path in `db.py` until 2026-08-27, which
worked on exactly one machine. It now defaults to the folder *containing* this checkout —
which resolves to the identical path here — and can be moved with `COLONY_PROJECTS_ROOT` in
the environment or in `.env`. The one key is read by `db._env_value`, which reads a single
line and mutates nothing, deliberately not by `mirror.load_env`: that loader pulls the whole
file into `os.environ`, and `db` is imported by the console, which hands its environment to
a `claude` subprocess. Loading `NOTION_TOKEN` there would put the token in front of the one
agent that is explicitly not allowed to read `.env`.

Three other copies of that literal outlived the first fix and were found the day after:
`seed.READ_SCOPE`, `control.DEFAULT_READ_SCOPE` and the console's system prompt each spelled
the path out again. All three now derive it from `db.PROJECTS_ROOT`. The failure mode they
had was quieter than a crash and worse for it — a checkout on another machine would seed and
hire agents whose read scope pointed at a drive letter that does not exist there, so every
agent would come back having found nothing, correctly, forever.

Priority weighting goes to **`job-search/` and the Job Radar pipeline** — that's the current
focus, so it gets first claim on the sprint budget. Everything else is worked when there's
headroom. That's a weight, not a wall: the colony still reads and can be handed a ticket
anywhere in the directory.

### 8.2 Three human gates, restated

The system is defined as much by what it can't do alone as what it can:

1. **Grooming** — Ordis drafts acceptance criteria; only the PO marks a story `ready`.
2. **Writing** — no change reaches the live tree without a PO-approved escalation.
3. **Promotion** — no skill enters the colony without PO approval.

Everything between those gates is autonomous.

### 8.3 The console — the one door that is not the colony

Every limit above is about an *autonomous* loop. A pulse that fires at 3am with no
one watching must not be able to run a shell, and `agent.py` denies `Bash` at the
top of the file for exactly that reason: it is what makes "the colony cannot push"
a capability rather than a promise.

None of that reasoning applies to the PO sitting in front of the dashboard typing.
And the cost of pretending it did was concrete: every change to Colony Dash itself
had to be made from a separate terminal, so the program that is supposed to run
itself was the one program it could not touch.

The console (`console.py`, `/api/console/*`, the Ordis panel's *open the console*
button) is a second door, and it is deliberately unlike the first:

| | The colony | The console |
| --- | --- | --- |
| **Who opens it** | the pulse, on a schedule | a person, by typing |
| **Tools** | an allowlist; `Bash` always denied | everything, `--dangerously-skip-permissions` |
| **Where it writes** | a throwaway worktree, one project, after approval | the live tree, wherever it is pointed |
| **Gates** | groom, write, promote | none |
| **Record** | tickets, runs, escalations | `console_turns`, with tokens and cost per turn |

What still holds, because it was never about the agent's permissions:

- The server binds to `127.0.0.1` unless `--host` says otherwise, and
  `/api/console/*` requires the `X-Colony` header, like every other write route.
  Off-machine, the header is not enough by itself and the token gate arms
  automatically — see §10.18.
- **No scheduled code path may import `console`.** `pulse.py` and `wake.py` do not,
  and a change that makes them do it turns the whole of §8.1 into decoration.
- Credentials still never get echoed or committed, and `git push`, `--amend`,
  force-push and branch deletion still ask first. Those are rules about the PO's
  data and history, not about what the process is technically able to do — they
  are stated in the console's system prompt, and they are the only thing in this
  column that is a promise rather than a wall.
- One turn at a time, held by both a process lock and a `pending` row. Two shells
  writing one tree is the failure the rest of this section exists to prevent, and
  it is the one failure the console could still cause on its own.

Clearing the chat starts a new `epoch` rather than deleting rows. The tokens were
spent either way, and a conversation that can erase its own cost is a conversation
that can lie about it.

Model and effort are per-conversation settings on the bar (`console_state.model`,
`console_state.effort`, migration 025), not constants in the module. The price
difference between `haiku`/`low` and `opus`/`max` on the same question is close to
an order of magnitude, and which one is right changes per message rather than per
install. *Compact* is not a local control either: it sends `/compact` as an
ordinary turn, so it queues behind a running turn, gets refused the same way, and
appears in the tape with what it cost.

The floor for a turn is roughly 30–35k tokens and it is not CLAUDE.md — that file
is 2.2 KB, about 570 tokens, under two percent of it. The rest is Claude Code's
own system prompt and the schemas for its built-in tools. It is not worth trimming:
that prefix is cached, so a turn reads it for about $0.007, and every attempt to
shrink it (`--tools` with a reduced set, `--strict-mcp-config`) writes a *new*
cache prefix and costs four to thirty times more. Measured, not assumed.

---

## 9. The dashboard

### 9.1 Why we build it rather than adopt one

The honest comparison, since "can we just use an existing app" is the right question to ask:

| Option | Verdict |
| --- | --- |
| **Grafana** | Excellent at time series. Our core objects are tickets and approvals, not metrics. Would give a nice spend chart and nothing else. |
| **Metabase** | Great SQL explorer, and a real option for *reporting* later. But it's a read-only BI tool — no approve button, no live fleet. Also awkward against SQLite. |
| **Retool / Appsmith / Budibase** | Genuinely close: drag-drop GUI over a database with buttons. But they're cloud-tethered or heavy self-hosts, and every button still needs a custom backend endpoint to actually launch a Claude run — so we write the backend either way and gain a hosting dependency. |
| **Streamlit** | The fast path — a working GUI in ~200 lines of pure Python. Real fallback if we want it running this week. Cost: a generic look you can't fully control, and full-page reruns instead of live updates. |
| **A TUI (Textual)** | Closest to the reference's aesthetic. Harder to make legible, and much harder to screenshot for a portfolio. |
| **Build it: FastAPI + one HTML page + SSE** | **Chosen.** |

Three reasons it wins, in order of weight:

1. **The approval gate is the product.** Every alternative needs a custom backend endpoint
   to spawn a Claude Code run on approve. Once that's written, the off-the-shelf tool is
   just a skin over our own API — and a dependency.
2. **You care about the feel, and it's a portfolio piece.** This is stated project intent.
   Retool screenshots look like Retool. This one should look like yours.
3. **The design is already done.** The published artifact *is* the front end — the same
   HTML/CSS, wired to real endpoints instead of static markup. The expensive part of
   "build our own" is already paid for.

**Packaging:** it runs as a real desktop window, not a browser tab — a **pywebview** wrapper
around the local server gives it a taskbar icon and its own window, using the Python stack
this machine already has. Same trick as the tray app, and it can sit on the second monitor
without a browser chrome around it. `http://127.0.0.1:8787` still works if you'd rather.

**Stack:** FastAPI + Uvicorn (bound to localhost only), server-sent events for live updates,
one self-contained HTML page, pywebview shell. No build step, no npm, no external services.

### 9.2 Layout

Read-only with exactly one exception: the PO approval controls in the Inbox. Everything
else is a view over the ledger — the dashboard can never be the reason state changed.

```
┌─ SPRINT 12 ─ "Job Radar: trustworthy daily digest" ── day 3/7 ────────────┐
│  ███████░░░░░░░░░░░░  4.1M tok  ·  22% of week   $12.40   resets Mon 09:00│
└──────────────────────────────────────────────────────────────────────────┘

┌─ COLONY ─────────────────────────────────────────────────────────────────┐
│ ● investigator   opus-5      running   t118 log scan     1m12s   18.3k    │
│ ● backend-dev    sonnet-5    running   t112 parser fix   4m03s   84.2k    │
│ ○ data-analyst   sonnet-5    idle          —                 —       —    │
│ ◐ reviewer       sonnet-5    queued    t112 diff review      —       —    │
└──────────────────────────────────────────────────────────────────────────┘

┌─ BOARD ──────────────────────────────────────────────────────────────────┐
│  BACKLOG   │  READY   │  STAFFED  │  RUNNING  │  PO REVIEW  │   DONE      │
│    12      │    4     │     2     │     2     │      1      │     3       │
└──────────────────────────────────────────────────────────────────────────┘

┌─ PO INBOX (2) ───────────────┐ ┌─ PULSE LOG ──────────────────────────────┐
│ ⚑ t112 wants to write        │ │ 14:00  wake · 2 escalated · 91.4k        │
│   6 files in job-radar/      │ │ 13:00  clean · 0 tok                     │
│   est 40k tok · [ok][×]      │ │ 12:00  clean · 0 tok                     │
│ ? #48 Dad Gig Scheduler      │ │ 11:00  wake · below bar · 12.1k          │
│   needs a decision  [answer] │ └──────────────────────────────────────────┘
└──────────────────────────────┘
┌─ FORGE (1 candidate) ────────┐
│ ⬦ notion-jd-schema-drift     │
│   3 runs · saves ~14k/run    │
│   [draft][dismiss]           │
└──────────────────────────────┘
```

| Panel | Source | Purpose |
| --- | --- | --- |
| Sprint header | `sprints` + `usage_samples` + `SUM(runs.total_tokens)` | One glance: on goal, on budget, how much week is left. |
| Colony | `runs WHERE status='running'` | Lloyd's Sessions panel. Role, model, status, ticket, elapsed, live tokens. Proof the thing is alive. |
| Board | `stories GROUP BY status` | Scrum board. Click a column to filter everything below. |
| PO Inbox | `escalations WHERE resolved_at IS NULL` | **The only panel that asks for anything.** Empty = close the tab. |
| In Flight | open `tickets` + unsent `notion_outbox` | Beside the Inbox, because it answers the question the Inbox creates: what did pressing that actually start? Read-only. |
| Pulse log | `pulses ORDER BY pulse_at DESC` | Heartbeat monitor. Consecutive "clean" rows collapse; a *missing* row renders red. |
| Completed | done `implement` tickets + filed `stories` | The record. Every delivery and every filed story, newest first, each opening the whole episode behind it. The only panel that never asks for anything and never writes. |
| Files | `projects.scan()` + the lazy tree | What moved on disk. Sortable three ways — see below. |
| Forge | `skills WHERE status='candidate'` | Promotion queue, ranked by tokens saved. |
| Spend | `runs` rolled up by day/role/story | Burn rate, most expensive story, tokens per accepted story. |

**The Files list sorts three ways, and the three are not cosmetic variants of each
other.** *changes* is the pulse's own order — commits first, then sheer volume — and
answers "what is outstanding". *recent* sorts on the newest mtime under the folder,
which is close to the opposite question: the folder you were editing a minute ago is
routinely eighth by volume, and it is the one you came to the panel to find. *name*
exists for when you already know the answer and want the list to hold still while you
click it. The timestamp is a file mtime rather than a `git log` date on purpose —
these rows are *uncommitted* changes, and the last commit says nothing about when
they happened. Every dirty path is stat'd, not only the forty the drawer shows, because
a max over a truncated sample lies exactly about the busiest folders. The choice lives
in localStorage beside the view menu: how you read a panel is not something the server
needs to know.

**Drawers keep a trail.** A drawer can open another drawer — a beat lists the projects
that moved, a story lists its tickets — and that used to be a one-way trip, with the
only route back being to close everything and find the beat again. Now the header grows
a back button naming where it goes. Nothing at the call sites had to change: `openDrawer`
runs synchronously at the top of every `open*` function, before its fetch, so "was a
drawer already on screen when this one opened?" is precisely the question of whether the
user stepped *down* or started fresh, and it can be asked in one place instead of at the
dozen places that open a drawer. Views that cannot honestly be restored — the composer,
with a half-written reply in it — pass no descriptor and so end the trail rather than
extending it.

**Appearance is the viewer's, and it never leaves the machine.** One drawer holds three
dials — text size, the fifteen palette tokens, and where each tile sits — and all of it is
localStorage beside the view menu, for the reason the view menu is: how you read the page
is not something the colony needs to know, and a page that phoned home about its font size
would be a page you could not trust to be only a page.

*Text size is a multiplier, not an offset.* Every size on the page is a step and every step
carries the same `--ui-scale`, so "make it bigger" keeps the ratios. A flat +2px would take
the 9px label to 11 and the 26px figure to 28, and a type scale whose ends have met is no
longer a scale. Getting there meant tokenising the twenty-four hardcoded `10px` and `9px`
sizes that had accumulated in the stylesheet as `--step--2` and `--step--3`; a dial that
moves most of the text is worse than no dial, because the parts that did not move are the
small ones you were trying to read.

*The palette editor shows its work.* Handing over fifteen colour pickers is handing over
the ability to make the page unreadable in four clicks, so every relationship that has to
hold shows its contrast ratio beside the swatch and goes coral when it breaks. The targets
are the ones this design actually holds, measured off the twenty-four shipped palettes
rather than copied off a checklist: ink 4.5:1 on panel, everything else 3:1. An accent on
its own chip bed is a 10px uppercase label, which argues for 4.5 — but eight of the shipped
themes sit between 3.55 and 4.4 there and none of them is hard to read. **An audit that
opens by declaring a third of the existing design broken is noise, not signal.**

*Randomize is not dice.* A uniformly random palette is unreadable roughly always, and —
more quietly wrong — it breaks the four accents loose from their meanings: a "coral" that
came out green stops saying *anomaly*. So the neutrals get a random hue, a random cast and
a coin-flip between a dark and a light ground, while the four accents keep their hue bands
and vary inside them; every colour then has its lightness solved for the contrast it owes.
A preset saves the whole look — palette, base theme and text size together — because the
same palette read at 130% and at 100% is two different designs.

*A palette named after its hues is unusable as a control panel.* `--violet` is
simultaneously Ordis, the Standby and Board titles, every focus ring and the
modified-files bar, so a picker labelled "violet" told you the one thing you
already knew and none of the four things about to move. Every swatch now says
what it paints, the rows are grouped by what they are for, and everything
downstream of the four accents is a token of its own — twelve panel titles and
four git states — so a title can be pinned without repainting the accent it was
mixed from. The four accents stay the source: pinning is opt-in, because the
whole point of rationing colour to four meanings is that the meanings propagate.
All sixteen derived tokens clear 3:1 on panel across all twenty-five shipped
theme variants, worst case 3.07, so the readout does not open red on a theme
nobody has touched.

*A capped tile scrolls inside itself* and keeps its own title bar in view, because a
scrolled panel whose heading has left the top of it is a list you cannot name. Capping is
opt-in per tile: the point of a cap is that *one* long panel stops pushing the rest of the
page down, and a page where everything is capped is a page of nine little windows.

**Relative times tick.** Everything on the page redraws when SSE pushes a snapshot — but
between two beats nothing is pushed for an hour, so "last beat 0m ago · next in 60m" was
baked at the moment of the beat and stayed there. A relative time is now a node that
remembers its own timestamp and is refreshed every fifteen seconds by its own interval.
This matters more than a cosmetic bug: the frozen clock read *younger* than the truth, and
a clock that lies in the direction of "everything is fine" is the one kind worth fixing on
sight.

*Snap is a mode, with a door at both ends.* Dragging is the fastest way to say
where a tile goes and the easiest thing to do by accident, so it is entered from
the drawer and left by a bar that is the only new thing on screen while it is on.
The tiles wiggle because that is the only signal that an ordinary click will now
rearrange the page rather than open a story. A drop **inserts** rather than
swaps: swapping moves a second tile the PO never named, while inserting pushes
the rest of the column down, which is what dragging into a list looks like
everywhere else.

*The Inbox scrolls rather than wrapping downwards*, and pads its last row with
hollow slots out to the Ticket Queue. Left to grow, a fifth question pushed the
whole board off the screen — the panel that exists to say "this needs you" was
the one making everything else unreadable. The slots are the other half of the
same idea: one question alone in a third of the width with two thirds of nothing
beside it reads as a layout that broke, not as an inbox that is nearly clear.

**In Flight is the Ticket Queue.** "In flight" described the mechanism — work
the colony has picked up — and the PO reads that panel to answer a different
question: what did I press, and has it landed. The queue is what they queued.

**The design rule:** an empty PO Inbox means the system is working and needs nothing.
Everything else on the page is ambient. If the dashboard nags when the Inbox is empty, it's
wrong.

**In Flight is the Inbox's other half, and it is deliberately not a second Inbox.** Every
control on this page queues rather than acts — a dispatch waits for the next wake, a board
change waits for the next pulse — which is the right design and leaves one hole in it: from
the moment you press the button to the moment the pulse runs, the page says nothing. The
work existed only two clicks deep in a story drawer, so "did that go through?" had no answer
where the decision was made. The rail sits beside the Inbox because that is where the button
was, holds both shapes of pending work (a ticket and a queued Notion push are different rows
but the same question), stripes its left edge with the same four accents the rest of the page
rations, and asks for nothing — it is a status light, not a list to work through. It scrolls
at six rows for the same reason: the moment it is tall enough to read end to end, it has
pushed the board off the screen.

The marker on a tile is the other direction of the same link. A question and the work waiting
on it are one piece of work — you approve the write in the Inbox, the ticket that was blocked
on it is in the rail — so a tile with something in flight carries a badge, and hovering it
lights the rows it means. The badge appears only when it can light something: an escalation
raised from a ticket that has since closed gets none, because a marker that highlights nothing
teaches you the link is broken.

### 9.3 The colony rail — four features taken from Lloyd's sidebar

Added 2026-08-17 after a second look at the reference. All four are backed by tables that
already exist in the ledger, so M2 is the only work.

**Avatars.** Every agent gets a face, and it never changes between runs. `agents.avatar_seed`
is a hash of `role@project`, drawn as an 8×8 pixel sprite on a canvas — mirrored down the
vertical axis so it reads as a character rather than noise — tinted with the persona's own
`roster.color`, with `roster.emoji` as the badge. That means a hired persona's identity is
carried through from the roster file to the running agent, and you learn to recognise
`backend-dev@job-radar` by sight the way you'd recognise a teammate.

**Active rail (left).** One card per running agent: avatar, role, model, ticket, elapsed, and
a progress bar that is **tokens spent against `max_tokens_run`** — the same bar Lloyd uses
for context, but measuring the thing we actually budget. Below the live ones, agents on
standby, dimmed.

**Standby browser.** The full 270-persona roster, grouped by division, collapsible, with a
search bar wired to the `roster_fts` index that's already built (`python -m colony roster
"database"` is the same query today). Clicking a persona opens the résumé — description,
vibe, and the file it came from — with a **Hire** button that opens the contract form
(model, tools, write scope, ceiling). Hiring is a PO action and raises a `hire` escalation
rather than taking effect silently.

**Artifacts panel (right).** Lloyd's file manager, ours being the run outputs: everything
under `.colony/artifacts/` — patches, reports, transcripts — grouped by ticket, with a
preview pane. Diffs render with syntax highlighting. This is also where an approval is
actually decided, because a patch you can read is the only honest basis for approving one.

**Click-through detail.** Every card on the board opens a drawer built from `story_events` —
the append-only timeline of everything that happened to that story, including `learning`
events. Not just "where is this", but "what did we find out, and when". That table exists
precisely so the detail view has something worth reading.

---
