"""Filing a story without Notion.

Intake used to have exactly one door, and it was on someone else's server. These
tests pin the second door open: a story filed here is a normal ledger story, a
named folder is a *confirmed* folder rather than a guess, and an unnamed one
raises the same question intake would have raised.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from colony import control, db, projects


class IntakeCase(unittest.TestCase):
    """A temporary ledger and a temporary projects root with one real folder."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "real-project").mkdir()
        (self.root / "real-project" / "PROJECT.md").write_text("# real", encoding="utf-8")
        self.conn = db.open_ledger(self.root / "ledger.db")

        self._patches = [
            mock.patch.object(db, "PROJECTS_ROOT", self.root),
            mock.patch.object(control, "ROOT_POSIX", self.root.as_posix()),
            mock.patch.object(projects, "ROOT", self.root),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self.conn.close()
        self._tmp.cleanup()

    def story(self, story_id: int) -> dict:
        return dict(self.conn.execute(
            "SELECT * FROM stories WHERE id = ?", (story_id,)).fetchone())


class TestCreateStory(IntakeCase):

    def test_a_filed_story_has_no_notion_page(self):
        """The whole point: a story the Notion sync has never heard of."""
        out = control.create_story(self.conn, title="Ship the thing",
                                   project="real-project")
        row = self.story(out["story_id"])

        self.assertIsNone(row["notion_page_id"])
        self.assertEqual(row["title"], "Ship the thing")
        self.assertEqual(row["status"], "backlog")

    def test_a_named_folder_is_confirmed_not_inferred(self):
        """`project_source` is what gates a write scope, so this is the
        difference that matters between this path and the Notion one."""
        out = control.create_story(self.conn, title="Ship it", project="real-project")
        row = self.story(out["story_id"])

        self.assertEqual(row["project"], "real-project")
        self.assertEqual(row["project_source"], "confirmed")

    def test_no_folder_raises_the_needs_info_question(self):
        out = control.create_story(self.conn, title="Something vague")
        row = self.story(out["story_id"])
        self.assertIsNone(row["project"])

        esc = self.conn.execute(
            "SELECT * FROM escalations WHERE story_id = ? AND resolved_at IS NULL",
            (out["story_id"],)).fetchall()
        self.assertEqual(len(esc), 1)
        self.assertEqual(esc[0]["kind"], "needs-info")

    def test_a_folder_that_does_not_exist_is_refused(self):
        """Unlike `confirm_project`, which may name a folder yet to be created.
        Here the typo would become the confirmed write scope immediately."""
        with self.assertRaises(control.Refused) as caught:
            control.create_story(self.conn, title="Typo", project="rael-project")
        self.assertIn("no folder named", str(caught.exception))

    def test_traversal_in_the_folder_is_refused(self):
        for bad in ("../elsewhere", "real-project/../..", "C:/Windows"):
            with self.subTest(bad=bad), self.assertRaises(control.Refused):
                control.create_story(self.conn, title="Nope", project=bad)

    def test_an_empty_title_is_refused(self):
        for bad in ("", "   ", "\n\t"):
            with self.subTest(bad=bad), self.assertRaises(control.Refused):
                control.create_story(self.conn, title=bad)

    def test_the_same_title_twice_in_a_minute_is_refused(self):
        """The double-tap guard. A phone submits a form twice far more often
        than a desktop does."""
        first = control.create_story(self.conn, title="Double tap",
                                     project="real-project")
        with self.assertRaises(control.Refused) as caught:
            control.create_story(self.conn, title="Double tap", project="real-project")
        self.assertIn(str(first["story_id"]), str(caught.exception))

    def test_a_bad_priority_is_refused_and_a_good_one_is_kept(self):
        with self.assertRaises(control.Refused):
            control.create_story(self.conn, title="Priority nine", priority=9)

        out = control.create_story(self.conn, title="Priority one",
                                   project="real-project", priority=1)
        self.assertEqual(self.story(out["story_id"])["priority"], 1)

    def test_the_brief_is_kept_and_the_event_is_written(self):
        out = control.create_story(self.conn, title="With a brief",
                                   description="  the whole reason  ",
                                   project="real-project")
        row = self.story(out["story_id"])
        self.assertEqual(row["description"], "the whole reason")

        events = self.conn.execute(
            "SELECT * FROM story_events WHERE story_id = ?",
            (out["story_id"],)).fetchall()
        self.assertEqual([e["kind"] for e in events], ["created"])

    def test_filing_is_recorded_as_a_po_action(self):
        """Every write the dashboard makes lands in `po_actions` before it lands
        anywhere else. A new verb that skips the audit trail is a new hole."""
        out = control.create_story(self.conn, title="Audited",
                                   project="real-project")
        act = self.conn.execute(
            "SELECT * FROM po_actions WHERE action = 'story'").fetchone()

        self.assertIsNotNone(act, "migration 026 should have taught the verb")
        self.assertEqual(act["target_kind"], "story")
        self.assertEqual(act["target_id"], out["story_id"])
        self.assertEqual(act["detail"], "Audited")

    def test_a_long_title_is_trimmed_rather_than_refused(self):
        out = control.create_story(self.conn, title="x" * 500,
                                   project="real-project")
        self.assertEqual(len(self.story(out["story_id"])["title"]),
                         control.MAX_TITLE)


if __name__ == "__main__":
    unittest.main()
