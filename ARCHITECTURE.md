# Colony Dash — Backbone Architecture

The orchestrator loop, its state model, and the dashboard that renders it. This is the
design document; `PROJECT.md` holds current status, decisions, and TODOs.
`ROSTER.md` covers where agents come from and how they get hired.

Reference implementation studied: **Lloyd**, posted to r/ClaudeAI by u/croovies
("Example of a real working loop orchestrator", 431 upvotes). Lloyd runs on scape.work
(Mac-only) — we are rebuilding the *pattern* on Claude Code + Windows, reshaped around
scrum roles.

---

## 0. The one-paragraph version

Jordan is the **Product Owner**. Ordis is the **Scrum Master** — a loop that wakes on a
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

- **Product Owner — Jordan (human).** Owns the backlog and priority. Reviews escalations.
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
read_scope:      ["D:/ALL STUFF/PROJECTS/**"]
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
  project             TEXT,                 -- which folder under D:\ALL STUFF\PROJECTS
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
  status         TEXT NOT NULL,           -- running|ok|failed|timeout|killed-over-budget
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

-- Things that need Jordan. The only table that demands attention.
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
the PO Inbox shows a card naming the specific missing piece — *"#48 Dad Gig Scheduler: I
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
  before dispatch, actual during the run. Breaching the per-run ceiling kills the run with
  status `killed-over-budget`, a recorded outcome rather than a crash.

### 8.1 Blast radius

You set it to the whole of `D:\ALL STUFF\PROJECTS`. Accepted, with a read/write split,
because "may look at" and "may change" are different permissions:

| | Scope |
| --- | --- |
| **Read** | All of `D:\ALL STUFF\PROJECTS` — the colony needs cross-project context to be worth anything. |
| **Write** | **Only the one project folder named by the active ticket**, and only in a worktree, and only after PO approval. |
| **Never, at any tier** | `.env` and any credential file; `.git/` internals; anything outside `D:\ALL STUFF\PROJECTS`; `git push`, `git commit --amend`, force-push, branch deletion. |

That root was a literal `Path("D:/ALL STUFF/PROJECTS")` in `db.py` until 2026-08-27, which
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
  force-push and branch deletion still ask first. Those are rules about Jordan's
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
question: what did I press, and has it landed. The queue is what he queued.

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

## 10. Build order

Each milestone is independently useful — the system is never a half-built thing waiting on
the next piece.

| # | Milestone | Ships | Useful on its own because |
| --- | --- | --- | --- |
| M0 ✅ | **Ledger** | Schema, migrations, seed, roster scan, CLI, MySQL mirror | A queryable record of project work exists |
| M1 ✅ | **Pulse, read-only** | `pulse.py`, tick/wake tiers, Notion sync, grooming dispatch | Daily automated status, zero write risk, near-zero tokens |
| M2 ✅ | **Dashboard, read view** | FastAPI + SSE + pywebview, all panels except approvals | The always-open window; the thing you actually wanted |
| M3 ✅ | **Hiring + gates** | Staffing from the roster, worktree isolation, write-capable dispatch, Inbox approvals, HALT + allowance | Real autonomous work with a safety rail |
| M4 ✅ | **Skill forge** | Detection, drafting, promotion, tokens-saved tracking | The compounding loop turns on |
| M5 ✅ | **Two-way Notion** | Checklist memory, stale escalations, a queued write path, dropping | The backlog stops being two places |

**Start at M0 and M1.** They're cheap, they're safe, and a week of pulse logs is the data
that tells us whether the bar in §4.6 is set correctly and what a token actually buys —
calibrations everything downstream depends on.

### 10.1 What exists on disk (2026-08-18)

```
colony-dash/
  colony/
    migrations/  001 schema · 002 chargeable tokens · 003 structural uniqueness
                 004 M3 control — po_actions, controls, project_changes, tickets
                 005 PO replies — po_messages, escalations.snoozed_until
                 006 skill forge — skill_uses, detector + draft columns, new verbs
                 007 draft requests — skills.draft_requested_at
                 008 two-way Notion — notion_outbox, checklist halves, staleness, drops
    db.py        WAL, foreign keys, hash-checked append-only migrations
    seed.py      investigator + reviewer contracts, sprint 1
    roster.py    scans ~/.agency-agents into `roster`; FTS search
    notion.py    stdlib REST client for the intake board — reads, and four writes
    outbox.py    the queue between a PO decision and a Notion HTTP call
    pulse.py     the two-tier heartbeat
    wake.py      the wake tier — grooming, budget guard, retry cap
    agent.py     the only code that spends tokens: one `claude -p` invocation
    control.py   every PO decision; the only module allowed to change state
    worktree.py  a throwaway git worktree per ticket; the write blast radius
    build.py     a staffed ticket → a patch waiting for approval
    forge.py     detect / draft / promote / measure — the compounding loop
    projects.py  one `git status` for the whole tree, bucketed by path
    mirror.py    full-refresh ledger → MySQL
    server.py    FastAPI, read-only connections, SSE change feed, /api/act/*
    desktop.py   pywebview shell; logs to .colony/dash.log; sets the window icon
    icon.py      the longhouse, drawn with Pillow → ui/colony.ico
    shortcut.py  writes the desktop .lnk through WScript.Shell
    ui/index.html  the whole front end — one file, no build step
    ui/colony.ico  generated; the taskbar mark
    cli.py       init / status / roster / agents / sql / pulse / mirror / dash
                 halt / resume / allowance / projects / shortcut / schedule / forge
    proc.py      every subprocess goes through here; no console window ever
    schedule.py  installs the hourly task under pythonw.exe, hidden
  .colony/ledger.db             gitignored; the ledger
  .colony/HALT                  present ⇒ the colony spends nothing
  .colony/worktrees/            gitignored; one per in-flight ticket
```

```
python -m colony init                 create + seed + scan the roster
python -m colony status               the dashboard, in text
python -m colony pulse                one heartbeat (free unless it wakes)
python -m colony pulse --dry-run      preview, writes nothing, spends nothing
python -m colony pulse --no-wake      tick only — guaranteed zero tokens
python -m colony roster "database"    search the hiring pool
python -m colony sql "SELECT ..."     SELECT-only console
python -m colony dash                 open the dashboard window
python -m colony dash --serve         server only, no window (browse to :8787)
python -m colony halt "reason"        stop all spending; the pulse keeps logging
python -m colony resume               lift the halt
python -m colony allowance            what the sprint may spend, base + boost
python -m colony allowance 10         +10 points for a high-volume sprint (0 clears)
python -m colony projects             what moved across the whole tree
python -m colony projects --diff X    the actual diff for one project
python -m colony shortcut             (re)write the desktop shortcut
```

**Verified working:** migrations, seeding, a 270-persona scan across 17 divisions, FTS
search, project inference, story upsert with change detection, needs-info escalation, the
`story_events` timeline, tick-vs-wake tiering, usage sampling from the shared cache, the
hourly scheduled task, **a wake that actually spawns an agent, grooms a story, and records
what it cost**, and — since 2026-08-18 — **the dashboard window itself: every panel in §9,
live over SSE, approvals included.** Also verified on M3 day: a POST without the
`X-Colony` header refused with 403; an over-cap allowance refused with 409 and a sentence
a person can read; a dispatch of a `needs-info` story refused by name; hire, dispatch,
cancel, retire, confirm-project and halt/resume all round-tripped against a **copy** of the
ledger, so the live board was never a test fixture.

### 10.2 What the first real wake taught us

The first grooming run is worth recording in full, because three of the four things it
proved were things the design had wrong.

**It worked.** Given "15 Part Job Search" it read the Job Radar source, decided the story
was not buildable yet, and named exactly why: whether the manual *Job & Internship Tracker*
and the auto-written *Job Radar Tracker* should merge, coexist with a defined handoff, or
one retire. That is a data-model decision only the PO can make — precisely the §4.2 contract.
It also returned a `learning` nobody asked for: a real bug in `score.py`, where
`seniority_block()` disarms every title disqualifier if an entry-level marker appears
anywhere in the same title, so "Associate Manager" survives a filter meant to drop it.

**The ceiling was measuring the wrong number.** 394,844 tokens against a 60,000 ceiling —
but 333,382 of those were cache *reads*: the same context re-read each turn, already paid
for when written. Counting them makes any ceiling unreachable. `runs` now stores both
`total_tokens` (the honest sum) and `chargeable_tokens` (input + output + cache writes),
and budgets, ceilings and the sprint line all use the chargeable figure. Real cost of that
run: **~50k chargeable, $0.50.**

**A breach is not a failure.** The first version overwrote the run's status with
`killed-over-budget`, and the caller's "did it succeed?" check then discarded a completed,
correct answer we had already paid for. Over-budget is now a separate flag: the breach
raises a `cost` escalation, and the work is harvested either way. **Never pay twice for the
same question.**

**Waking has to include work already in the ledger.** The tick only woke on *change* from
Notion, so six ungroomed stories would have sat untouched forever. Groomable backlog is now
itself a reason to wake, capped at `MAX_ATTEMPTS = 2` per story so a story the agent keeps
failing on can't bill for the same failure every hour.

### 10.3 What building the read view taught us

**A dashboard's first job is to disagree with you.** Within a minute of first rendering, the
Colony panel showed an agent that had been "running" for eleven minutes. Nothing was
running. `run_ticket` opens the `runs` row *before* spawning, so cost survives a crash —
which also means a killed parent leaves a `running` row nobody will ever close. The parent
had been killed by the scheduler's own `ExecutionTimeLimit = PT10M`, a cap shorter than the
two seven-minute grooms the same task was authorised to run. The task is now `PT30M`, and
`reap_orphaned_runs()` closes anything still `running` after twenty minutes as `timeout`.
Tokens already spent stay recorded: **an orphan is an unknown ending, not a refund.** It
raises an anomaly, never a wake reason — you don't spend money reacting to a corpse.

