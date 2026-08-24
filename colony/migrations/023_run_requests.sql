-- A build agent can now ask for a command to be run, and Ordis runs it.
--
-- "if a build agent cant run `py -m apply.main auto` against a real Notion row
--  then ordis can, that is the 'team' aspect of this whole operation"
--
-- A build agent has Read, Grep, Glob, Edit and Write and nothing else. That is
-- deliberate: a shell is `git push`, `rm -rf` and `curl | sh`, and none of
-- those should exist inside an unattended run. The cost of the rule is that
-- any criterion phrased "run X and confirm Y" was unanswerable, and the agent
-- had no way to say so except by skipping it.
--
-- So the agent stops guessing and hands the command over. `needs_run` in its
-- reply becomes a `run-request` card: the command as written, the reason, and
-- what the agent expects to see. Jordan reads it and either runs it or does
-- not. The output goes back on the story as an event, so the next build reads
-- what happened rather than asking again.
--
-- Two constraints have to admit a new word, and SQLite cannot alter a CHECK in
-- place, so both tables are rebuilt. The escalations rebuild follows 019 and
-- carries every column added since 001; the po_actions rebuild follows 020.
-- Nothing references po_actions at all. Escalations is referenced, which 019
-- got wrong and got away with; see the note above the pragma.

-- `tickets` and `po_messages` both hold a foreign key into `escalations`, so
-- dropping it trips the constraint even though the rename puts every reference
-- back. `db.migrate` runs each migration with `PRAGMA foreign_keys = OFF` and
-- checks `foreign_key_check` afterwards, which is what SQLite's own procedure
-- for this kind of change prescribes. Nothing is needed here.

CREATE TABLE escalations_new (
  id             INTEGER PRIMARY KEY,
  ticket_id      INTEGER REFERENCES tickets(id),
  story_id       INTEGER REFERENCES stories(id),
  kind           TEXT NOT NULL
                 CHECK (kind IN ('needs-info','write-approval','decision','cost',
                                 'skill','hire','brief-changed','run-request')),
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

CREATE TABLE po_actions_new (
  id          INTEGER PRIMARY KEY,
  at          TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  action      TEXT NOT NULL
              CHECK (action IN ('approve','reject','defer','amend','dismiss',
                                'confirm-project','halt','resume',
                                'allowance','hire','retire','dispatch','cancel','note',
                                'draft-skill','promote-skill','retire-skill',
                                'drop','restore','notion','pulse','scope','run')),
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
