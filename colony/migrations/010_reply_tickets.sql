-- A reply to Ordis becomes a ticket the moment it is written, not when the wake
-- gets around to it.
--
-- Before this, `wake.answer_po` created the ticket, ran it and closed it inside
-- one wake, so the Ticket Queue never showed it: the row was born `staffed` and
-- died `done` between two page loads. The PO's experience was that answering a
-- question sent it nowhere. Queueing the ticket at write time makes the wait
-- visible — the same thing the outbox does for a Notion push, for the same
-- reason.
--
-- The link is a column rather than a title convention because the wake has to
-- find the ticket it was handed, and matching on `title LIKE 'Answer the PO:%'`
-- would pair the wrong reply with the wrong ticket the first time two questions
-- start with the same sentence.
ALTER TABLE tickets ADD COLUMN po_message_id INTEGER REFERENCES po_messages(id);

CREATE INDEX IF NOT EXISTS idx_tickets_po_message ON tickets(po_message_id);