**Read-only by construction, not by discipline.** Every request opens the ledger with
`db.connect(read_only=True)`. The dashboard cannot be the reason state changed, so no
panel needs to be audited for write side effects. Approval controls stay out until M3,
where the gate that governs them exists.

**The change feed must not tick.** The pulse writes hourly from another process, so the
server re-reads its own snapshot every two seconds and pushes only when a sha256 of it
moves. Elapsed timers are excluded from that fingerprint and computed browser-side from
`started_at` — otherwise every clock second would look like a state change and the feed
would push forever.

**The stream that reports failures was the failure.** The windowed launch died with exit 1,
no traceback, no log. Cause: `cli.py` evaluated `sys.stdout.isatty()` at import time for
colour detection, and under `pythonw.exe` `sys.stdout` is `None`. Fixed with a None-safe
check, a hardened `_force_utf8()`, and `.colony/dash.log` — because a GUI that dies
silently is a GUI you debug by guessing.

**Still deliberately not built at the end of M2:** staffing and write-capable dispatch
(§4.3–4.4). They need the Inbox approval gate, and building the spawner before the gate
that governs it is the wrong order. That is M3.

### 10.4 What building the write view taught us

**The gate chain is the product.** M3 is not "the colony can now edit files" — it is six
gates, four of them human: groomed → the PO accepts the acceptance criteria → the project
folder is confirmed → an agent is hired with a write scope → the PO dispatches → the build
runs in a worktree → the PO approves the patch. Every one of those can be refused, and a
refusal costs nothing. The feature is the number of places a person can say no.

**Record the decision before it takes effect.** Every write inserts a `po_actions` row
first, in the same transaction as the thing it authorises. If the effect fails the record
rolls back with it, and if it succeeds there is no ordering in which the ledger shows a
state change nobody asked for. "What changed and who said so" is one query, always.

**Nothing in `control.py` spends tokens.** Approving a story marks it dispatchable; the
next wake decides whether to act. That single indirection is why a mis-click is free — the
dashboard hands out permission, and only `agent.py` ever converts permission into money.

**Denying Bash is the load-bearing guarantee.** Edit and Write are bounded: the worst case
is a wrecked throwaway worktree, thrown away. Bash is unbounded — one line reaches the
network, the credential store, or `git push`. So the write contract grants Edit/Write
inside one worktree and denies Bash outright. "The colony cannot push" stops being a policy
we intend to follow and becomes a capability the process does not have.

**The handoff is a patch, not a merge.** An approved build lands uncommitted in the real
project folder. Jordan reads the diff in the drawer and commits it himself, in his own
words. The colony never commits, never pushes, never rewrites history — which also means
the recovery from a bad approval is `git checkout .`, not archaeology.

**HALT is deliberately asymmetric.** It writes both `.colony/HALT` and a `controls` row, so
a running pulse and a cold-started one reach the same conclusion. It does *not* stop the
heartbeat: a halted colony still logs, still syncs, still reaps orphans. And it cannot kill
an in-flight run — the honest promise is **"no new work"**, and the panel says exactly that
rather than implying a kill switch we don't have.

**Movement, not dirtiness.** The first project scan reported "75 untracked" every hour
forever, because `git status` reports a folder as dirty for as long as it stays dirty. The
pulse now logs only projects whose counts differ from the last sample or that have commits
in the window. A log that repeats an unchanging fact is a log nobody reads.

**A custom header is the whole CSRF story.** Every `/api/act/*` POST must carry
`X-Colony: 1`. A cross-origin form can POST to localhost; it cannot set a custom header
without a preflight the browser will refuse to send. One header, one `_guard`, done — and
the server is bound to 127.0.0.1 regardless.

*(Later: that last clause is exactly the assumption `--host` breaks. The header is
still the whole CSRF story; it was never the access story. §10.18.)*

**Refusals are written to be read.** `control.Refused` maps to 409 with its message intact,
and every message names the state and the next move: *"story is needs-info, not ready.
Accept its acceptance criteria in the Inbox first."* An error that tells you which button
to press next is the difference between a gate and an obstacle.

**Personas are read from disk, not from the ledger.** The roster table carries names and
divisions; the drawer reads the actual `.md` file when you click. 270 persona bodies in
every snapshot would be megabytes down the SSE feed to answer a question asked once — and
reading the file means what you see is what the agent will be handed, not a copy that
drifted.


### 10.5 What a day of living with it taught us

**A console window has two parents and you must kill both.** The dash flashed a terminal
2-3 times a minute and popped an hourly one that never closed. These looked like one bug
and were two. The flashing was `PROJECT_TTL_S = 30.0`: every 30 seconds the SSE snapshot
re-ran `projects.scan()`, which shells out to `git` — a console child spawned from a GUI
process gets a console. The hourly window was the scheduled task itself running through
`cmd.exe`. `colony/proc.py` fixes the children (`CREATE_NO_WINDOW` plus a hidden
`STARTUPINFO`, applied at every call site); `colony/schedule.py` fixes the parent (the task
runs `pythonw.exe`, `-Hidden`). Fix one and the other is still on screen, which is why the
first attempt looked like it had failed.

**A reply is not a decision.** The PO can now type a free-form answer into any Inbox card,
the way you would type into Claude Code. It queues a `po_messages` row, spends nothing, and
**leaves the escalation open** — the next wake reads it and answers. Ordis may *suggest* a
project folder from a reply but never confirm one: `project_source` stays `inferred`, so no
reply can authorize a write. That keeps §8.2's three gates intact while removing the thing
that actually blocked work — a dropdown with no right answer in it. A reply that resolves
an item closes it as `amend`, never as an approval nobody gave. And if the agent produces
no parseable JSON, the message stays `unread`: an unanswered question survives.

**`scan()` answers "what moved"; `tree()` answers "what is there".** The file panel was
built on `scan()`, so it was empty exactly when the working tree was clean — it looked
unimplemented because a tidy tree and a broken panel render identically. They are two
different questions and the panel needs both: the change list on top, the browsable tree
below. The tree is lazy, one directory per request, because the root holds ~60 projects and
some carry `node_modules`.

**Validate the resolved path, not the string.** `safe_path()` resolves first and then checks
containment, so a symlink pointing out of the tree fails the same way `..` does. It also
rejects any segment that is hidden or matches `is_secret()` — `.env*`, `credentials.json`,
`id_rsa`. `create_project()` goes further and whitelists each segment with a regex, because
it creates rather than reads: it must fail closed.

**Specificity beat intent in the theme CSS.** Four of the eight themes did nothing on a dark
OS. `@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]):not(...) }` scores
0,4,0 and outranks `:root[data-theme="ember"]` at 0,2,0 — system-dark won every time. The
guard is now `:root:not([data-theme])`. A theme that was chosen is never "system", so the
absence of the attribute is the entire condition, and the chain grows no longer as themes
are added.

**"Later" has to move something.** A button that only re-timestamps a card is indistinguish-
able from a button that does nothing. It now writes `snoozed_until`, the tile dims, and it
sorts to the bottom — the state is visible, and un-snooze is the same call with
`snooze_hours: 0`. (Which is why the server reads it with an explicit `is None` check:
`or 8` would silently turn un-snooze back into a snooze.)

### 10.6 What building the forge taught us

**Detection belongs in the tick, not the wake.** The first sketch had the forge scanning for
candidates after each wake, which is where the runs it reads come from. But detection is pure
SQL over work already paid for, and a thing that costs nothing has no business waiting behind
a budget guard. Running it in the tick means a HALTed colony still *notices* that a procedure
is emerging — which is right, because noticing is not doing. A new candidate is also
deliberately not a reason to wake. It sits there until the PO asks for it.

**Asking for a draft and paying for one are separate events.** `control.py`'s second rule is
that nothing in it spends tokens, and the obvious implementation of a "draft this" button
breaks that rule in one line. So the button writes `draft_requested_at` and the next wake does
the work, behind the same budget check as everything else. The PO gets a queued acknowledgement
instead of a spinner, a mis-click costs nothing, and the gate and the spender stay separate
exactly as they do for dispatch. The column is a timestamp rather than a flag because
"asked at 14:02, still not drafted" is a question a boolean cannot answer.

**The forge is allowed to decline its own candidate.** The draft prompt asks for `worth_it`,
and a false answer retires the candidate on the spot with its reason attached. Three runs that
succeeded easily and identically teach nothing, and a skill that restates the obvious is a
permanent tax on every future run's context window. The cheapest place for a bad idea to die
is before a file exists.

**`tokens_saved` had to be derived, or nobody would believe it.** A single counter that only
ever goes up is unfalsifiable. `skill_uses` records one row per run that loaded a skill, with
the baseline it was measured against, so the headline figure can always be taken apart into the
runs that produced it — and a saving is allowed to be negative, because a skill that makes runs
*more* expensive has to be able to say so.

**A skill loaded into a failed run is a loss.** `record_uses` is called before the early
returns in `groom_story`, not after them. Recording only the runs that finished would mean the
win rate measures the runs the skill was already winning, which is how a metric quietly stops
measuring anything. For the same reason `killed-over-budget` counts as a success: those runs
produced their answer, and excluding them would hide exactly the runs a shortcut skill helps.

**The detector that proposed a skill is kept.** `skills.detector` survives retirement, because
the useful long-run question is not "which skill failed" but "which signal keeps proposing
worthless skills" — unanswerable without it.

