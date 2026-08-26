-- The console gets a model and an effort level.
--
-- 024 hard-coded both, which was fine while the console was one experiment and
-- wrong the moment it became the way this program is edited. A throwaway "what
-- does this file do" and a "rewrite the scheduler" are not the same purchase,
-- and the difference between sonnet/low and opus/max on the same question is
-- most of an order of magnitude in price.
--
-- NULL means "use the module default" in both columns, so an existing row keeps
-- working without a backfill and the default can move in code later.

ALTER TABLE console_state ADD COLUMN model  TEXT;
ALTER TABLE console_state ADD COLUMN effort TEXT;
