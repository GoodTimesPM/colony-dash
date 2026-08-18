-- `agents` has UNIQUE (role, project), and the two structural agents carry
-- project = NULL. In SQL, NULL is not equal to NULL — so that constraint never
-- fires for them, `ON CONFLICT DO NOTHING` never triggers, and every run of
-- `colony init` quietly appended a second investigator and a second reviewer.
--
-- It surfaced the moment migration 002 raised the investigator's ceiling: the
-- agents list came back with four rows instead of two. Left alone it would have
-- become a real problem, because an agent row is a permissions grant — duplicate
-- contracts for the same role are duplicate answers to "what may this agent
-- touch", and nothing guarantees the loop reads the one you last edited.

DELETE FROM agents
 WHERE project IS NULL
   AND id NOT IN (SELECT MIN(id) FROM agents WHERE project IS NULL GROUP BY role);

-- A partial index says what the table constraint cannot: for structural agents,
-- the role alone is the identity.
CREATE UNIQUE INDEX IF NOT EXISTS agents_structural_role
    ON agents(role) WHERE project IS NULL;