**The whitelist goes where the string becomes a path.** `write_skill_file()` re-validates the
slug even though it came out of the same row `promote_skill` just read. The row is not the
boundary; the `Path` join is. Promotion also writes the file *last* in the transaction, so a
failed write rolls the ledger back rather than leaving a `skills` row pointing at a path that
does not exist. The reverse residue — file on disk, COMMIT failed — is the lesser harm: an
unreferenced SKILL.md is inert.

**The panel shows active skills, not only pending ones.** The M2 stub listed candidates and
drafts, which meant the forge panel went empty exactly when the forge had *succeeded* — the
same failure the Files panel had in §10.5. Once a skill is promoted, what the PO wants to see
is what it has earned since.

### 10.7 What building the two-way link taught us

M5 started as three separate complaints — the Inbox kept asking about work that was already
finished, there was no way to change anything from a phone, and a story you had decided against
sat on the board forever. They turned out to be one bug wearing three coats: **the colony had no
memory of which version of a story it was talking about.**

**An escalation is prose written at a moment.** "This cannot start until you decide X" is true
the hour it is raised and false an hour later, once you have gone and decided X in Notion. The
text does not know that. `escalations.raised_hash` records the version of the story the question
was written against, and `stale_at` marks the moment the story moved past it. Nothing rewrites
the prose — you cannot amend a question after the fact and still call it a record of what was
asked — but the tile now knows it is talking about a page that no longer exists.

**Stale is a flag, not a delete.** The obvious fix is to drop the escalation when the story
changes. It is also wrong: the colony really was confused, and erasing the evidence erases the
one signal that says the grooming prompt needs work. Stale questions sort to the back, hide
behind a toggle, and keep a **re-ask** button, which is the only honest answer to a question
about last week: not yes, not no, but *go and read it again*.

**A checklist has two halves and the ledger only kept one.** `fetch_page_body` flattened
`- [x] merge the databases` and `- [ ] decide the retention window` into the same kind of
string, so the ledger could see the sentence but not the checkbox. Every downstream symptom
followed from that: the groom prompt could not tell an agent what was already done, so the agent
asked; the board could not show progress, so you had to open Notion to find out you had already
finished. `done_items` / `open_items` are stored separately and **both** feed the content hash,
which is what makes a ticked box a change the colony notices at all.

**The fix for re-asking about finished work is not cleverness, it is a heading.** The groom
prompt now opens with ALREADY DONE (n) — *treat these as closed, do not re-raise them, do not
ask about them, do not put them in criteria* — before STILL OPEN (n). No model reasoning
required; the information simply was not in the prompt before.

**Clearing a field is not the same as re-queueing the work.** Un-parking a story meant setting
`acceptance_criteria = NULL` so `GROOMABLE_WHERE` would pick it up again — except the same
predicate caps grooming at two attempts per story, and the story had already spent both. The
first repair attempt was to compare timestamps (`t.created_at >= stories.updated_at`), which
reads correctly and is silently always-true, because `groom_story` stamps `updated_at` *after*
creating the ticket. The working version marks the superseded groom tickets `wontfix` and counts
only tickets that are not: an explicit fact in a row, rather than an inference from two clocks.

**control.py still does no network I/O.** The same rule that made skill drafting a queued
request in §10.6 makes every Notion write a `notion_outbox` row: the button writes the row, the
tick performs the HTTP. That buys retries, an audit trail of everything the colony has said
upward, and a switch that holds the queue instead of dropping it. It also means a Notion outage
costs a delay rather than a decision — and `flush()` never raises, because a tick that dies
because Notion was slow is a tick that stops doing the eleven other free things it was going to
do.

**HALT is not the Notion switch.** HALT means *spend nothing*, and a comment on a page is not a
token. A halted colony that also went silent upward would look broken to anyone reading the
board on their phone, when what it actually is is paused. `controls.notion_write` is separate,
and either can be on while the other is off.

**Sync before flush, and the newer side wins.** A status set on a phone at 9am must land in the
ledger before the colony overwrites it with one queued yesterday. `_tick` syncs first and
flushes second for exactly this reason; it is a merge, not a push.

**The colony's Notion vocabulary is deliberately tiny.** Set a status, tick a checkbox, leave a
comment. It may not create rows, delete rows, or edit the brief — the brief is the one artefact
that is unambiguously the PO's. `"In Progress"` is excluded from `WRITABLE_STATUS` for a
sharper reason than tidiness: it is the status the intake filter selects on, so a loop able to
write it could feed itself work forever.

**Dropping had to be reversible to be usable.** A drop that deleted anything would be a decision
nobody makes at 11pm. It archives, records *why* — the prompt refuses to proceed without a
reason, because six months later the reason is the only part anyone wants — closes the open
questions, cancels the waiting tickets, and stays restorable. It refuses outright on an
`in-progress` story: a running ticket has a worktree and a budget attached, and archiving the
story out from under it orphans both.

**Colour was doing no work.** Every panel title was `--ink-dim`, which made eleven sections read
as one grey mumble; finding STANDBY meant reading the words. The section hues are derived from
the four accent tokens with `color-mix` rather than written per theme, so every palette gets
them without any of them drifting out of key with its own ground — and the 3px bar down the left
of each title is the part that actually carries at 11px mono.

**A theme has to be nameable from its colours alone.** The first pass shipped thirty-six, and
reading them side by side the PO could see that eleven were one of three palettes with a
different comment above it: deco, cold war and grunge were moss; harvest, clay, paper, rivendell
and outer rim were parchment; spice, frontier and mordor were ember. Writing a *theme* and
writing a *mood board entry* are different jobs, and it is the reference — chrome, tintype,
avocado — that makes them feel distinct while the hex values quietly converge on the same warm
neutral. Eleven were deleted and four rebuilt from the thing itself rather than its era: a diner
is neon red and blue at 1am, not beige at noon; Y2K is a rave flyer, not the white plastic the
flyer was advertising. The test that survives: if you cannot name the theme from a strip of its
own swatches, it is a duplicate wearing a costume.

Running that test over the survivors rebuilt seventeen of the twenty-four. The failure mode it
exposes is narrower than "they look alike": a theme drifts when it is written from its *mood*
rather than its *reference*. Cyberpunk written from a mood is dark-and-neon; written from the
reference it leads with an acid yellow, and the yellow is the entire recognition. Neon noir and
cyberpunk are the same street — the difference is water, so neon noir has to be the *less*
saturated of the two, which is the opposite of what writing-from-mood produces. Where a palette
already exists in the world, quoting it beats approximating it: LCARS for bridge, polar night
for fjord, P1 and P3 for the two CRTs. And a theme with a discipline should keep it all the way
down — phosphor is one hue at four intensities, and its orange anomaly is a documented lie,
because an alarm that reads as "slightly brighter green" is an alarm nobody sees.

**Every palette is contrast-audited rather than eyeballed.** A theme is thirteen colours that
have to hold four relationships: ink over ground at 4.5:1, and each of the four accents over its
panel at 3:1. The audit runs over the parsed stylesheet, so a palette written at 2am is checked
by the same standard as the rest. Exactly one failed — fjord's aurora red at 2.46 against the
polar-night panel — and lifting it two steps off the published value is the right call every
time, because an anomaly colour you have to go looking for is not an anomaly. The audit also
catches the subtler fault: two accents close enough that amber and coral stop meaning different
things. Daylight and basalt are exempt from all of this on purpose. They are what a viewer who
never touches the picker sees, and being the neutral default *is* their identity — a system
theme with a point of view is a point of view nobody chose.

**A picker sorted by change recency is a picker you cannot use.** The folder dropdown fell back
to the working-tree scan when `/api/projects` had not arrived, and that list is ordered by what
moved most recently — perfect for "what did I touch today", useless for "find job-search in this
list". Sorting at the point of render rather than trusting either source is the fix that stays
fixed.

**What you look at is not what runs.** The view menu writes localStorage and nothing else.
Hiding a panel does not stop the colony filling it, and nothing about the choice reaches the
server — a page that phoned home about which panels you had open would be a page you could not
trust to be only a page. Panels default on and detail defaults off, so a panel added later
appears for someone who has been using the menu for months.

### 10.8 What the silence taught us

**A heartbeat can die on its own command line.** Eight hours of no beat, and the scheduled task
was firing every hour, on time, with `LastTaskResult 2` — which reads like `ERROR_FILE_NOT_FOUND`
and is not. It was the pulse's own exit code: argparse exits 2 on a usage error. The task passed
`--log D:\ALL STUFF\PROJECTS\...\pulse.log` unquoted, this machine's project root has two
spaces in it, and the pulse rejected the strays and quit before it reached any colony code. The
lesson is not "quote your paths" — it is that **an exit code from a scheduler is the child's exit
code**, so a silent loop should be diagnosed by running the exact registered command line by
hand rather than by reading the number as the scheduler's own. The proof of the fix is the same
command line, quoted, exiting 0.

Two things made eight hours of silence possible at all. The task reports success or failure to
nowhere, so a pulse that dies every hour looks exactly like a pulse that has nothing to say; and
the log the pulse writes is the log it never got far enough to open. **A heartbeat needs a
liveness signal that does not depend on the heartbeat running** — the dashboard should read
`LastTaskResult` and the age of the newest `pulses` row, and say so when the newest beat is older
than two intervals.

