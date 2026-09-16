-- One write-capable agent per project, forever. That was never a decision
-- anybody made; it fell out of `stories_to_staff` skipping any project that
-- already had a writer, and it meant a folder could hold exactly one specialist
-- for the rest of its life. Wanting a second one meant retiring the first.
--
-- The `agents` table already supported a team -- UNIQUE (role, project) refuses
-- a duplicate role, not a second person -- so the contract layer needed nothing.
-- What it lacked was a way to say WHICH story a contract was cut for, which is
-- what lets the guard move from "this folder has somebody" to "this piece of
-- work has somebody" and lets dispatch hand a ticket to the right seat.

ALTER TABLE agents ADD COLUMN story_id INTEGER REFERENCES stories(id);

-- 0 is the lead: the seat that receives the implement ticket. Higher numbers are
-- specialists hired alongside them for the same story. Deliberately an integer
-- rather than a boolean, because the ordering is the answer to "who writes" and
-- a second boolean would only push that question somewhere else.
ALTER TABLE agents ADD COLUMN seat INTEGER NOT NULL DEFAULT 0;

-- Backfill. Every hire the colony has ever made came through an approved `hire`
-- escalation, and that escalation records the story it was raised on plus the
-- exact proposal it approved. Matching on role AND project is what makes this
-- safe: `UNIQUE (role, project)` guarantees at most one agent row per pair, so
-- no agent can pick up a story that belongs to a different contract.
--
-- Without this, the moment the guard becomes story-scoped, every story that
-- already has a working agent reads as unstaffed and the next pulse pays for a
-- hire nobody asked for.
UPDATE agents
   SET story_id = (
        SELECT e.story_id FROM escalations e
         WHERE e.kind = 'hire'
           AND e.po_decision = 'approve'
           AND e.story_id IS NOT NULL
           AND json_extract(e.proposal, '$.role')    = agents.role
           AND json_extract(e.proposal, '$.project') IS agents.project
         ORDER BY e.id DESC LIMIT 1)
 WHERE project IS NOT NULL;

CREATE INDEX IF NOT EXISTS agents_story ON agents(story_id) WHERE story_id IS NOT NULL;
