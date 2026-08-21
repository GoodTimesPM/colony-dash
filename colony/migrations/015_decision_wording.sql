-- The four decisions, past-tensed properly.
--
-- 014 backfilled a ticket for every call Jordan has ever made, and wrote the
-- answer as 'PO ' || po_decision || 'd.' — which is right for exactly one of the
-- four values that column allows. The rows already said "PO amendd." and
-- "PO rejectd.", and a decision ticket exists to be read years later, so the
-- record it holds should be a sentence. `story_events` carried the same typo
-- from the same source; both are repaired here and the generator in
-- `control._PAST` no longer produces it.
UPDATE tickets SET findings = 'PO rejected.'
 WHERE decided_esc_id IS NOT NULL AND findings = 'PO rejectd.';
UPDATE tickets SET findings = 'PO amended.'
 WHERE decided_esc_id IS NOT NULL AND findings = 'PO amendd.';
UPDATE tickets SET findings = 'PO deferred.'
 WHERE decided_esc_id IS NOT NULL AND findings = 'PO deferd.';

UPDATE story_events SET summary = 'PO rejected: ' || substr(summary, 13)
 WHERE kind = 'decided' AND summary LIKE 'PO rejectd: %';
UPDATE story_events SET summary = 'PO amended: ' || substr(summary, 12)
 WHERE kind = 'decided' AND summary LIKE 'PO amendd: %';
UPDATE story_events SET summary = 'PO deferred: ' || substr(summary, 12)
 WHERE kind = 'decided' AND summary LIKE 'PO deferd: %';