**The window was throwing away every setting the page saved.** The theme did not
survive a relaunch, and neither did the text size, the palette or the tile
layout — all of which live in localStorage on purpose, because how the PO reads
the page is not the colony's business. The page was innocent: pywebview's
`webview.start()` defaults to `private_mode=True`, which hands WebView2 an
incognito profile and bins its storage when the window closes. It presented as a
bug in the theme picker, which is the wrong file entirely — the picker wrote the
key, read it back within the session, and was correct every time. The profile now
lives at `.colony/webview/`, beside the ledger and inside the same gitignore.
**A default that silently discards state is worse than one that fails**, because
the failure is attributed to whatever last touched the state.

**A refusal without its reason is a bug report you cannot act on.** Every queued Notion
push failed with `HTTPError: HTTP Error 403: Forbidden`, which is what `urllib` says when it
throws the response body away — and that sentence is consistent with four unrelated causes:
the page is not shared with the integration, the page id is wrong, the token expired, or the
integration is read-only. Notion had said which all along, in the body: `restricted_resource
— Insufficient permissions for this endpoint`. Reads were fine, so the token and the sharing
were fine; the integration simply had **Read content** and not **Update content**, which is a
two-click fix nobody could find behind the number. `_request` now raises `NotionError`
carrying Notion's own `code` and `message`, and the outbox writes that straight onto the row
rather than prefixing a class name in front of a sentence that already reads as one.
**Nothing from the request can reach that string** — it is built only from the response — so
the token cannot leak into a tile.

**An audit is only useful if the thing it audits can pass it.** The palette generator was
written to solve each chip bed against its accent at 4.5:1, and a four-thousand-sample run
of the same algorithm failed 78% of the time. The reason is structural rather than a tuning
problem: on a light theme, if the accent sits just clear of a near-white panel, no bed light
enough to belong on that panel can also be clear of the accent — the solver walks the bed to
`#ffffff` and the pair still fails. Choosing the bed *first*, as a tint, and then solving the
accent for the harder of its two jobs, passes 5000 of 5000 with every worst case above the
worst shipped theme. **The lesson is to sample the generator, not to eyeball three outputs**:
three good rolls prove nothing about a space this size, and there is no JS engine on this
machine, so the algorithm was re-derived in Python and run in bulk instead.

**`--dry-run` wrote to Notion.** It gates the ledger — the whole pulse runs inside a transaction
that gets rolled back — and `outbox.flush()` sits inside that transaction making live HTTP calls
to somebody else's server. The rollback un-does the `attempts` increment and the `last_error`, so
a dry run that really did attempt a board mutation leaves a row that still reads "never tried".
**A transaction is not a sandbox.** Anything a dry run does over the network is already done, and
worse, the local record of having done it is the part that gets erased.

### 10.9 What a note that became a question taught us

**Two lines of code made the colony invent obligations out of notes.** Intake mapped
`status = "backlog" if notion_status == "In Progress" else "needs-criteria"`, so all six
of the other Notion statuses became `needs-criteria` — which is inside
`wake.GROOMABLE_WHERE`. An idea Jordan wrote down and left alone came back an hour later
as a question in his PO Inbox asking which folder it belonged to. The other line is the
same mistake from the other end: the *update* path never touched `status` at all, so
moving a row to Done in Notion changed nothing in the ledger and the story stayed on the
board looking stale. **A default branch in a status mapping is a claim that every
unlisted value means the same thing**, and here five of them meant the opposite of the
one they were folded into.

The fix is a `settled_as` column rather than three more values in `status`. Partly
mechanical — `status` carries a CHECK constraint, widening it in SQLite means rebuilding
a table three others hold foreign keys into, inside a `BEGIN;…COMMIT;` executescript
under `PRAGMA foreign_keys = ON`. But the mechanical obstacle pointed at the real one:
they are different facts, and a column that has to answer two questions gives a wrong
answer to one of them the first time they disagree.

**A groom ticket is a receipt with an expiry, and nothing was expiring it.** A groom that
ends in a question leaves a `blocked` ticket behind. That ticket is useful exactly as
long as the question is open. Jordan answered both Age of Fate questions by confirming
the project folder, and saw two identical `Groom: Age of Fate Pack · BLOCKED` tiles in
the Ticket Queue — because `_flight` shows every ticket in
`('open','staffed','running','blocked')` and nothing had closed them. The duplicate tiles
were the cheap half of the bug. The expensive half: `GROOMABLE_WHERE` counts non-`wontfix`
groom tickets against `MAX_ATTEMPTS = 2`, so **answering the question was precisely what
froze the story at two attempts, permanently.** `control.clear_spent_groom_tickets` now
retires blocked groom tickets with no open escalation on every tick — housekeeping rather
than a migration, so rows already in that state heal on the next beat — and
`confirm_project` and the reject-criteria path call `regroom_budget` so the answer that
closes a question also gives the story its attempts back.

**Two spellings of the same predicate drift, and the drift is silent.** `server._ordis`
had its own hand-written groomable SQL, missing the attempt cap and the `dropped_at`
check; the rail said 4 while the pulse said 3. It imports `wake.GROOMABLE_WHERE` now.
There is no version of this where two copies stay equal.

**A number that stops moving because the colony is standing down reads exactly like a
broken counter.** The sprint header sat at `172.3k tok` for days and Jordan asked if it
was stale. It was correct — 172,258 chargeable across ten runs, and no run has ended
since 2026-08-18 01:00. Likewise the forge: detection is free and runs in the tick, three
candidates exist and two have drafts requested, but drafting runs in the wake behind
`budget_ok`, and every wake since had logged *"week at 49% is at or past the 35% colony
allowance"* — into the pulse log, where a PO looking at the forge card would never think
to look. The header now carries `last run <ago>`, and a queued draft says
`requested — held: week at 49% of the 35% allowance` in coral, next to the button that
queued it. **Neither fix is mechanism; both are the page saying out loud what it already
knew.** A dashboard that shows a stalled value without showing why is asking its reader
to guess between "working" and "broken", and the guess is free to make wrong.

**The pulse log scrolls now.** It grows by a row an hour and never shrinks, so it was the
one panel guaranteed to eventually own the page. It scrolls inside its own body rather
than capping the section, which keeps the newest beat under the heading where you look
for it; `PULSE_LIMIT` went 40 → 120, because the limit stopped being what fits on screen
and became how far back you can scroll — about five days of hourly beats.

### 10.10 The status you filter out is the status you cannot see

**§10.9 built filing and none of it could ever fire.** `notion.fetch_board` sent
Notion a filter — `Status = In Progress OR Exploring` — which is the right answer
to *what may the colony work on* and the wrong answer to *what is on the board*.
The sync needs the second. A row moved to Done left the result set entirely, so
the sync never saw it move; the story sat in the ledger frozen at its last
workable status, `settled_as` stayed NULL, and the migration's backfill had
nothing to backfill because `notion_status` was never a settled value in the
first place. Two of the six live stories had been marked Done in Notion for a
day and a half and were still on the board asking questions.

The filter was correct the day it was written, when the sync's only job was
finding work. It became wrong the moment a second job — noticing that work had
stopped — was given to the same query. **A predicate that encodes one caller's
question is a landmine for the second caller**, and it does not announce itself:
the sync did not fail, it succeeded over the wrong set.

Reading the whole board costs one request for the page list. The expensive part
is the per-row body fetch, and a filed row does not need one — it is fetched for
its status alone. So `body_fetched` rides along on the row dict, the sync writes
the body columns only when a body was actually read, and a story keeps its brief
on the way to the shelf instead of arriving blank. The same flag suppresses the
ticked-items diff, which would otherwise report every checked box as newly
unchecked the first time a filed row synced.

**A filed row that has never been seen is not imported.** The insert path raises
a "which folder is this?" escalation when it cannot infer a project — so
importing the whole idea list would have recreated §10.9's bug at ten times the
volume, with a not-started row producing exactly the question it must never
produce. Filing therefore only ever applies to stories the colony already knows
about.

**A button that waits for a round trip is a button that does nothing.** Pressing
*Done* queued an outbox row and stopped. The story went quiet up to an hour
later, when the next tick sent the push and the tick after that read it back —
and with `notion_write` off, or a read-only token, never at all. `queue_notion`
now applies the filing to the ledger at the moment of the press and queues the
push as the mirror of a decision already made. The sync stays the authority on
what Notion *says*; this is the colony agreeing with an instruction it was
handed directly. `settle_story` and `revive_story` moved to `control.py` for it,
which is where they belonged anyway — they are ledger decisions, not heartbeat
bookkeeping.

`WRITABLE_STATUS` lost `"Archived"` in the same pass. It is not an option on the
Status select, and Notion answers an unknown select option by **creating** it —
so the one list whose job is to bound what the colony may write was the thing
that would have added an eighth status to the board.

**A ticket that is born staffed and dies done inside one wake is invisible.**
`wake.answer_po` created its own ticket, ran it and closed it between two page
loads, so replying to Ordis produced no row in the Ticket Queue at any moment a
human could observe. From the PO's side the reply went nowhere. `control.reply`
now opens the ticket at write time — status `open`, the message itself as both
title and work order, linked by `tickets.po_message_id` — and the wake *claims*
that ticket rather than opening a second, filling in the role and the real
prompt when it does. The role is deliberately left NULL until then: which tier
answers is a budget decision made against the ceiling that applies at wake time,
and a role written down an hour early is a guess wearing a fact's clothes.

The tile carries the sentence the PO typed, clamped to two lines, and reads
`reply · waiting for Ordis` rather than `research · unstaffed` — the mechanism
was accurate and told him nothing. **This is the outbox lesson a second time:
the wait is the thing worth showing.**

### 10.11 A screenshot is the message, and the shell was eating the page

Four faults, one shape: the dashboard kept refusing to carry what the PO
actually wanted to say.

