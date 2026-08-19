-- A story the PO has filed: Done, Shipped, Shelved, New or Not started.
--
-- A column of its own rather than three more values in `status`, for two
-- reasons. The first is mechanical: `status` carries a CHECK constraint, and
-- widening it in SQLite means rebuilding a table three others hold foreign keys
-- into. The second is the real one — filing and workflow are different facts.
-- `status` says where the colony had a story in its own process; `settled_as`
-- says whether the PO is asking for anything at all. Keeping them apart means
-- un-filing a story restores it exactly where it was, instead of guessing.
--
-- NULL means live. Anything else means the colony asks nothing about this row.
ALTER TABLE stories ADD COLUMN settled_as TEXT
  CHECK (settled_as IS NULL OR settled_as IN ('done','shelved','not-started'));

CREATE INDEX IF NOT EXISTS idx_stories_settled ON stories(settled_as);

-- Backfill from the Notion status already on the row. The sync only re-reads a
-- story when its content hash moves, so a row filed before this migration —
-- and then left alone, which is exactly what filing means — would otherwise
-- never be evaluated and would sit in the live columns forever.
UPDATE stories SET settled_as = CASE notion_status
    WHEN 'Done'        THEN 'done'
    WHEN 'Shipped'     THEN 'done'
    WHEN 'Shelved'     THEN 'shelved'
    WHEN 'New'         THEN 'not-started'
    WHEN 'Not started' THEN 'not-started'
  END
 WHERE notion_status IN ('Done','Shipped','Shelved','New','Not started');
