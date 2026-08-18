-- M3.1: the Inbox becomes two-way, and "later" starts meaning something.
--
-- Every control the dashboard had until now was a button, and a button can only
-- say one of the things it was built to say. Most of what the PO actually needs
-- to answer an escalation is a sentence: "that folder doesn't exist yet, make
-- it", "merge this with the other tracker", "you've misread the story — the
-- deliverable is the CSV, not the dashboard". None of that fits in approve or
-- reject, and routing it through Notion means the answer lands nowhere near the
-- question.
--
-- So: a message table. Jordan writes into the tile; the next wake reads the
-- unread rows, answers them, and writes the answer back into the same row. The
-- escalation stays open the whole time, because a question that has been
-- *replied to* is not a question that has been *resolved*.
--
-- ARCHITECTURE.md §9.4 (the gate), §4.2 (the wake).

CREATE TABLE po_messages (
  id            INTEGER PRIMARY KEY,
  at            TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  escalation_id INTEGER REFERENCES escalations(id),
  story_id      INTEGER REFERENCES stories(id),
  -- 'po' is Jordan writing to Ordis. Kept as a column rather than implied by
  -- which fields are null, because the thread is going to be read in order and
  -- a reader should never have to infer who is speaking.
  author        TEXT NOT NULL DEFAULT 'po' CHECK (author IN ('po','ordis')),
  body          TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'unread'
                CHECK (status IN ('unread','read','answered','filed')),
  read_at       TEXT,
  read_by_pulse INTEGER REFERENCES pulses(id),
  tokens        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_po_messages_unread ON po_messages(status, at);
CREATE INDEX idx_po_messages_esc    ON po_messages(escalation_id, id);


-- "Later" used to be a no-op with a label: it recorded a `defer` and left the
-- tile looking exactly as urgent as it did before. A snooze needs an end, or it
-- is just a way of lying to yourself about an Inbox.
ALTER TABLE escalations ADD COLUMN snoozed_until TEXT;
