-- The PO's own terminal, inside the dashboard.
--
-- Every other conversation in this ledger is asynchronous and rationed: the PO
-- writes to a card, the pulse picks it up an hour later, an agent with a
-- tool allowlist answers it. That is correct for the colony -- an autonomous
-- loop that can run `rm -rf` unattended at 3am is not a loop anyone should
-- own -- and it is wrong for the one case where the PO is sitting right there
-- watching, wanting to change the program itself.
--
-- So this is a second door, and it is deliberately a different door. It is
-- driven by a human keystroke, never by the pulse. It has no ticket, no
-- worktree, no write scope, no allowlist. It is the terminal, rendered in the
-- Ordis panel. `console.py` is the only module that opens it.
--
-- `epoch` is what "clear" moves. Clearing does not delete the record -- the
-- tokens were still spent and a chat that erases its own cost is a chat that
-- can lie about it -- it starts a new conversation and leaves the old turns
-- addressable by their epoch.

CREATE TABLE console_turns (
  id         INTEGER PRIMARY KEY,
  epoch      INTEGER NOT NULL,
  at         TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
  role       TEXT    NOT NULL CHECK (role IN ('po','ordis')),
  body       TEXT    NOT NULL DEFAULT '',
  status     TEXT    NOT NULL DEFAULT 'done'
                     CHECK (status IN ('pending','done','failed')),
  session_id TEXT,
  tokens     INTEGER NOT NULL DEFAULT 0,
  cost_usd   REAL    NOT NULL DEFAULT 0,
  elapsed_s  REAL,
  error      TEXT
);

CREATE INDEX ix_console_turns_epoch ON console_turns (epoch, id);

-- One row, forever. The current conversation and the CLI session it resumes.
--
-- `session_id` is what makes this a conversation rather than a series of
-- unrelated one-shot prompts: the first turn of an epoch claims a UUID with
-- `--session-id`, every turn after it passes `--resume`. Clearing drops the
-- UUID, so the next turn starts a session that has never heard of the last one.
CREATE TABLE console_state (
  id         INTEGER PRIMARY KEY CHECK (id = 1),
  epoch      INTEGER NOT NULL DEFAULT 1,
  session_id TEXT,
  cwd        TEXT
);

INSERT INTO console_state (id, epoch, session_id, cwd) VALUES (1, 1, NULL, NULL);
