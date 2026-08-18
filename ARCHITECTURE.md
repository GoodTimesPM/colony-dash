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

## 5. Notion is the intake

**The board:** [Project Ideas/To-Do](https://app.notion.com/p/1d23280aadd341cbbd1467c771ee6d88)
· database `1d23280a-add3-41cb-bd14-67c771ee6d88` · data source
`9b0a4ad3-29f5-41d3-9518-ee0c3ecc9481`.

This is where you write intent from anywhere — phone, laptop, away from this machine — and
the colony picks it up on the next tick. Live schema:

| Property | Type | How the colony reads it |
| --- | --- | --- |
| `Idea` | title | → `stories.title` |
| `Status` | select — New / Exploring / In Progress / Shipped / Shelved | **the intake trigger**, below |
| `Priority` | status — Low / Medium / High | → `stories.priority` (High=1) and queue order |
| `Category` | multi-select | routing hint for which roster division to hire from |
| `Related Link` | url | context for the agent |
| *page body* | — | → `stories.description`. **This is the brief.** |

### 5.1 The intake contract

**`Status = "In Progress"` is the signal to work on it.** Nothing else is picked up.

- `New` — captured, not thought through. The colony ignores it entirely.
- `Exploring` — the colony may run **read-only research** on it if budget allows, and
  attach findings to the Notion page. It will never write code for an `Exploring` row.
- `In Progress` — **in the colony's queue.** Ordered by `Priority`.
- `Shipped` / `Shelved` — closed. The colony stops and archives its tickets.

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
| Pulse log | `pulses ORDER BY pulse_at DESC` | Heartbeat monitor. Consecutive "clean" rows collapse; a *missing* row renders red. |
| Forge | `skills WHERE status='candidate'` | Promotion queue, ranked by tokens saved. |
| Spend | `runs` rolled up by day/role/story | Burn rate, most expensive story, tokens per accepted story. |

**The design rule:** an empty PO Inbox means the system is working and needs nothing.
Everything else on the page is ambient. If the dashboard nags when the Inbox is empty, it's
wrong.

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
| M4 | **Skill forge** | Detection, drafting, promotion, tokens-saved tracking | The compounding loop turns on |
| M5 | **Two-way Notion** | Loop-authored stories and questions push up as comments | The backlog stops being two places |

**Start at M0 and M1.** They're cheap, they're safe, and a week of pulse logs is the data
that tells us whether the bar in §4.6 is set correctly and what a token actually buys —
calibrations everything downstream depends on.

### 10.1 What exists on disk (2026-08-18)

```
colony-dash/
  colony/
    migrations/  001 schema · 002 chargeable tokens · 003 structural uniqueness
                 004 M3 control — po_actions, controls, project_changes, tickets
    db.py        WAL, foreign keys, hash-checked append-only migrations
    seed.py      investigator + reviewer contracts, sprint 1
    roster.py    scans ~/.agency-agents into `roster`; FTS search
    notion.py    stdlib REST client for the intake board (read-only)
    pulse.py     the two-tier heartbeat
    wake.py      the wake tier — grooming, budget guard, retry cap
    agent.py     the only code that spends tokens: one `claude -p` invocation
    control.py   every PO decision; the only module allowed to change state
    worktree.py  a throwaway git worktree per ticket; the write blast radius
    build.py     a staffed ticket → a patch waiting for approval
    projects.py  one `git status` for the whole tree, bucketed by path
    mirror.py    full-refresh ledger → MySQL
    server.py    FastAPI, read-only connections, SSE change feed, /api/act/*
    desktop.py   pywebview shell; logs to .colony/dash.log; sets the window icon
    icon.py      the longhouse, drawn with Pillow → ui/colony.ico
    shortcut.py  writes the desktop .lnk through WScript.Shell
    ui/index.html  the whole front end — one file, no build step
    ui/colony.ico  generated; the taskbar mark
    cli.py       init / status / roster / agents / sql / pulse / mirror / dash
                 halt / resume / allowance / projects / shortcut
  pulse.cmd                     what Task Scheduler runs; logs to .colony/pulse.log
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

**Refusals are written to be read.** `control.Refused` maps to 409 with its message intact,
and every message names the state and the next move: *"story is needs-info, not ready.
Accept its acceptance criteria in the Inbox first."* An error that tells you which button
to press next is the difference between a gate and an obstacle.

**Personas are read from disk, not from the ledger.** The roster table carries names and
divisions; the drawer reads the actual `.md` file when you click. 270 persona bodies in
every snapshot would be megabytes down the SSE feed to answer a question asked once — and
reading the file means what you see is what the agent will be handed, not a copy that
drifted.
