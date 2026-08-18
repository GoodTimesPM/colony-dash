-- M3: the write path.
--
-- M2 was read-only by construction. M3 is the first milestone that lets the
-- dashboard change state, so the first thing it adds is not a feature but an
-- audit trail: `po_actions` records every write the dashboard is allowed to
-- make, stamped before the change lands. A colony that can act on its own needs
-- a ledger of who told it to.
--
-- ARCHITECTURE.md §4.3-4.4 (staffing, dispatch), §8 (scopes), §9.4 (the gate).


-- Every PO decision, whatever surface it came from. Append-only in practice:
-- nothing in the codebase updates or deletes a row here.
CREATE TABLE po_actions (
  id          INTEGER PRIMARY KEY,
  at          TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  action      TEXT NOT NULL
              CHECK (action IN ('approve','reject','defer','confirm-project','halt','resume',
                                'allowance','hire','retire','dispatch','cancel','note')),
  target_kind TEXT CHECK (target_kind IN ('story','escalation','agent','sprint','ticket','colony')),
  target_id   INTEGER,
  detail      TEXT,
  source      TEXT NOT NULL DEFAULT 'dashboard'
);
CREATE INDEX idx_po_actions_at ON po_actions(at DESC);


-- Colony-wide switches the PO can flip. Deliberately a key/value table rather
-- than more columns on `sprints`: a switch is not a property of a sprint, and
-- the set of switches will grow faster than the schema should.
--
-- HALT is the exception and lives on disk (.colony/HALT) as well, because the
-- one moment you most need to stop dispatch is the moment something is wrong,
-- and a file cannot be blocked by a locked database.
CREATE TABLE controls (
  key        TEXT PRIMARY KEY,
  value      TEXT,
  note       TEXT,
  updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);


-- The pulse log's long form. `finding` is the one-line summary the log shows;
-- `detail` is everything the tick actually looked at, so "what changed this
-- hour" has an answer better than "clean".
ALTER TABLE pulses ADD COLUMN detail TEXT;


-- What the colony saw in the project folders. One row per project per pulse
-- that found a change — a working tree that moved, a commit that landed. This
-- is what makes the pulse log worth reading between wakes, and it costs nothing
-- but a `git status` per project.
CREATE TABLE project_changes (
  id            INTEGER PRIMARY KEY,
  pulse_id      INTEGER REFERENCES pulses(id),
  project       TEXT NOT NULL,
  branch        TEXT,
  head_sha      TEXT,
  dirty_files   INTEGER NOT NULL DEFAULT 0,
  added         INTEGER NOT NULL DEFAULT 0,
  modified      INTEGER NOT NULL DEFAULT 0,
  deleted       INTEGER NOT NULL DEFAULT 0,
  untracked     INTEGER NOT NULL DEFAULT 0,
  commits_since INTEGER NOT NULL DEFAULT 0,
  summary       TEXT,
  seen_at       TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX idx_project_changes ON project_changes(project, seen_at DESC);


-- Staffing paperwork the hiring escalation needs to survive a restart: the
-- proposal is written down when it is raised, not reconstructed when it is
-- approved. An approval that has to re-derive what it is approving is an
-- approval of something else.
ALTER TABLE escalations ADD COLUMN proposal TEXT;   -- JSON

ALTER TABLE tickets ADD COLUMN approved_at TEXT;
ALTER TABLE agents  ADD COLUMN notes TEXT;
ALTER TABLE agents  ADD COLUMN hired_from TEXT;    -- 'roster' | 'structural' | 'dashboard'

INSERT INTO controls (key, value, note) VALUES
  ('halt', '0', 'dispatch disabled colony-wide'),
  ('allowance_boost', '0', 'percentage points added to the sprint allowance for this week');
