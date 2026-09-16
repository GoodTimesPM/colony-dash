-- A hire card asks one question -- "is this the right person?" -- and the only
-- evidence on it is the reasoning of the agent that made the pick. Asking the
-- chooser to audit its own choice is the one check that cannot work, which is
-- why `_diversity_note` is written in Python rather than by the model.
--
-- This is the other half of that. `specialized/agents-orchestrator` is a persona
-- for doing exactly this job, and ROSTER.md's position on it stands: we do not
-- hire it, because two things picking agents is worse than one. Read on demand
-- against a pick already made, it costs one run and answers to nobody.
--
-- Stored on the escalation rather than as a story event because it is evidence
-- for one decision. When that decision is made the evidence goes with it.

ALTER TABLE escalations ADD COLUMN second_opinion TEXT;
ALTER TABLE escalations ADD COLUMN second_opinion_at TEXT;
