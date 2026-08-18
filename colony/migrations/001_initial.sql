-- Colony Dash ledger, initial schema.
-- See ARCHITECTURE.md §3 for why these boundaries are where they are.
--
-- Conventions:
--   * timestamps are ISO-8601 TEXT in local time ('YYYY-MM-DD HH:MM:SS')
--   * every enum-ish column carries a CHECK, because a ledger with a typo'd
--     status is a ledger you stop trusting
--   * tokens are the unit; every *_usd column is notional (a Pro plan is not
--     metered in money) and exists only as a familiarity anchor for the UI


-- A week of intent. One sprint = one 7-day usage window.
CREATE TABLE sprints (
  id            INTEGER PRIMARY KEY,
  name          TEXT NOT NULL,
  goal          TEXT,
  starts_on     TEXT NOT NULL,
  ends_on       TEXT NOT NULL,              -- aligned to the Anthropic 7-day reset
  budget_pct    REAL NOT NULL DEFAULT 35.0, -- max % of the weekly window the colony may burn
  budget_tokens INTEGER,                    -- calibrated equivalent, filled in from real data
  status        TEXT NOT NULL DEFAULT 'planning'
                CHECK (status IN ('planning','active','closed')),
  created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);


-- A backlog item, mirrored from the Notion board. Intent lives here.
CREATE TABLE stories (
  id                  INTEGER PRIMARY KEY,
  notion_page_id      TEXT UNIQUE,          -- sync key; NULL for loop-authored stories
  sprint_id           INTEGER REFERENCES sprints(id),
  title               TEXT NOT NULL,
  description         TEXT,                 -- the Notion page body: this is the brief
  acceptance_criteria TEXT,                 -- Ordis drafts, PO approves
  project             TEXT,                 -- folder under D:\ALL STUFF\PROJECTS
  project_source      TEXT NOT NULL DEFAULT 'inferred'
                      CHECK (project_source IN ('inferred','confirmed')),
  notion_status       TEXT,                 -- raw Notion Status, for audit
  category            TEXT,                 -- raw Notion Category (JSON array)
  related_link        TEXT,
  priority            INTEGER NOT NULL DEFAULT 3,   -- 1 High, 2 Medium, 3 Low
  est_tokens          INTEGER,
  est_cost_usd        REAL,
  status              TEXT NOT NULL DEFAULT 'backlog'
                      CHECK (status IN ('backlog','needs-info','needs-criteria','ready',
                                        'in-progress','po-review','accepted','rejected','archived')),
  blocked_reason      TEXT,
  created_at          TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  updated_at          TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  notion_synced_at    TEXT,
  notion_hash         TEXT                  -- content hash, so an unchanged row costs nothing
);
CREATE INDEX idx_stories_status   ON stories(status);
CREATE INDEX idx_stories_priority ON stories(priority, status);


-- A unit of agent work. Stories fan out into tickets.
CREATE TABLE tickets (
  id                INTEGER PRIMARY KEY,
  story_id          INTEGER REFERENCES stories(id),
  parent_ticket_id  INTEGER REFERENCES tickets(id),
  title             TEXT NOT NULL,
  intent            TEXT NOT NULL
                    CHECK (intent IN ('investigate','implement','review','research','chore')),
  role              TEXT,                   -- FK-ish to agents.role, deliberately soft
  status            TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open','staffed','running','blocked','done','wontfix')),
  severity          TEXT CHECK (severity IN ('trivial','minor','major','critical')),
  work_order        TEXT,                   -- the literal prompt handed to the agent
  findings          TEXT,
  artifact_path     TEXT,                   -- diff, report, or file the run produced
  write_scope       TEXT,                   -- the one folder this ticket may modify
  requires_po       INTEGER NOT NULL DEFAULT 0,
  est_tokens        INTEGER,
  created_at        TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  closed_at         TEXT
);
CREATE INDEX idx_tickets_status ON tickets(status);
CREATE INDEX idx_tickets_story  ON tickets(story_id);


