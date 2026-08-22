-- `po_actions.action` did not know about two of the decisions it records.
--
-- Every PO action is written down before it takes effect — rule 1 of
-- `control.py` — through `_record(conn, decision, ...)`, which passes the
-- decision straight through as the action name. The CHECK on this column lists
-- approve, reject and defer, and stops there. So `amend` has never been
-- writable: `control.decide(esc, "amend")` accepts the argument, gets as far as
-- recording it, and dies on a constraint. Nothing calls it that way yet, which
-- is the only reason it has never been seen; `wake` sets `po_decision='amend'`
-- with its own UPDATE and never goes through the door this guards.
--
-- `dismiss` is the new one, and it would have hit the same wall. Both go in.
--
-- Safe to rebuild plainly: nothing anywhere references `po_actions`, so the
-- drop cannot dangle a foreign key — which is exactly what stopped 017 from
-- widening a CHECK the same way. (`PRAGMA foreign_keys = OFF` would not have
-- helped 017 either: it is a no-op inside a transaction, and every migration
-- runs inside one.)

CREATE TABLE po_actions_new (
  id          INTEGER PRIMARY KEY,
  at          TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  action      TEXT NOT NULL
              CHECK (action IN ('approve','reject','defer','amend','dismiss',
                                'confirm-project','halt','resume',
                                'allowance','hire','retire','dispatch','cancel','note',
                                'draft-skill','promote-skill','retire-skill',
                                'drop','restore','notion')),
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
