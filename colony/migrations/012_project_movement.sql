-- What "moved" means, written down.
--
-- `project_changes` stored a snapshot per pulse: how many files were dirty in a
-- folder at that moment. That is a *level*, and the drawer read it back as if
-- it were a *change* — a heading that said "projects that moved" over a list of
-- names and nothing else. A folder that has sat at fourteen untracked files all
-- week reads there exactly like one that gained fourteen in the last hour, and
-- The PO's question — "I didn't touch these, so why did they move?" — had no
-- answer on the page because the answer was never written down.
--
-- So: four deltas against the previous sample, the files behind them, when the
-- newest of those files was last written, and a word for who wrote it. The
-- deltas are the movement, the files are where it happened, and `moved_by` is
-- the honest half of why — the colony's only route into the working tree is a
-- patch the PO approved, so 'outside' means something on the machine that is
-- not the colony, which is what they were actually asking.
--
-- `branch` and `head_sha` already existed and were never populated; the scan
-- fills them in from now on, so a row can say what it was modified *against*.
ALTER TABLE project_changes ADD COLUMN d_added     INTEGER;
ALTER TABLE project_changes ADD COLUMN d_modified  INTEGER;
ALTER TABLE project_changes ADD COLUMN d_deleted   INTEGER;
ALTER TABLE project_changes ADD COLUMN d_untracked INTEGER;
ALTER TABLE project_changes ADD COLUMN files       TEXT;
ALTER TABLE project_changes ADD COLUMN moved_by    TEXT;
ALTER TABLE project_changes ADD COLUMN touched_at  TEXT;
