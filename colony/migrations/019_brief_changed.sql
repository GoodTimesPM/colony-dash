-- A card for a brief that changed after the colony was done reading it.
--
-- "I added some more things to the '15 Part Job Search' project in notion
--  however within the pulse it says 'wake skipped'"
--
-- It did, and correctly. `GROOMABLE_WHERE` excludes any story that already has
-- acceptance criteria, on purpose: re-grooming an accepted story every time the
-- PO fixes a sentence would spend a model to reproduce an answer the colony
-- already has. But that rule has an edge nobody wrote down. Once a story is
-- groomed, built and accepted, editing its Notion page does NOTHING. The tick
-- reports "1 changed" in the log, and drops it. Story #1 had new scope added to
-- it and two consecutive beats reported a story edit and then stood down.
--
-- `brief-changed` is the missing path, and it is free: the tick raises it, no
-- model reads it, and `raised_hash` holds it to once per version of the brief.
-- Approving clears the criteria and puts the story back in the groom queue;
-- rejecting records that the edit was cosmetic and changes nothing.
--
-- The CHECK has to be rebuilt to admit the new word. SQLite cannot alter a
-- constraint in place, so the table is copied. Every column added since 001 is
-- carried across by name — `proposal`, `snoozed_until`, `raised_hash`,
-- `stale_at`, `dismissed_at` — and both indexes are recreated. Nothing
-- references escalations with an enforced foreign key (`PRAGMA foreign_keys` is
-- off on this ledger, and the two referencing columns are advisory), so the
-- rename is safe.
CREATE TABLE escalations_new (
  id             INTEGER PRIMARY KEY,
  ticket_id      INTEGER REFERENCES tickets(id),
  story_id       INTEGER REFERENCES stories(id),
  kind           TEXT NOT NULL
                 CHECK (kind IN ('needs-info','write-approval','decision','cost',
                                 'skill','hire','brief-changed')),
  reason         TEXT NOT NULL,
  recommendation TEXT,
  est_tokens     INTEGER,
  raised_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  resolved_at    TEXT,
  po_decision    TEXT CHECK (po_decision IN ('approve','reject','defer','amend')),
  proposal       TEXT,
  snoozed_until  TEXT,
  raised_hash    TEXT,
  stale_at       TEXT,
  dismissed_at   TEXT
);

INSERT INTO escalations_new
       (id, ticket_id, story_id, kind, reason, recommendation, est_tokens,
        raised_at, resolved_at, po_decision, proposal, snoozed_until,
        raised_hash, stale_at, dismissed_at)
SELECT  id, ticket_id, story_id, kind, reason, recommendation, est_tokens,
        raised_at, resolved_at, po_decision, proposal, snoozed_until,
        raised_hash, stale_at, dismissed_at
  FROM escalations;

DROP TABLE escalations;
ALTER TABLE escalations_new RENAME TO escalations;

CREATE INDEX idx_escalations_open  ON escalations(resolved_at, raised_at DESC);
CREATE INDEX idx_escalations_stale ON escalations(stale_at);
