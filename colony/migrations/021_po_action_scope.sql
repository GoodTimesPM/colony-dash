-- The PO can now widen a hired agent's write scope from its contract drawer,
-- and that change is recorded like every other control. The verb is 'scope'.
--
-- Same rebuild as 020, for the same reason 018 gives: nothing references
-- `po_actions`, so the drop cannot dangle a foreign key.

CREATE TABLE po_actions_new (
  id          INTEGER PRIMARY KEY,
  at          TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  action      TEXT NOT NULL
              CHECK (action IN ('approve','reject','defer','amend','dismiss',
                                'confirm-project','halt','resume',
                                'allowance','hire','retire','dispatch','cancel','note',
                                'draft-skill','promote-skill','retire-skill',
                                'drop','restore','notion','pulse','scope')),
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