**The drop dialog offered a status that does not exist.** Dropping a story asked
"also set it to Archived in Notion?", and §10.10 had just removed `"Archived"`
from `WRITABLE_STATUS` because it is not an option on the Status select. So the
drop came back refused, with the one sentence guaranteed to make no sense in
context — *starting work is yours* — for an act that is the opposite of starting
work. It offers **Shelved** now, which is a real option and is what dropping a
story means. The lesson is not about the string: **a constant that two callers
disagree about will be wrong at whichever one nobody re-read.**

**"In Progress" is writable now.** It was kept off the list to stop the colony
moving a row into its own intake filter and feeding itself work it invented.
That is still the right rule, but the list was the wrong place to keep it: the
only two callers of `queue_notion` are a button in the story drawer and the drop
dialog, and both of them are the PO's hand on a control. No agent, wake or tick
queues a status. What the omission actually prevented was *Jordan* starting work
from the dashboard, which was never the thing to prevent. **A guard placed one
layer away from what it guards ends up forbidding the wrong party.**

**The window was eating text selection.** pywebview defaults `text_select` to
`False` and enforces it by injecting `user-select: none` over the whole
document, so nothing on the page could be highlighted or copied — not a token
count, not a finding, not an error. A kiosk default living inside a tool. This
is the second time a pywebview default has quietly broken the dashboard in a way
that looked like our bug (`private_mode` was the first, §9.2), and the pattern
is worth naming: **the shell has opinions about what a page is for, and they are
opinions about a different page.**

**A reply can carry a screenshot.** Everything the PO said before had to survive
being retyped as prose first — "it destroyed the formatting of the title" is a
lossy re-encoding of the picture, and he was doing the lossy part by hand.
Paste, drop or pick; the upload lands on `POST /api/upload` at the moment of the
paste rather than at send, so a file that is too big fails while he is looking at
the composer instead of an hour later. Files live in `.colony/attachments/` under
generated names — the label is kept for the chip, the filename never is, because
a value that has been through the browser is an input again when it comes back.
`resolve` re-checks containment against the resolved parent, so `..`, a symlink
and an absolute path all fail identically.

The work order hands the agent an **absolute path**, not base64: `.colony/` is
already inside the read scope, `Read` opens images, an image costs the same
either way, and a prompt that carries its evidence by reference is one you can
still read in the ticket a week later.

**And a story can be replied to.** Replying used to require an Inbox item to
reply *to*, which made every conversation the colony's to open — the PO could
answer questions and could not raise one. Most of what he wants to say about a
story arrives while he is reading the story. The story drawer has the button now;
a story-only thread is a `po_message` with a NULL `escalation_id`, which
`answer_po` already handled.

**Filing stopped truncating the title.** The filed shelf borrowed the dropped
row's shape, and a dropped row's interesting half is the *reason* — one line,
ellipsis, move on. A filed row's interesting half is the title, so shelving a
story took a full-size board title and dropped it into a dim one-line stub with
its end cut off. It wraps now, and it is a button back into the story: the shelf
is where you go to ask "did I finish that?", and an answer you cannot click is
half an answer.

### 10.12 The conversation belongs to the story, and readiness is not a question

**A thread scoped to the escalation was deleting the history.** `control.thread`
keyed on `escalation_id`, and an escalation is an episode: Ordis closes one when
he believes his answer resolved it, the next groom raises a fresh one about the
same story an hour later, and the drawer opens *empty* on the new question with
four messages sitting one row away in the ledger. Jordan opened "reply to
Ordis" on story 1 and found nothing there — the three "15 Part Job Search cannot
start yet" escalations (#6, #8, #13) are one conversation that the schema had
cut into three, two of them already closed.

Worse: the answer he never saw was written into escalation #8 at 20:07:25 and
the same wake closed #8 at 20:07:56. **The reply and the door closing on it were
thirty-one seconds apart.** A tile that vanishes carries its own contents out of
the room. The Inbox's `messages` / `last_reply` / `awaiting_ordis` subqueries had
the identical bug one layer up, so the new tile also reported zero messages on a
story with a four-message history.

The thread keys on the **story** now, and the tile's counts with it. *An
escalation is an episode; the story is the thread.*

**And the thread shows the things that were not messages.** Four kinds of thing
happen in one of these conversations and only one was ever drawn: the PO writes,
Ordis answers, the colony *raises a question* — which is what starts most of
them, and was invisible inside them, so a reply arrived with no sign of what it
replied to — and Ordis *records a learning*, the only part of the exchange still
worth anything a month later, which lived two clicks away in the story timeline.
`control.conversation` merges all four into one time-ordered list and the page
gives each a colour on the existing rationing: amber is the PO, violet is Ordis,
mint is the learning, and the question is deliberately the quiet one because it
is a heading for what follows rather than another voice.

**Ready-to-start is derived, not raised.** Nothing had ever been dispatched, and
the reason is that nothing tells you when a story becomes dispatchable. Accepting
the criteria closes the last escalation, the tile disappears, and the story sits
in `ready` behind a button two clicks into a drawer — the moment the PO thinks
the work has begun is the moment the colony goes silent about it.

The fix is *not* another escalation. An escalation is an event: raised once,
answered once, closed forever — and readiness is a **state**, true until someone
dispatches. Raised as a question it could be dismissed while still being true,
which is the one failure this Inbox exists to prevent. So `server._ready` derives
the tile from the story: it exists for exactly as long as the story is ready, and
it is gone the instant the ticket is cut. It has no `id`, nothing to approve and
nothing to snooze; the only two useful controls are *start it* and *here is what
is still in the way*.

That last part matters as much as the tile. `control.dispatch` enforces three
preconditions — criteria accepted, project confirmed, somebody hired with write
scope — and the only way to learn which one you failed was to press the button
and read the refusal. The tile carries the blockers on its face. **A gate that
only speaks when you push it is indistinguishable from a gate that is open.**

**Why only two agents have ever run.** Nothing is wrong with the roster; the
colony has only ever done one kind of work. `wake.run` loads exactly one
contract — `contract(conn, "investigator")` — and grooming, answering the PO and
drafting skills are all research, so all three go to the same desk. The seeded
`reviewer` has no caller. The 270 personas in `roster` are a hiring pool nobody
has hired from. The rest of the org is behind `build.py`, whose contract is
looked up per *project* (`contract(conn, role, project)`) and which only ever
runs from a dispatched ticket — so the org chart unlocks at dispatch, and
dispatch is what this section just made visible.

**The allowance dial turns both ways.** +5 / +10 / +25 were three ways up and no
way back: every one raised the ceiling, so an overshoot could only be cleared to
zero and rebuilt. It is −5 / +5 / clear now, the two steps deliberately the same
size, so a mispress costs exactly one press to undo.

### 10.13 One palette, a dial with a whole range, and a way back out

**The allowance was a ratchet.** `allowance_boost` was clamped at zero on the
way down and +25 on the way up, and the three buttons were all "up" — so the
control could only climb, an overshoot could only be cleared to zero and rebuilt,
and the ceiling on the ceiling was a guess made on the PO's behalf about a quota
he shares with his own Claude Code sessions and knows more about than the code
does. The delta is signed now, the only clamp left is 0–100% of the week (an
allowance outside that is not a number, it is a typo), and there is a box to type
the figure into — because "I need 80% this week" is a thing you know directly and
reaching it by counting nine presses is arithmetic the page should be doing.
Zero is a real setting: the colony stops spending without the finality of HALT.

`control.set_allowance` still stores a *delta from the sprint's baseline* rather
than overwriting `budget_pct`, so what the sprint was designed around stays
visible beside whatever the PO has done to it. `set_allowance_pct` is the same
store reached from the other end.

**Two views of one conversation were colour-coded on two schemes.** The story
timeline and the reply drawer show the same exchange, and violet meant "a
learning" in one and "Ordis said it" in the other; amber meant "blocked" in one
and "you said it" in the other. Reading them side by side meant re-learning the
colours halfway down the drawer. The timeline now uses the thread's scheme —
amber is the PO, violet is Ordis, mint is the learning, the prompting question is
the quiet one — and names the rows the way the drawer names them, because "note"
twice in a row is not the timeline of a conversation and "you" then "ordis" is.

The two conversational rows are `note` events told apart by their summary, which
is written at exactly one place each (`control.py` for the PO, `wake.py` for
Ordis). Their `kind` cannot carry it: `story_events.kind` has a CHECK constraint,
and adding a value means rebuilding the table — which would still leave every row
already in the ledger uncoloured.

**A leaf drawer can have somewhere to go back to.** `openDrawer` pushed the trail
only when the *new* view was itself returnable, which conflated two different
things: a half-written reply cannot be re-opened by a back button (that would be
a lie about what was preserved), but it can still know where it came from. The
cost was the reply drawer — you opened a story, clicked *reply to Ordis* to
answer the thing you were reading, and the only way back to the story was to
close everything and find it again. It pushes the trail whatever the new view is
now, and a reply opened straight off an Inbox tile seeds the trail with its own
story, which is the one place the question's context lives.

**A truncated learning looked like a finished sentence.** `_event` cuts
`summary` at 400 characters, which is right for a line in a timeline — but the
callers passed it the whole thought and `detail=None`, so the 401st character did
not exist anywhere. The learnings in the drawer ended mid-word and there was
nothing to expand to, because nothing had been kept. `wake._said` now writes the
gist to `summary` and the whole text to `detail`, and the page folds any long
block to a few fading lines with a button that says how much more there is. The
twelve learnings already in the ledger were written before this and cannot be
recovered; they stay as they are.

