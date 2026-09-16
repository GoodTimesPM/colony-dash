"""When the PO changes what the work is, the work has to change.

The acceptance criteria on a story are written once, at groom, and they are the
only thing a build agent reads as the job. Everything else the PO says lands as
history underneath them. That split is fine while the PO is answering questions
about the work and wrong the moment they change the work.

Story #1 is what that cost. The PO said, twice in one thread, to drop everything
else and build items 12, 14 and 15. Ordis filed both as `settled`, which writes
a line of history and leaves the criteria alone. The criteria still described
Item 8, so the next build built Item 8, found it already shipped, wrote no files
and parked the story -- four times in three hours, while the PO watched the
colony work on something they had told it to forget.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from colony import control, db, wake


class Ledger(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "proj-a").mkdir()
        self.conn = db.open_ledger(self.root / "ledger.db")
        self.addCleanup(self.conn.close)
        for p in (mock.patch.object(db, "PROJECTS_ROOT", self.root),
                  mock.patch.object(control, "ROOT_POSIX", self.root.as_posix())):
            p.start()
            self.addCleanup(p.stop)

    def story(self, status: str = "ready", criteria: str = "- build item 8") -> int:
        cur = self.conn.execute(
            "INSERT INTO stories (title, status, project, project_source, "
            "                     acceptance_criteria) "
            "VALUES ('15 Part Job Search', ?, 'proj-a', 'confirmed', ?)",
            (status, criteria))
        return int(cur.lastrowid)

    def row(self, story_id: int) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM stories WHERE id = ?",
                                 (story_id,)).fetchone()


class Rescoping(Ledger):

    def test_the_criteria_for_the_old_job_are_cleared(self):
        sid = self.story()
        control.rescope_story(self.conn, sid, "items 12, 14 and 15 only")
        after = self.row(sid)
        self.assertIsNone(after["acceptance_criteria"])
        self.assertEqual(after["status"], "needs-criteria")

    def test_the_new_scope_is_where_the_next_groom_will_read_it(self):
        sid = self.story()
        control.rescope_story(self.conn, sid, "items 12, 14 and 15 only")
        detail = self.conn.execute(
            "SELECT detail FROM story_events WHERE story_id = ? AND kind = 'decided' "
            "ORDER BY id DESC LIMIT 1", (sid,)).fetchone()["detail"]
        self.assertEqual(detail, "items 12, 14 and 15 only")

    def test_a_rescoped_story_is_groomable_again(self):
        """The point of clearing the criteria. `needs-criteria` with criteria
        still set is a story nothing reads and nothing dispatches."""
        sid = self.story()
        control.rescope_story(self.conn, sid, "items 12, 14 and 15 only")
        self.assertEqual([s["id"] for s in wake.stories_to_groom(self.conn, 10)],
                         [sid])

    def test_open_questions_about_the_old_job_are_closed(self):
        sid = self.story()
        for kind in ("decision", "needs-info", "run-request"):
            self.conn.execute(
                "INSERT INTO escalations (story_id, kind, reason) VALUES (?,?,'x')",
                (sid, kind))
        control.rescope_story(self.conn, sid, "items 12, 14 and 15 only")
        still_open = self.conn.execute(
            "SELECT COUNT(*) n FROM escalations WHERE story_id = ? AND "
            "resolved_at IS NULL", (sid,)).fetchone()["n"]
        self.assertEqual(still_open, 0)

    def test_a_running_ticket_is_left_to_finish(self):
        """It owns a worktree. Yanking it mid-run strands the worktree, and it
        cannot write anything the new criteria will not simply supersede."""
        sid = self.story()
        for status in ("open", "staffed"):
            self.conn.execute(
                "INSERT INTO tickets (story_id, title, intent, role, status) "
                "VALUES (?,'t','implement','builder',?)", (sid, status))
        control.rescope_story(self.conn, sid, "items 12, 14 and 15 only")
        left = {r["status"]: r["n"] for r in self.conn.execute(
            "SELECT status, COUNT(*) n FROM tickets WHERE story_id = ? "
            "GROUP BY status", (sid,))}
        self.assertEqual(left, {"wontfix": 1, "staffed": 1})

    def test_the_groom_attempt_ceiling_is_reset(self):
        """Old grooms answered a question about a story that no longer exists.
        Counting them means the rescoped story hits the ceiling unread."""
        sid = self.story()
        for _ in range(wake.MAX_ATTEMPTS):
            self.conn.execute(
                "INSERT INTO tickets (story_id, title, intent, role, status) "
                "VALUES (?,'Groom: x','investigate','investigator','done')", (sid,))
        control.rescope_story(self.conn, sid, "items 12, 14 and 15 only")
        self.assertEqual([s["id"] for s in wake.stories_to_groom(self.conn, 10)],
                         [sid])

    def test_an_empty_scope_is_refused(self):
        sid = self.story()
        with self.assertRaises(control.Refused):
            control.rescope_story(self.conn, sid, "   ")
        self.assertEqual(self.row(sid)["acceptance_criteria"], "- build item 8")


class OrdisReply(Ledger):
    """`answer_po` reading a `rescope` off Ordis's JSON, end to end."""

    def reply(self, sid: int, **answer) -> list[dict]:
        self.conn.execute(
            "INSERT INTO po_messages (story_id, author, body, status) "
            "VALUES (?, 'po', 'only items 12, 14 and 15', 'unread')", (sid,))
        result = mock.Mock(status="ok", chargeable_tokens=10, text="ok",
                           error=None)
        result.json_payload.return_value = {"answer": "understood", **answer}
        terms = {"role": "ordis", "model": "m", "tools_allowed": [],
                 "tools_denied": [], "max_tokens_run": 1000}
        with mock.patch.object(wake.agent, "run_ticket", return_value=result):
            return wake.answer_po(self.conn, terms, ["proj-a"])

    def test_a_rescope_in_the_reply_clears_the_criteria(self):
        sid = self.story()
        self.reply(sid, rescope="items 12, 14 and 15 only, from PROPOSAL_next_steps.md")
        self.assertIsNone(self.row(sid)["acceptance_criteria"])

    def test_settled_alone_still_leaves_the_criteria_standing(self):
        """The old behaviour, and it is correct for a fact that does not change
        the job. This is the case `rescope` had to be told apart from."""
        sid = self.story()
        self.reply(sid, settled="The PO says the key is set in .env.")
        self.assertEqual(self.row(sid)["acceptance_criteria"], "- build item 8")

    def test_a_rescope_beats_a_blocker_written_about_the_old_job(self):
        sid = self.story()
        self.reply(sid, rescope="items 12, 14 and 15 only",
                   still_blocked_on="Which OG tracker column holds the salary?")
        after = self.row(sid)
        self.assertEqual(after["status"], "needs-criteria")
        self.assertIsNone(after["blocked_reason"])

    def test_the_scope_is_written_where_the_po_can_read_it_back(self):
        sid = self.story()
        self.reply(sid, rescope="items 12, 14 and 15 only")
        self.assertIn("Scope now: items 12, 14 and 15 only",
                      self.row(sid)["po_answers"])
