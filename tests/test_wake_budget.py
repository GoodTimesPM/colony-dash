"""The wake re-checks the budget before each step, not only at the top."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from colony import db, wake


class TestWakeBudget(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = db.open_ledger(Path(tmp.name) / "ledger.db")
        self.addCleanup(self.conn.close)
        self.week = {"seven_day": 1.0}
        self.built = []

        def groom(conn, story, terms, projects):
            self.week["seven_day"] = 99.0          # this groom ate the week
            return {"story_id": story, "title": "t", "verdict": "ok", "tokens": 500}

        def build_one(conn, ticket):
            self.built.append(ticket)
            return {"tokens": 100}

        for p in (
            mock.patch.object(wake.control, "is_halted", lambda: False),
            mock.patch.object(wake, "contract", lambda conn, role: {"role": role}),
            mock.patch.object(wake.pulse_mod, "candidate_projects", lambda: []),
            mock.patch.object(wake, "answer_po", lambda conn, terms, projects: []),
            mock.patch.object(wake.forge_mod, "pending_drafts", lambda conn: []),
            mock.patch.object(wake, "stories_to_groom", lambda conn, limit: [1, 2]),
            mock.patch.object(wake, "groom_story", groom),
            mock.patch.object(wake, "staff_stories", lambda conn, terms: []),
            mock.patch.object(wake.build_mod, "pending", lambda conn, limit: ["t1"]),
            mock.patch.object(wake.build_mod, "run_one", build_one),
            mock.patch.object(wake.usage_mod, "read", lambda: dict(self.week)),
        ):
            p.start()
            self.addCleanup(p.stop)

    def test_stops_after_the_groom_that_spent_the_week(self):
        report = wake.run(self.conn, {"seven_day": 1.0})
        self.assertEqual(len(report["groomed"]), 1)
        self.assertEqual(self.built, [])
        self.assertIn("budget reached after groom 1", report["stopped"])

    def test_runs_everything_while_there_is_room(self):
        with mock.patch.object(wake, "groom_story",
                               lambda c, s, t, p: {"story_id": s, "title": "t",
                                                   "verdict": "ok", "tokens": 1}):
            report = wake.run(self.conn, {"seven_day": 1.0})
        self.assertEqual(len(report["groomed"]), 2)
        self.assertEqual(self.built, ["t1"])
        self.assertIsNone(report["stopped"])


if __name__ == "__main__":
    unittest.main()