**A tile folds when you click its title.** The heading is the one part of a panel
that is never content, which makes it the obvious handle and meant no new control
on nine panels. Folded is a *layout* fact — stored in `LAYOUT` beside the column,
the order and the height cap — so a tile you put away is still away tomorrow. It
is distinct from capping: a cap says "this one is long, keep it in a box"; a fold
says "not this week". Two things the handler must not swallow: the buttons that
live inside some headings (Board's *filed*, Files' sort order) and a click while
the board is in snap mode, where dragging a tile by its title is the interaction.

### 10.14 The spend panel stops being a sparkline

A 34px line over the last fourteen days answers one question — is it going up —
and refuses every question with a number in it. The panel now carries a real
chart: five grains from an hour to a year, a line or a bar, faded gridlines on
both axes, and the value under the pointer written out in full.

**The series moved out of the snapshot.** `/api/state` is one payload for eleven
panels, pushed on every fingerprint change; there is no reason for the other ten
to carry 48 hourly buckets so that one of them can draw a line the PO may not be
looking at. `GET /api/spend?grain=…` is a separate read, fetched when the panel
renders and when the grain changes, and `_spend` in the snapshot is now the
by-role breakdown alone.

**One hourly query, five grains.** The endpoint groups `runs` by hour in SQL and
rolls the buckets up in Python. The hourly query returns one row per hour that
actually had a run — bounded by real activity, not by the length of the window —
so it stays small however far back the chart looks, and "a week starts on
Monday" is one line of `timedelta` instead of a nest of SQLite date modifiers.
No timezone work: every timestamp in the ledger is written with
`datetime('now','localtime')`, so the buckets are cut on the clock the PO reads.

**Empty buckets are emitted, not skipped.** A chart that plots only the hours
that had runs draws a continuous line across a quiet night and calls it steady
spending. The flat stretch at zero is the information. The response also carries
`outside` — the runs that fall before the window — so "0 runs" can be told apart
from "all of it happened earlier than this".

**Drawn in pixels, not a stretched viewBox.** The sparkline could afford
`preserveAspectRatio="none"` because it had no text in it; the moment there are
axis labels, non-uniform scaling smears them. The width comes from the element
and a `ResizeObserver` redraws when it changes — width only, since drawing is
what sets the height and watching that would be a loop. Two SVG details worth
recording: `var()` is not legal inside a presentation attribute, so the accent
is set once as the element's `color` and everything inside uses `currentColor`;
and the crosshair is built once and moved rather than redrawn per pixel of
pointer travel.

Bars own a band and the line is plotted at the centre of the same band, so the
crosshair lands in the same place whichever shape is showing. The readout never
empties — with nothing hovered it holds the window total — because a line that
appears on hover and vanishes on leave makes the panel jump every time the
pointer crosses it. Grain and shape are localStorage, like the Files sort order:
how you read a panel is not a decision about the colony, so it does not belong
in the ledger.

### 10.15 A window you can move, and a row that stays a row

**The spend chart can be pointed somewhere other than now.** `/api/spend` takes
an `end`, `_series` anchors its buckets there instead of on the clock, and the
panel grows a pair of paging arrows, a date box and a `now`. Three sizes of step
over one axis: the arrows move a whole window, the date box lands on a bucket,
and the arrow *keys* walk one bucket at a time — and run the window on when they
reach the edge, which is what makes the whole ledger reachable without a mouse.

Paged windows overlap by exactly one bucket: the new window ends where the old
one began, so the bucket that was under the crosshair when the arrow was pressed
is still on screen after the load, and paging reads as a pan rather than a jump
cut. The client does its own calendar arithmetic to get there, because `Date`
normalises month and year overflow the same way the server's `_back` does — the
alternative was a second endpoint that exists only to say "one window earlier".

Two consequences of a movable window that were not true of a fixed one. The
count of runs outside it had to split in **two** — `outside` behind and `ahead`
in front — because "0 runs" on a paged window is far more often a window pointed
at the wrong end of the ledger than a quiet fortnight, and only a count on each
side tells those apart. And the response carries `live`, whether the window
still ends in the present, which the page uses to grey out the forward controls
rather than hide them: a button that vanishes takes the layout with it and
removes the affordance at the moment you most want to know it exists. `end` is
also deliberately *not* persisted — the grain is a habit worth remembering, but
a date you paged to is a look you took once, and a dashboard that opens in July
because that is where you left it is a dashboard lying about the present.

**The Inbox ghost slots were a one-shot measurement.** `padSlots` asks the grid
how many columns `auto-fill` resolved to, which is the right question — but the
answer is only true of the width the grid had at that instant, and the render
that pads the row is not always standing on a laid-out grid: a fold still
opening, a window not yet sized, the first paint of a restored layout. Measured
then, the row was padded to a width that no longer existed and the slots stopped
short of the Ticket Queue; measured a moment later it was fine. That is the
whole of the "sometimes they come back, sometimes they don't". The live count is
now kept on the element, the padding is idempotent, and a `ResizeObserver` redoes
it on every width change. `getComputedStyle` returning an unresolved
`repeat(auto-fill, minmax(...))` — which is what a display:none ancestor gets —
is reported as *don't know* rather than counted, because a confident wrong
column count is worse than no answer when something is about to ask again.

**The fold caret is gone, and was never really there.** `content: "\u25BE"` is a
JavaScript escape in a CSS declaration; CSS spells it `\25BE`, and `\u` in a CSS
string is simply the letter `u`. Every heading on the page had been printing the
literal text `u25BE` since the folding tiles landed. Fixing the escape was the
smaller change; removing it was the better one. Nine headings were each carrying
a marker for a control most of them will never be used for, the pointer cursor
already says the heading is clickable, and a folded tile is unmistakably folded.

### 10.16 Modified against what, and moved by whom

Three complaints about the same missing sentence, which is that a number on a
dashboard is only information next to the thing it is a number *of*.

**"What is modified relative to?"** One commit, the same one for every folder:
the whole of `D:\ALL STUFF\PROJECTS` is a single git repo, so `modified` means
"different from HEAD" sixty times over. The panel had never said so. `head()`
now carries the subject and date alongside the sha, `scan()` stamps the branch
and sha on every row, and the drawer opens with **compared with** before it
shows a single count. The buckets are named in the reader's vocabulary rather
than git's: `untracked` is a statement about git's index, and "new, never
committed" is the same fact stated about the file — which is the one that
explains a folder full of changes nobody remembers making. The Files heading
names the baseline too, and each row carries when it was last written, because
"why is this dirty when I never opened it" is usually answered by a timestamp
two weeks old.

The drawer's eyebrow read **"undefined · undefined"** on every project, for the
plain reason that it was reading `branch` and `head_sha` off a scan row that had
never carried either. They belong to the repo, so `/api/project` returns the
repo's head.

**"8 PO decision(s) to act on" over an Inbox holding one item.** The count was
`WHERE resolved_at IS NOT NULL AND po_decision IS NOT NULL` — every escalation
he had *ever* decided, with nothing to clear it. It only went up. From his first
approval onward every tick had a standing reason to wake, forever. Two separate
things were wrong. The count is now scoped to the window, so it decays like the
other reasons. And the wording had the direction backwards: `control.decide`
applies an approval at the moment it is made, so these are decisions *he made*,
not decisions waiting on him — what the wake picks up afterwards is the
consequence, which already has its own reason in the list.

**"Projects that moved" was a list of names.** `project_changes` stored a level
— how many files were dirty at that instant — and the drawer rendered it under a
heading that promised a change. A folder sitting at fourteen untracked files all
week read exactly like one that gained fourteen in the hour. Migration 012 adds
the four deltas against the previous sample, the file list behind them, when the
newest of those files was written, and `moved_by`. They are stored rather than
recomputed because the drawer is reading a beat from hours ago: by then "the
previous sample" is a different row and the files have moved on.

`moved_by` is the honest half of *why*. The colony's only route into the working
tree is a patch the PO approved, so the ledger can say with certainty when a
change was not its doing — and "not the colony" is the sentence that answers
"I genuinely didn't touch those things". What it deliberately does not do is
guess *what* wrote them; a dashboard that invented an author would be worse than
one that admits the machine has other programs on it.


### 10.17 What packaging it for strangers taught us

Preparing this repo to be read by someone who has never seen it turned up three
bugs, and none of them was in the logic. They were all in the assumption that
there is only one machine.

**A literal path is a bug that only fires on someone else's computer.** `db.py`
held `Path("D:/ALL STUFF/PROJECTS")`, and fixing that one line felt like the
whole job because every other module derives its root from it. It was not: a
grep the next day found the same string spelled out again in `seed.READ_SCOPE`,
in `control.DEFAULT_READ_SCOPE`, and inside the console's system prompt. The
first one was load-bearing and obvious. The other three were the dangerous kind,
because a wrong read scope does not crash — it produces an agent that searches a
directory that is not there, finds nothing, and reports that honestly. The
lesson is that "I fixed the hardcoded path" is a claim that has to be checked
with a grep for the *value*, not for the constant.

**A test suite that needs an install is a test suite nobody runs.** The whole
dependency surface of this project is four packages, and adding a fifth so that
the tests can run would have been the largest thing in `requirements.txt` by
consequence. Standard-library `unittest` costs a slightly less pleasant
assertion vocabulary and buys `py -m unittest discover -s tests` working on a
fresh clone with nothing installed but the runtime.

What the 41 tests cover is deliberately not "the code". It is the set of
statements this document makes that are otherwise only promises:

* migrations apply in filename order, are idempotent, and refuse to run if an
  already-applied file has been edited — the sha256 guard in §3.1
* `connect` really does set WAL and foreign keys, and a read-only handle really
  does refuse a write — §3.3