-- One agent invocation. The cost record.
CREATE TABLE runs (
  id                 INTEGER PRIMARY KEY,
  ticket_id          INTEGER NOT NULL REFERENCES tickets(id),
  agent_role         TEXT NOT NULL,
  model              TEXT NOT NULL,
  session_id         TEXT,                  -- Claude Code session id, for replay
  transcript_path    TEXT,
  worktree_path      TEXT,                  -- write-capable runs never touch the live tree
  started_at         TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  ended_at           TEXT,
  status             TEXT NOT NULL DEFAULT 'running'
                     CHECK (status IN ('running','ok','failed','timeout','killed-over-budget')),
  input_tokens       INTEGER,
  output_tokens      INTEGER,
  cache_read_tokens  INTEGER,
  cache_write_tokens INTEGER,
  total_tokens       INTEGER,               -- the headline number on the dashboard
  cost_usd           REAL,                  -- notional
  verdict            TEXT
);
CREATE INDEX idx_runs_status ON runs(status);
CREATE INDEX idx_runs_ticket ON runs(ticket_id);


-- The heartbeat log. One row per tick, findings or not.
CREATE TABLE pulses (
  id            INTEGER PRIMARY KEY,
  pulse_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  tier          TEXT NOT NULL CHECK (tier IN ('tick','wake')),
  window_start  TEXT NOT NULL,
  window_end    TEXT NOT NULL,
  actions       TEXT,                       -- JSON: what it did this tick
  finding       TEXT,                       -- human-readable; literally 'clean' when nothing
  anomalies     INTEGER NOT NULL DEFAULT 0,
  tokens        INTEGER NOT NULL DEFAULT 0, -- 0 for a tick, by design
  duration_ms   INTEGER,
  next_pulse_at TEXT
);
CREATE INDEX idx_pulses_at ON pulses(pulse_at DESC);


-- Sampled from the shared usage cache the tray app writes. One poller, not two.
CREATE TABLE usage_samples (
  id                  INTEGER PRIMARY KEY,
  sampled_at          TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  five_hour_pct       REAL,
  seven_day_pct       REAL,
  seven_day_resets_at TEXT,
  source_mtime        TEXT                  -- cache file mtime, so a stale file is visible
);
CREATE INDEX idx_usage_at ON usage_samples(sampled_at DESC);


-- Things that need Jordan. The only table that demands attention.
CREATE TABLE escalations (
  id             INTEGER PRIMARY KEY,
  ticket_id      INTEGER REFERENCES tickets(id),
  story_id       INTEGER REFERENCES stories(id),
  kind           TEXT NOT NULL
                 CHECK (kind IN ('needs-info','write-approval','decision','cost','skill','hire')),
  reason         TEXT NOT NULL,
  recommendation TEXT,
  est_tokens     INTEGER,
  raised_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  resolved_at    TEXT,
  po_decision    TEXT CHECK (po_decision IN ('approve','reject','defer','amend'))
);
CREATE INDEX idx_escalations_open ON escalations(resolved_at, raised_at DESC);


-- Learned procedure. Memory is facts; a skill is a procedure. See ARCHITECTURE.md §7.
CREATE TABLE skills (
  id            INTEGER PRIMARY KEY,
  name          TEXT NOT NULL,
  slug          TEXT UNIQUE NOT NULL,
  status        TEXT NOT NULL DEFAULT 'candidate'
                CHECK (status IN ('candidate','drafted','active','retired')),
  summary       TEXT,
  evidence_runs TEXT,                       -- JSON array of run ids
  path          TEXT,                       -- .claude/skills/<slug>/SKILL.md once promoted
  times_used    INTEGER NOT NULL DEFAULT 0,
  wins          INTEGER NOT NULL DEFAULT 0,
  losses        INTEGER NOT NULL DEFAULT 0,
  tokens_saved  INTEGER NOT NULL DEFAULT 0, -- a skill's value, in the same currency as the budget
  created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  promoted_at   TEXT
);


