-- Sprints get the instant, not just the day.
--
-- The allowance week resets Friday at 05:00 local, so a sprint measured in
-- whole dates is measuring the wrong seven days: `date(started_at) BETWEEN
-- starts_on AND ends_on` swallows the five hours before Friday's reset at one
-- end and a whole extra Friday at the other. Sprint 1 was seeded with
-- today + 7 as a placeholder and never corrected, so it has been running
-- Monday-to-Monday against a Friday-to-Friday budget the entire time.
--
-- `starts_on` / `ends_on` stay, as the date shadow of the same window, because
-- they are what a human reads on the sprint line and what the CLI prints.
-- `pulse.align_sprint` maintains both from `usage.week_window`, and every spend
-- query moves to the timestamps.
ALTER TABLE sprints ADD COLUMN starts_at TEXT;
ALTER TABLE sprints ADD COLUMN ends_at   TEXT;

-- Left NULL on purpose rather than guessed at in SQL. The real edges come from
-- the reset instant the API reports, which this file cannot see; the next tick
-- fills them in, and a NULL is honestly "not yet aligned" where a computed
-- Friday would be indistinguishable from a confirmed one.