* `ALWAYS_DENIED` wins over a contract that asks for `Bash`, and the write tools
  unlock only with `allow_writes` — §8.1
* a write scope refuses `..`, dot folders, absolute paths and folders that do
  not exist, and a read-only agent has no scope to widen — §8.1
* the console admits exactly one turn, and a pending row left by a crashed
  process still blocks the next send — §8.3
* `db._env_value` adds nothing to `os.environ`, which is the leak the whole
  function exists to avoid

Two of those needed the `claude` CLI intercepted rather than run: `agent.invoke`
is checked by capturing the argv it would have executed, and the console's lock
is checked with `_answer` replaced by a stub that blocks. A safety test that
spawns the thing it is testing is a safety test that costs money and fails on a
machine without an API key.

**The dashboard was one 6,957-line file.** It had grown that way honestly —
there is no build step here on purpose, since a toolchain would be more moving
parts than the page it builds — but a single file holding the markup, 1,830
lines of CSS and 4,880 lines of JavaScript is one no editor will syntax-check
and no diff will read. It is now `index.html`, `app.css` and `app.js` in the
same folder, served by two new routes with `Cache-Control: no-store`, which is
what the page already effectively had by being re-read from disk on every
request. Caching `app.js` on a dashboard that is edited while it is open buys
nothing and costs an afternoon.

The split was verified by reassembling the three files and diffing the result
against the committed original: byte-identical. That check is worth more than
reading the diff, because the failure being guarded against is not "did I move
the code" but "did I move it exactly".

### 10.18 The phone, and the two doors work already opened

Three things were wanted here: file a story without going through Notion, read
the dashboard on a phone, and reach it from off the machine. They sound like one
feature and they are three, with almost nothing shared between them.

**The first one was already legal, just unreachable.** `stories.notion_page_id`
has been `TEXT UNIQUE` and nullable since migration 001, carrying the comment
"NULL for loop-authored stories". The only `INSERT INTO stories` in the entire
codebase is in the Notion intake path, and the sync loop walks the rows Notion
hands back rather than reconciling the table against them — there is no reaper.
So a row Notion has never heard of is not a row at risk; it is a row intake
never looks at. What was missing was a function, a route and a form, not a
column and not a flag.

The one thing that did need widening was `po_actions.action`, which is a CHECK
constraint, which SQLite cannot alter in place. Migration 026 is the same
full-table rebuild as 006, 008, 018, 020, 021 and 023: create the new table,
copy the rows, drop the old, rename, rebuild the index. Verified against a copy
of the live ledger rather than a fresh one, because the interesting question was
whether 140 real rows and their foreign keys survived the rebuild, and a fresh
database has neither.

**A named folder on a locally-filed story is `confirmed`, not `inferred`.** That
is the same distinction §8.2 draws for `confirm_project`: only a confirmed
project can become a write scope, and the PO typing a folder into the form is
the same act. But `create_story` is stricter than `confirm_project` in one way —
it *refuses* a folder that does not exist. In the inbox, a typo costs one more
question. Here it would silently become the confirmed write scope at the moment
of creation, with nothing downstream left to catch it. A story filed with no
folder raises exactly one `needs-info` escalation, which is the existing
machinery for "we know what you want and not where".

**The service worker caches nothing.** It exists because a browser will not
offer to install a page without one, and it responds to exactly one thing: a
failed navigation, with an offline notice. Not `/api/state`, which cached is
yesterday's board rendered with today's confidence. Not `app.js`, which cached
is precisely the bug the `no-store` headers on those routes were added to
prevent. It is served from `/sw.js` at the root rather than from `/ui/`, because
a worker may only control pages at or below its own path.

**Responsive was mostly about undoing scrolling.** `.tiles`, `.done-list` and
`.flight .rail` each cap their height and scroll internally, which is right on a
1500px window and a trap on a phone: a swipe that starts inside a scrolling box
moves the box instead of the page, and the page underneath looks frozen. Under
720px all three go `max-height: none; overflow: visible` and the page does the
scrolling. Three smaller ones cost more time than they should have: `100dvh`
rather than `100vh`, because `100vh` on a phone means the viewport with the
address bar hidden; `font-size: max(16px, var(--step--1))` on inputs, because
iOS zooms the page when a focused field's type is under 16px and does not zoom
back out, and the floor has to be absolute rather than a step because the type
scale is a user setting; and `viewport-fit=cover` paired with
`env(safe-area-inset-*)`, which together are the difference between a standalone
launch that looks like an app and one that looks like a page with grey bands.

**The `X-Colony` header was never an access control.** This is the part worth
writing down, because the docstring that said "a header is enough" was true for
a reason that stops being true the moment `--host` is passed. The header is CSRF
protection: it proves the call came from the dashboard's own page. The *access*
control was the loopback bind. Two halves of one sentence, and only one of them
survives binding to a network address.

So `access.py` arms itself off the bind rather than off a setting. `check(host)`
returns False on loopback, returns True when a token is configured, and *raises*
otherwise — it never returns a permissive answer, so there is no path through
`serve()` that reaches `uvicorn.run` with an open, tokenless server. That is the
one property in `tests/test_access.py` worth having.

The gate answers a navigation with the login page and everything else with a
401. The discriminator is the `Accept` header rather than a list of paths,
because a browser asks for `text/html` on a navigation and on nothing else, and
a list of asset paths is a list that needs maintaining. Handing the login page's
HTML to `fetch` surfaces as a JSON parse error, and handing it to a
`<script src>` surfaces as a syntax error on line one of a file that is fine —
both of them several layers from the cause.

The cookie is `HttpOnly`, `SameSite=Lax`, ninety days, and deliberately **not**
`Secure`: a tailnet address is plain http, and a cookie the browser refuses to
store is a login screen that never goes away. `?k=<token>` exists so the first
visit can be a link or a QR code; the middleware swaps it for the cookie and the
page strips it from the address bar with `history.replaceState`, because a token
in a URL is a token in the browser history and in every screenshot of it.

One dependency was avoided on purpose. `await request.form()` pulls in
`python-multipart` — a fifth runtime package in a project whose install story is
four. The login form is one field; `urllib.parse.parse_qs` on the raw body is
six lines and no new import.

### 10.19 Why accounts are a different program, not a later feature

The obvious next step after "reach it from a phone" is "give it accounts", and
it is worth being precise about why that is not the next step.

**Desktop/phone parity is not what accounts buy.** It came free with §10.18.
There is one server, one ledger and one filesystem; the phone is a second view
of the same state, not a second copy of it. There is no sync step because there
is nothing to sync. Accounts would not improve that — they would introduce the
problem they are usually brought in to solve.

**Per-user API keys do not remove the blocker.** The intuition is that each user
brings their own key and bills themselves, which is true and which solves the
cheap half. The expensive half is that this colony's actual job is spawning the
`claude` CLI against real files in real git worktrees on a real disk. Hosting
that for a second person means hosting their filesystem, their git remotes,
their `claude` authentication and their worktrees, with one tenant's build agent
one path-traversal bug away from another tenant's repository. The write-scope
rules in §8.2 are written against one operator's directory tree; they are not a
sandbox, and calling them one because there is now a login would be the worst
possible reading of them.

What that describes is a hosted build service that happens to share a schema
with this. The ledger design would survive the port — it is already the system
of record, already resumable, already free of per-machine state except for the
projects root. Nothing else would. So the honest boundary is: this program is
single-operator by construction, `access.py` is remote access rather than
authentication, and the multi-tenant version is a separate build that starts
from this database design and none of this execution model.

### 10.20 Already running when you pick it up

§10.18 made the dashboard reachable from a phone and then, in practice, did not.
The server only ran while a terminal was open on the desktop, and the phone is
the device you use *because* you are not at the desk. "First go to the desk and
start it" cancels the feature out.

So the server gets a scheduled task of its own, beside the pulse's. It is
deliberately the same shape — `pythonw` so there is no console, hidden so it
does not flicker in the task list, `--log` because a background process with
nowhere to print is a process you debug by guessing — and it differs in three
ways, each of which is a bug if you get it wrong.

**No execution time limit.** Task Scheduler's default is three days, after which
it kills the task. A server that stops on the third Tuesday and comes back at
the next logon is worse than one that never started, because the first time you
find out is from a phone that cannot reach it. `PT0S` rather than the default
`P3D`, and `autostart --show` says which one is installed rather than making you
read it out of the task's XML.

**`--host auto`, resolved every launch.** A task is written once and runs for
months; an address is a fact about the network at boot. A task holding a literal
`100.x.y.z` fails silently on the first day that address changes, and it fails
as "the phone stopped working" rather than as "the bind failed". `net.py`
resolves it instead, and is picky about what it will accept: a tailnet address
(`100.64.0.0/10`) wins, an RFC 1918 address is the fallback and is announced
differently, and anything else raises rather than being bound.

The pickiness is not decoration. The obvious implementation of "is this a
private address" is `ipaddress.ip_address(x).is_private`, which is a broader
question than it sounds — Python counts the documentation and benchmarking
ranges in it, so `203.0.113.7` and `198.18.0.1` both answer True. An address
being reserved is not the same as it being your house. The three RFC 1918
networks are spelled out, and the test for it is the one that caught the
difference.

**Restart on failure**, three times a minute apart, plus a 45-second start
delay. Both cover the same thing: losing the race with the network at logon,
which is the only failure mode that is actually likely and is exactly the one
`--host auto` would otherwise turn into a hard stop.

The task holds no secret. It names a directory and some flags; the token stays
in `.env` and is read by the process the task launches. And `preflight()` asks
`access.check` the same question the server will ask *before* registering
anything, so a missing token is a refusal in the terminal you typed into rather
than an exit code in a log at seven in the morning.

