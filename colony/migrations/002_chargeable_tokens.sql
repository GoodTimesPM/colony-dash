-- The first real agent run cost 394,844 tokens against a 60,000 ceiling — but
-- 333,382 of those were cache *reads*: the same context re-read on every turn,
-- already paid for once when it was written. Charging it again counts one file
-- read twenty times, which makes any ceiling meaningless within a single turn.
--
-- So the ledger now keeps both numbers and is explicit about which is which:
--   total_tokens      every token that moved, cache included. The honest sum.
--   chargeable_tokens input + output + cache writes. What the budget is spent
--                     against, and what a per-run ceiling is measured on.
--
-- The four raw counters stay, so either figure can be recomputed later.

ALTER TABLE runs ADD COLUMN chargeable_tokens INTEGER;

-- Calibration from that same run: grooming one story cost 61,462 chargeable
-- tokens, just over the investigator's original 60,000 ceiling. A ceiling that
-- a normal, correct run breaches every time is not a guard — it is a guarantee
-- that the Inbox fills with cost escalations nobody reads (§4.6). Raised to
-- 100,000, which is real headroom over a measured run rather than a guess.
UPDATE agents SET max_tokens_run = 100000
 WHERE role = 'investigator' AND project IS NULL AND max_tokens_run = 60000;

-- Backfill the one run that predates the split.
UPDATE runs
   SET chargeable_tokens = COALESCE(input_tokens, 0)
                         + COALESCE(output_tokens, 0)
                         + COALESCE(cache_write_tokens, 0)
 WHERE chargeable_tokens IS NULL;
