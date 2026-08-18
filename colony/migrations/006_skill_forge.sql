-- M4: the forge gets a ledger.
--
-- `skills` already existed from 001 — the shape of the idea was settled before
-- there was anything to put in it. What 001 could not know is *how* a skill
-- comes to exist and how its value is proved, and both of those need columns.
--
-- Two decisions worth stating, because they are the ones that make the forge
-- auditable rather than merely automatic:
--
-- 1. **`tokens_saved` is derived, not asserted.** A single running counter that
--    only ever goes up is a number nobody can check and therefore nobody
--    believes. `skill_uses` records one row per run that loaded a skill, with
--    the baseline it was measured against, so the headline figure on the
--    dashboard can always be taken apart into the runs that produced it — and
--    a bad baseline can be corrected after the fact.
--
-- 2. **The detector that proposed a skill is kept.** When a skill turns out to
--    be worthless, the useful question is not "which skill failed" but "which
--    signal keeps proposing worthless skills". Without `detector` on the row,
--    that question has no answer six weeks from now.
--
-- ARCHITECTURE.md §7.

ALTER TABLE skills ADD COLUMN detector    TEXT;    -- which signal proposed it
ALTER TABLE skills ADD COLUMN trigger_when TEXT;   -- when a run should load it
ALTER TABLE skills ADD COLUMN draft_md    TEXT;    -- the SKILL.md body, before promotion
ALTER TABLE skills ADD COLUMN roles       TEXT;    -- JSON array; 'ordis' is a legal member
ALTER TABLE skills ADD COLUMN baseline_tokens INTEGER;  -- median chargeable for its class
ALTER TABLE skills ADD COLUMN last_used_at TEXT;
ALTER TABLE skills ADD COLUMN retired_at  TEXT;
ALTER TABLE skills ADD COLUMN retire_reason TEXT;

CREATE TABLE skill_uses (
  id              INTEGER PRIMARY KEY,
  skill_id        INTEGER NOT NULL REFERENCES skills(id),
  run_id          INTEGER REFERENCES runs(id),
  at              TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  verdict         TEXT CHECK (verdict IN ('win','loss','neutral')),
  tokens          INTEGER NOT NULL DEFAULT 0,   -- what the run actually spent
  baseline_tokens INTEGER NOT NULL DEFAULT 0,   -- what its class spent before the skill
  saved           INTEGER NOT NULL DEFAULT 0    -- baseline - tokens; may be negative
);

CREATE INDEX idx_skill_uses_skill ON skill_uses (skill_id, at DESC);
CREATE INDEX idx_skills_status ON skills (status, created_at DESC);

-- po_actions has to learn three verbs and one noun. SQLite cannot widen a CHECK
-- in place, so the table is rebuilt — 16 rows at the time of writing, and the
-- alternative (recording a promotion as a generic 'approve' on a 'colony'
-- target) would make the one action that writes a file to disk indistinguishable
-- from every other approval in the audit trail. That is exactly backwards.
CREATE TABLE po_actions_new (
  id          INTEGER PRIMARY KEY,
  at          TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  action      TEXT NOT NULL
              CHECK (action IN ('approve','reject','defer','confirm-project','halt','resume',
                                'allowance','hire','retire','dispatch','cancel','note',
                                'draft-skill','promote-skill','retire-skill')),
  target_kind TEXT CHECK (target_kind IN ('story','escalation','agent','sprint','ticket',
                                          'colony','skill')),
  target_id   INTEGER,
  detail      TEXT,
  source      TEXT NOT NULL DEFAULT 'dashboard'
);

INSERT INTO po_actions_new (id, at, action, target_kind, target_id, detail, source)
     SELECT id, at, action, target_kind, target_id, detail, source FROM po_actions;

DROP TABLE po_actions;
ALTER TABLE po_actions_new RENAME TO po_actions;
