-- Stories the PO writes here, instead of in Notion.
--
-- `stories.notion_page_id` has been nullable since 001 and the column comment
-- has said "NULL for loop-authored stories" that whole time, so the schema
-- already allowed a story with no Notion row behind it. Nothing could create
-- one: the only INSERT into `stories` anywhere in the codebase is the intake
-- path in `pulse.py`, which reads a Notion board. That made Notion a hard
-- dependency for the one operation the board exists to support -- adding work.
--
-- The sync loop iterates the rows Notion returns and touches those; it has no
-- reaper for ledger rows Notion has never heard of. A story with a NULL
-- `notion_page_id` is therefore invisible to intake rather than at risk from
-- it, which is why this needs no new column and no new flag.
--
-- All this migration does is teach `po_actions` the verb, so that filing a
-- story is an audited PO decision like every other write the dashboard makes.
-- Same rebuild dance as 006, 008, 018, 020, 021 and 023: SQLite cannot widen a
-- CHECK constraint in place.

CREATE TABLE po_actions_new (
  id          INTEGER PRIMARY KEY,
  at          TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  action      TEXT NOT NULL
              CHECK (action IN ('approve','reject','defer','amend','dismiss',
                                'confirm-project','halt','resume',
                                'allowance','hire','retire','dispatch','cancel','note',
                                'draft-skill','promote-skill','retire-skill',
                                'drop','restore','notion','pulse','scope','run',
                                'story')),
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
