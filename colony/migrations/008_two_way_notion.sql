-- M5: Notion stops being read-only, and a story starts remembering its own past.
--
-- Three problems, one migration, because they are the same problem seen from
-- three sides: the colony had no memory of *which version* of a story it was
-- talking about.
--
-- 1. **An escalation is prose written at a moment.** "This cannot start until
--    you decide X" is true the hour it is raised and can be false an hour
--    later, because the PO went and decided X. Nothing revisited it, so the
--    Inbox kept asking for things that were already done. `raised_hash` records
--    the version of the story the question was written against and `stale_at`
--    marks the moment the story moved on. A stale escalation is not deleted —
--    the question was really asked, and deleting it would erase the fact that
--    the colony was once confused — it is flagged, sorted down, and re-asked.
--
-- 2. **A checklist has two halves and the ledger only kept one.** The Notion
--    page body was flattened to text, so `- [x] done` and `- [ ] not done` were
--    the same thing to everything downstream. `done_items` / `open_items` split
--    them, which is what lets a groom prompt say "these are finished, do not
--    raise them again" instead of handing the agent the whole brief and hoping.
--
-- 3. **Writing to Notion is I/O, and control.py does not do I/O.** Same rule
--    that made skill drafting a queued request in 007: the button records the
--    intent, the pulse performs it. An outbox also buys retries and an audit
--    trail — "I set that to Done from my phone" becomes a row rather than a
--    memory. ARCHITECTURE.md §5.4.


-- ── 1. a story remembers what is already inside it ───────────────────────────

ALTER TABLE stories ADD COLUMN done_items  TEXT;   -- JSON array: checked to-dos in the brief
ALTER TABLE stories ADD COLUMN open_items  TEXT;   -- JSON array: unchecked to-dos in the brief
ALTER TABLE stories ADD COLUMN dropped_at  TEXT;   -- set when the PO drops it; status goes 'archived'
ALTER TABLE stories ADD COLUMN drop_reason TEXT;


-- ── 2. an escalation remembers which version it was asking about ─────────────

ALTER TABLE escalations ADD COLUMN raised_hash TEXT;  -- stories.notion_hash at the time
ALTER TABLE escalations ADD COLUMN stale_at    TEXT;  -- the brief changed after this was written

CREATE INDEX idx_escalations_stale ON escalations(stale_at);


-- ── 3. the Notion outbox ─────────────────────────────────────────────────────
--
-- One row per thing the colony wants to say upward. `kind` is deliberately
-- narrow: the colony may set a status, tick a checkbox, or leave a comment, and
-- that is the whole vocabulary. It may not create rows, delete rows, or edit the
-- brief — the board is the PO's, and a loop that can rewrite its own instructions
-- is not a loop with a human gate in it.
CREATE TABLE notion_outbox (
  id            INTEGER PRIMARY KEY,
  story_id      INTEGER REFERENCES stories(id),
  page_id       TEXT NOT NULL,
  kind          TEXT NOT NULL CHECK (kind IN ('status','comment','check')),
  payload       TEXT NOT NULL,               -- JSON: {status} | {text} | {item, checked}
  queued_at     TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  sent_at       TEXT,
  attempts      INTEGER NOT NULL DEFAULT 0,
  last_error    TEXT,
  source        TEXT NOT NULL DEFAULT 'dashboard'
);
CREATE INDEX idx_notion_outbox_pending ON notion_outbox(sent_at, queued_at);


-- ── 4. two more verbs in the audit trail ─────────────────────────────────────
--
-- Same rebuild dance as 006: SQLite cannot widen a CHECK in place. 'drop' is
-- its own verb rather than a 'reject' because rejecting a proposal and dropping
-- a story off the board are different decisions, and six weeks from now the
-- difference is the whole question. 'notion' covers anything queued upward.
CREATE TABLE po_actions_new (
  id          INTEGER PRIMARY KEY,
  at          TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  action      TEXT NOT NULL
              CHECK (action IN ('approve','reject','defer','confirm-project','halt','resume',
                                'allowance','hire','retire','dispatch','cancel','note',
                                'draft-skill','promote-skill','retire-skill',
                                'drop','restore','notion')),
  target_kind TEXT CHECK (target_kind IN ('story','escalation','agent','sprint','ticket',
                                          'colony','skill')),
  target_id   INTEGER,
  detail      TEXT,
  source      TEXT NOT NULL DEFAULT 'dashboard'
);

INSERT INTO po_actions_new (id, at, action, target_kind, target_id, detail, source)
     SELECT id, at, action, target_kind, target_id, detail, source FROM po_actions;

DROP TABLE po_actions;
ALTER TABLE po_actions_new RENAME TO po_actions;
CREATE INDEX idx_po_actions_at ON po_actions(at DESC);


INSERT INTO controls (key, value, note) VALUES
  ('notion_write', '1', 'the colony may push status, comments and checkboxes back to Notion');