**The duplicate-server bug this created.** `launch()` decided whether a
dashboard was already up by probing one address, and that was fine while the
only address was loopback. A logon task binds the network address instead, so
`_port_is_free("127.0.0.1", 8787)` answers True while a dashboard is running,
and double-clicking the desktop shortcut raises a second server on the same port
on a different interface. Two dashboards, one ledger, and no error anywhere —
the shortcut works, the window opens, and nothing is obviously wrong.

The fix is a marker: a running server writes the address it actually bound to
`.colony/dash.url`, and `launch()` reads it. The file is a hint and never a
fact — it outlives the process that wrote it every single time — so the port
behind it is always probed before it is believed, and a marker for a different
port is ignored rather than trusted, because two dashboards on two ports is
something someone may have meant.

### 10.21 One button, and a QR encoder to go with it

§10.20 left phone access working and unreachable for a different reason: the
setup was five steps and one of them was editing a credential file. Mint a
token, open `.env`, paste it, save, run `py -m colony autostart`. Every one of
those happens at the desk, on the machine you are about to walk away from,
which is the argument for doing them from the page you are already looking at.

So `phone.py` folds the four operations into one call and the dashboard grows a
panel under **file → phone** with a switch in it. `POST /api/act/phone` resolves
an address, mints a token if there is not one, registers the logon task and
starts it; `GET /api/phone` answers the panel with the state and a QR code.
Neither is part of `/api/state`, because answering costs a PowerShell call and a
socket probe and the live feed polls every few seconds for a value nobody is
reading.

Two rules constrain the write, and both are about the fact that a web request is
now editing `.env`.

**A token is written only when there is not one.** A request that can rewrite
the access token is a request that can lock a paired phone out of the ledger by
accident, and the accident looks exactly like the button working. An existing
token is used as it is.

**The write is an append, not a rewrite.** No parse, no round trip through a
dict, no reformat. `.env` holds the Notion token beside the access token, and a
file that is only ever appended to cannot lose the line above it. The leading
newline is conditional in both directions: a file already ending in one must not
grow a blank line per call, and a file *not* ending in one must not get the
token glued to the end of the last value. Both are tests.

Turning it off removes the task and stops. The token stays, because deleting it
would log out a phone that is paired and working, and "off" here means the
server stops coming up on the network — not "forget everything".

It does *show* the token, inside the URL and inside the QR code, and that is not
a contradiction. The two callers are a terminal on the desk where `.env` already
is, and one route behind the dashboard's guard — which is either loopback or a
client that already holds the token. Neither learns anything it could not read
directly. What the module will not do is *change* the value out from under a
device already using it.

**Why there is a QR encoder in this repo.** The address is a private IP, a port
and a 43-character token, and that is a string nobody should retype on a phone
keyboard. Every library that draws one is a fine library; the install story for
this project is four packages, and "it also needs a QR encoder" is a worse trade
than three hundred lines that never change again. The format was frozen in 2000.

`qr.py` is deliberately narrow — byte mode, versions 1 through 10, no ECI, no
structured append, no kanji — which covers 271 bytes and refuses rather than
guessing beyond it. The real payload is about seventy and lands on a version 5
symbol at level M.

It was written against `segno` as an oracle and then the oracle was deleted.
960 pinned comparisons (every version, every level, all eight masks, three
lengths each), 200 fully automatic ones, a 3,908-symbol ASCII sweep and both
sides of every capacity boundary, all module-for-module identical. What survives
in `tests/test_qr.py` is a set of matrix hashes plus a decoder that reads the
symbols back out — format information, unmask, de-interleave, recompute every
block's Reed-Solomon codewords, recover the string. The fixtures prove the
output has not changed; the decoder proves it was right to begin with, and it is
the half that survives someone regenerating the fixtures.

Five bugs of ours turned up in that comparison and one in `segno`. The one worth
recording is ours: **the mask is scored before format and version information
are written.** ISO/IEC 18004:2015 §7.8 is explicit, `segno`'s source carries the
comment "DO NOT add format / version info in advance of evaluation", and doing
it the other way is invisible — every mask still produces a valid symbol, the
penalties are merely all shifted by roughly the same constant, so only a *close*
comparison between two candidates tips the wrong way. It reads as a filing
detail and is a correctness one. The same goes for the dark module at
`(8, size-8)`: it belongs to the format block rather than the skeleton, because
leaving it set during scoring puts one stray module into all eight comparisons.

(`segno`'s bug, for anyone repeating the exercise: `write_padding_bits` does
`[0] * (8 - length % 8)`, which appends a whole zero byte when the stream is
already byte-aligned — which is every byte-mode symbol below version 10. The
reference is `(8 - size % 8) % 8`. The oracle was patched before the comparison.
`segno` also prefers ISO-8859-1 for non-ASCII while this module always uses
UTF-8, which is a difference rather than a bug, and the reason the sweep is
ASCII-only.)

The terminal half is `py -m colony phone`, which prints the same code as
half-block characters — two module rows per line, because a character cell is
twice as tall as it is wide and one module per cell comes out stretched and,
on a narrow window, wrapped and unscannable. Dark modules are drawn as the
*light* half-blocks: a terminal is light-on-dark, and the naive mapping is a
photographic negative that will not scan. And it checks the stream's encoding
before it draws. `_force_utf8` reconfigures with `errors="replace"`, which is
right everywhere else in this CLI — a log line with a question mark in it is
still a log line — and wrong here, because a QR code with question marks in it
is not a degraded QR code, it is a rectangle that looks like the feature
working. It prints the URL and says so instead.

### 10.22 Two silent failures between the button and the phone

The switch in §10.21 worked, and the phone loaded forever. Neither end logged
anything, because neither end had anything to log. Two separate faults were
stacked, and what they have in common is worth more than either of them: both
report success at every layer and produce a spinner.

**The server never bound the address in the QR code.** `desktop.launch` decides
whether to start a server or point at one that is already running, and one arm of
that decision reads `.colony/dash.url`, a marker file naming the address the last
server bound. The marker exists for a real case: a desktop shortcut probes
`127.0.0.1`, the logon task may have put the dashboard on a network address, and
without the marker double-clicking the icon would raise a second server on the
same port on a different interface — two dashboards, one ledger, no error
anywhere.

It was consulted in both directions, and only one of them is sound. The logon
task asked for `10.0.0.57`, found the desktop dashboard answering on `127.0.0.1`,
concluded it was already serving, and exited. The log said the dashboard was up.
The dashboard *was* up. Nothing had ever listened on the address in the QR code.
A loopback server does not satisfy a request for a network address — satisfying
it is the entire content of the request. The decision now lives in
`desktop._reusable`, which believes the marker only when loopback is what was
asked for, and it has a test file of its own.

That fixes the next logon. It does not fix the moment the button is pressed,
which is a different problem: the task starts a *second* process, and that
process correctly stands down when it finds this one holding the port. So
`server.serve_extra` opens the second socket in the process that was asked. One
FastAPI app, two sockets, no second ledger pool and no second event stream — and
no second pulse, because the pulse has always been a scheduled task rather than a
thread in the server. The bind is proven from outside with `_wait_for_port`
before the call returns; a bind that fails does so inside the thread, where
uvicorn logs it and exits, and a caller that only checked "did the thread start"
would go on to report the switch on.

One consequence needs its own flag. `REQUIRE_TOKEN` is per process, so arming it
mid-session would demand the token from the desktop dashboard the switch was
pressed in — a page that was open, unguarded, a moment ago, on a machine that
can read `.env` directly. `TRUST_LOOPBACK` says the narrow thing: this process
serves loopback *as well as* a network address, so a loopback peer is let
through. It is set by `serve_extra` and never by `serve`, which is not a
technicality — a server bound only to the network has no loopback socket, so a
loopback peer cannot arrive, and that server trusts the token and nothing else.
The check is the peer address from the ASGI scope. `X-Forwarded-For` is not
consulted and must not be: it is a claim made by the caller.

**Windows Firewall had no rule for the port.** This is the more interesting one,
because it is invisible by design. A dropped packet is not a refused connection:
a refusal comes back in milliseconds and the browser says so, while a drop looks
exactly like a server that is thinking about it, forever.

What makes it easy to miss is that Python ships two executables. Running the
dashboard from a terminal runs `python.exe`, and the first network bind pops the
"allow this app" box, which writes a rule for `python.exe`. Everything works. The
logon task and the desktop shortcut both run `pythonw.exe` — the windowless twin,
a different file, therefore a different rule, and one that will never be created
by a prompt, because a hidden background task has no window to prompt in front
of. The feature works when you test it from a terminal and fails on the machine
you walk away from.

`firewall.py` reads what it can unelevated and hands back the exact command for
the rest. Adding a rule needs administrator rights and the dashboard is never
going to have them — a web request that could elevate itself would be a far worse
thing than an unreachable phone — so `allow()` is called only from the CLI, where
a UAC prompt is something the person at the keyboard asked for by name. The rule
it writes is one port, TCP, inbound, private profiles: not "allow pythonw.exe",
which would open every port any Python script on this machine ever binds, on any
network it is on.

`state()` returns `open`, `blocked`, or `unknown`, and the third is not a synonym
for the second. Port filters are among the things an unelevated caller cannot
read on a locked-down machine, and telling someone their firewall is the problem
when it is not sends them off to fight the wrong thing with an admin prompt open.

Both the panel and the CLI now say this, because `serving: yes` cannot: that
probe runs on this machine, and a packet from this machine never meets the
firewall.
