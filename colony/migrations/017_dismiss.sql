-- An Inbox card you can close without answering it.
--
-- "This task is not needed anymore but clicking 'drop Story' is not good for
--  the project as it could lead to canceling work/project, can there be an 'x'
--  button on the top right of each inbox tile for this reason."
--
-- The Inbox had four decisions and none of them meant "this question stopped
-- mattering". Approve lies to the loop, reject sends the story back for work
-- nobody wants, later only postpones, and "drop story" — the one control that
-- actually cleared a tile — takes the whole story off the board with it. So
-- the cheapest way to tidy the Inbox was also the most destructive thing on it.
--
-- A column rather than a fifth `po_decision`, and not to dodge the rebuild: a
-- dismissal is not an answer. `po_decision` records which of the four answers
-- the PO gave, and this file's whole point is that he gave none of them. The
-- ledger already spells that state — `resolved_at` set with `po_decision`
-- NULL is how a filed story's questions are closed as moot (`control.settle`),
-- and everything downstream already knows not to read those as instructions.
-- `dismissed_at` says which of the two moots this was: the PO closed it, or
-- the story it was about walked away.
--
-- The row survives either way, wording and `raised_hash` and all. The colony
-- was genuinely confused about this story once, and deleting the evidence
-- would make the same confusion undiagnosable the second time.
ALTER TABLE escalations ADD COLUMN dismissed_at TEXT;

-- And the ceiling the first real build breached.
--
-- 120,000 was never measured. It was the default in `propose_hire`, written
-- before anything had ever built anything, and it went unquestioned into every
-- write-capable contract. The first build spent 209,233 chargeable tokens on a
-- nine-file change and came back correct — so the ceiling was not protecting
-- a budget, it was generating a cost escalation for a run that worked. A
-- ceiling a normal, correct run breaches every time is a guarantee that the
-- Inbox fills with receipts nobody reads (4.6, and the same lesson as 002).
--
-- 400,000 is the PO's number and it is roughly twice the one measured build,
-- which is the shape of headroom that catches a runaway without punishing a
-- large-but-honest one. Read-only roles keep their smaller ceilings: grooming
-- and reviewing are bounded work and their numbers came from real runs.
UPDATE agents
   SET max_tokens_run = 400000
 WHERE write_capable = 1 AND max_tokens_run = 120000;
