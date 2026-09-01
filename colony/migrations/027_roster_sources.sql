-- Where a persona came from, so the user's own agents survive a `git pull`.
--
-- Until now `roster.sync` scanned exactly one directory: the agency-agents
-- clone at `~/.agency-agents`. That is somebody else's repository, and the
-- only place a hand-written persona could go was inside it -- where the next
-- `git pull` in that clone either clobbers it or refuses to fast-forward.
--
-- So the scan now reads two roots. The clone stays untouched and pullable, and
-- anything the PO writes lands in `~/.colony-agents/<division>/<slug>.md`,
-- which nothing upstream has ever heard of. Neither directory is inside this
-- repository, and neither is ever committed: personas are a machine's
-- furniture, not this project's source.
--
-- `source` is what tells the two apart after the scan has flattened them into
-- one table. It decides exactly one thing in the UI -- whether a persona can be
-- edited or deleted from the dashboard -- because offering an edit button for a
-- file that a `git pull` will overwrite is offering a change that silently
-- disappears.
--
-- Existing rows are 'agency': the only scanner that has ever run wrote them
-- from the clone.

ALTER TABLE roster ADD COLUMN source TEXT NOT NULL DEFAULT 'agency'
  CHECK (source IN ('agency', 'local'));

CREATE INDEX idx_roster_source ON roster(source);