-- The hiring pool: personas scanned out of ~/.agency-agents. Résumés, not employees.
-- Nothing here can run. Hiring copies a row into `agents` with a contract attached.
CREATE TABLE roster (
  id           INTEGER PRIMARY KEY,
  slug         TEXT UNIQUE NOT NULL,        -- division/filename, e.g. engineering/backend-architect
  name         TEXT NOT NULL,
  division     TEXT NOT NULL,
  description  TEXT,
  emoji        TEXT,
  color        TEXT,
  vibe         TEXT,
  path         TEXT NOT NULL,
  body_hash    TEXT NOT NULL,               -- upstream `git pull` changing a persona is visible
  scanned_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX idx_roster_division ON roster(division);

-- Full-text search over the roster, so the standby browser's search bar is instant
-- even at 255 personas.
CREATE VIRTUAL TABLE roster_fts USING fts5(
  slug, name, division, description, vibe,
  content='roster', content_rowid='id'
);
CREATE TRIGGER roster_ai AFTER INSERT ON roster BEGIN
  INSERT INTO roster_fts(rowid, slug, name, division, description, vibe)
  VALUES (new.id, new.slug, new.name, new.division, new.description, new.vibe);
END;
CREATE TRIGGER roster_ad AFTER DELETE ON roster BEGIN
  INSERT INTO roster_fts(roster_fts, rowid, slug, name, division, description, vibe)
  VALUES ('delete', old.id, old.slug, old.name, old.division, old.description, old.vibe);
END;
CREATE TRIGGER roster_au AFTER UPDATE ON roster BEGIN
  INSERT INTO roster_fts(roster_fts, rowid, slug, name, division, description, vibe)
  VALUES ('delete', old.id, old.slug, old.name, old.division, old.description, old.vibe);
  INSERT INTO roster_fts(rowid, slug, name, division, description, vibe)
  VALUES (new.id, new.slug, new.name, new.division, new.description, new.vibe);
END;


-- Hired agents: a persona plus the governance the persona file does not carry.
-- Contracts are per-project by design — the same persona can be hired twice with
-- different write scopes and different ceilings.
CREATE TABLE agents (
  id              INTEGER PRIMARY KEY,
  role            TEXT NOT NULL,            -- how the colony refers to them, e.g. backend-dev
  project         TEXT,                     -- NULL = structural (investigator, reviewer)
  roster_slug     TEXT REFERENCES roster(slug),   -- NULL for hand-written structural roles
  model           TEXT NOT NULL DEFAULT 'claude-sonnet-5',
  write_capable   INTEGER NOT NULL DEFAULT 0,
  tools_allowed   TEXT NOT NULL,            -- JSON array
  tools_denied    TEXT,                     -- JSON array
  read_scope      TEXT NOT NULL,            -- JSON array of globs
  write_scope     TEXT,                     -- JSON array of globs; NULL when not write_capable
  skills          TEXT,                     -- JSON array of skill slugs
  max_tokens_run  INTEGER NOT NULL DEFAULT 120000,
  definition_of_done TEXT,                  -- JSON array
  avatar_seed     TEXT NOT NULL,            -- deterministic sprite seed for the Colony panel
  status          TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active','standby','retired')),
  hired_at        TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  retired_at      TEXT,
  UNIQUE (role, project)
);


-- Append-only per-story timeline. This is what a click on the board opens:
-- not just current status, but everything that happened and everything learned.
CREATE TABLE story_events (
  id         INTEGER PRIMARY KEY,
  story_id   INTEGER NOT NULL REFERENCES stories(id),
  ticket_id  INTEGER REFERENCES tickets(id),
  run_id     INTEGER REFERENCES runs(id),
  at         TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  kind       TEXT NOT NULL
             CHECK (kind IN ('created','synced','groomed','blocked','staffed','dispatched',
                             'finding','learning','escalated','decided','accepted','note')),
  summary    TEXT NOT NULL,
  detail     TEXT,                          -- markdown, rendered in the detail drawer
  tokens     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_story_events ON story_events(story_id, at DESC);
