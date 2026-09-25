-- `killed-over-budget` becomes `over-budget`.
--
-- Nothing kills the run. The ceiling is checked after `claude -p` exits, the
-- answer is kept, and the status only records that the run cost more than its
-- role's ceiling. "Killed" told the dashboard reader the work was thrown away.
--
-- Same rebuild as 026: SQLite cannot change a CHECK constraint in place.

CREATE TABLE runs_new (
  id                 INTEGER PRIMARY KEY,
  ticket_id          INTEGER NOT NULL REFERENCES tickets(id),
  agent_role         TEXT NOT NULL,
  model              TEXT NOT NULL,
  session_id         TEXT,
  transcript_path    TEXT,
  worktree_path      TEXT,
  started_at         TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  ended_at           TEXT,
  status             TEXT NOT NULL DEFAULT 'running'
                     CHECK (status IN ('running','ok','failed','timeout','over-budget')),
  input_tokens       INTEGER,
  output_tokens      INTEGER,
  cache_read_tokens  INTEGER,
  cache_write_tokens INTEGER,
  total_tokens       INTEGER,
  cost_usd           REAL,
  verdict            TEXT,
  chargeable_tokens  INTEGER
);

INSERT INTO runs_new (id, ticket_id, agent_role, model, session_id, transcript_path,
                      worktree_path, started_at, ended_at, status, input_tokens,
                      output_tokens, cache_read_tokens, cache_write_tokens, total_tokens,
                      cost_usd, verdict, chargeable_tokens)
SELECT id, ticket_id, agent_role, model, session_id, transcript_path,
       worktree_path, started_at, ended_at,
       CASE status WHEN 'killed-over-budget' THEN 'over-budget' ELSE status END,
       input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, total_tokens,
       cost_usd, verdict, chargeable_tokens
  FROM runs;

DROP TABLE runs;
ALTER TABLE runs_new RENAME TO runs;
CREATE INDEX idx_runs_status ON runs(status);
CREATE INDEX idx_runs_ticket ON runs(ticket_id);
