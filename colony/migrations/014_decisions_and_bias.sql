-- Two things the PO asked for, and both of them are shapes the ledger lacked.
--
-- 1. "Basically any call/choice can be made a ticket to ensure that it has been
--    understood." A decision was an `escalations` row that flipped to resolved
--    and a line in `audit`. Neither is on the board. The Ticket Queue is where
--    The PO watches work exist, and approving criteria — the most consequential
--    thing they do all week — left nothing there at all.
--
--    So a decision gets a ticket, born closed, carrying the question on one side
--    and their answer on the other. It is not work to do; it is work that was
--    done, by them, and the Queue is the one place the colony keeps that kind of
--    record where anyone will see it.
--
--    `intent` stays `chore` rather than growing a 'decide' value, and that is a
--    deliberate retreat from the first version of this migration. Widening the
--    CHECK means rebuilding the table, and the rebuild cannot be done inside the
--    single transaction `db.migrate` gives each file: dropping the old table
--    fires an implicit DELETE that trips `runs` → `tickets` however the swap is
--    ordered, with `defer_foreign_keys` and with `legacy_alter_table` alike, and
--    `PRAGMA foreign_key_check` reports nothing wrong the entire time.
--
--    The Queue already had this problem and had already solved it: a PO reply is
--    `intent='research'` and renders as "reply" because `po_message_id` is set.
--    A decision is `intent='chore'` and renders as "decision" because this is.
ALTER TABLE tickets ADD COLUMN decided_esc_id INTEGER REFERENCES escalations(id);

CREATE INDEX idx_tickets_decision ON tickets(decided_esc_id);

-- The decisions already made, given the tickets they never got. Backfilled from
-- the escalations themselves so the Queue does not open on a history that
-- starts today — the twelve calls the PO has already made are the examples of
-- what this column is for, and they are the ones worth being able to re-read.
INSERT INTO tickets (story_id, title, intent, status, work_order, findings,
                     requires_po, created_at, closed_at, decided_esc_id)
SELECT e.story_id,
       'Decision: ' || substr(e.reason, 1, 110),
       'chore', 'done',
       e.reason || CASE WHEN e.recommendation IS NOT NULL
                        THEN char(10) || char(10) || 'Ordis recommended: ' || e.recommendation
                        ELSE '' END,
       'PO ' || e.po_decision || 'd.',
       1, e.raised_at, e.resolved_at, e.id
  FROM escalations e
 WHERE e.resolved_at IS NOT NULL AND e.po_decision IS NOT NULL;

-- 2. "I do not want to only see one agent being chosen over and over again just
--    because we found one that works. This environment needs to be diverse."
--
--    Diversity is not a rule you can put in a prompt and trust, because the
--    thing that would produce the bias is the same thing you would be asking to
--    police it. It needs a counter. Two columns on the persona, kept by `hire`,
--    so "how often has this one been picked?" has an answer in the ledger
--    rather than in an agent's recollection of the conversation — and so the
--    selection prompt can carry the count beside every candidate and be judged
--    against it afterwards.
ALTER TABLE roster ADD COLUMN times_hired   INTEGER NOT NULL DEFAULT 0;
ALTER TABLE roster ADD COLUMN last_hired_at TEXT;

UPDATE roster SET
  times_hired = (SELECT COUNT(*) FROM agents a WHERE a.roster_slug = roster.slug),
  last_hired_at = (SELECT MAX(a.hired_at) FROM agents a WHERE a.roster_slug = roster.slug)
WHERE EXISTS (SELECT 1 FROM agents a WHERE a.roster_slug = roster.slug);
