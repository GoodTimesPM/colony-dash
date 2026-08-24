-- The forced-beat buttons could not record themselves, so they returned 500.
--
-- `control.force_pulse` writes the action down before starting the beat, the
-- same as every other control, and passes 'pulse' as the action name. That verb
-- is not in the CHECK on this column, so the INSERT failed with an integrity
-- error, the endpoint returned 500, and the dashboard showed "refused (500)".
-- The thread never started, so no beat ran and nothing reached the pulse log.
--
-- 'pulse' goes in the list. The rest of the table is unchanged from 018.
--
-- Safe to rebuild plainly, for the reason 018 gives: nothing references
-- `po_actions`, so the drop cannot dangle a foreign key.

CREATE TABLE po_actions_new (
  id          INTEGER PRIMARY KEY,
  at          TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  action      TEXT NOT NULL
              CHECK (action IN ('approve','reject','defer','amend','dismiss',
                                'confirm-project','halt','resume',
                                'allowance','hire','retire','dispatch','cancel','note',
                                'draft-skill','promote-skill','retire-skill',
                                'drop','restore','notion','pulse')),
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
CREATE INDEX idx_po_actions_at ON po_actions(at DESC);
