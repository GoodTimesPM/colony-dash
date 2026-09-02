-- What the PO settled, kept where the next agent will read it.
--
-- The loop had a hole big enough to park the whole board in. A story that could
-- not start went to `needs-info` and raised an Inbox card naming what was
-- missing. The PO answered — in the thread, which is where the reply box put
-- them — and the reply path wrote their answer into `po_messages`, closed the card
-- as 'amend', and stopped. `po_messages` is a transcript; nothing grooms from
-- it. GROOMABLE_WHERE excludes `needs-info` on purpose, so the story stayed
-- parked, and with the card closed it was also invisible.
--
-- Every one of the eight stories on the board was in that state: blocked, with
-- no open question, waiting on an answer that had already been given. The
-- symptom the PO reported was the honest one — "there is nothing in my inbox to
-- change, just a reply in chat."
--
-- So a story gets a place to hold decisions that came from the PO rather than
-- from Notion. It is deliberately NOT `description`: the Notion sync overwrites
-- that column on every edit, and an answer that a page edit can erase is not an
-- answer the loop can build on. This column is append-only and the sync never
-- touches it.
ALTER TABLE stories ADD COLUMN po_answers TEXT;

-- The answers already given, promoted out of the transcript. Five messages on
-- "15 Part Job Search" and two on "Full computer scan" — real scoping decisions
-- that the loop paid tokens to produce and then dropped on the floor.
UPDATE stories SET po_answers = (
  SELECT group_concat(line, char(10) || char(10)) FROM (
    SELECT '[PO, ' || m.at || '] ' || m.body AS line
      FROM po_messages m
     WHERE m.story_id = stories.id AND m.author = 'po'
     ORDER BY m.id
  )
)
WHERE EXISTS (SELECT 1 FROM po_messages m
               WHERE m.story_id = stories.id AND m.author = 'po');

-- And un-park what those answers unblocked. This is the same move
-- `stale_escalations` makes when a brief changes under an open question: the
-- version of the story that was blocked no longer exists, so the block does not
-- survive it.
UPDATE stories
   SET status = 'backlog', blocked_reason = NULL,
       updated_at = datetime('now','localtime')
 WHERE status = 'needs-info' AND po_answers IS NOT NULL
   AND dropped_at IS NULL AND settled_as IS NULL;

-- Giving back the grooming attempts those runs spent. They asked a question
-- The PO has since answered; counting them against the answered version would
-- park the story at two attempts forever, which is the same permanent-block
-- failure one layer down.
UPDATE tickets
   SET status = 'wontfix', closed_at = datetime('now','localtime')
 WHERE title LIKE 'Groom:%' AND status <> 'wontfix'
   AND story_id IN (SELECT id FROM stories WHERE po_answers IS NOT NULL);
