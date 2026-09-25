"""A deferred card snoozes for the hours asked, fractions included."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from colony import control, db


class Snooze(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.conn = db.open_ledger(Path(self._tmp.name) / "ledger.db")
        self.addCleanup(self.conn.close)
        sid = self.conn.execute(
            "INSERT INTO stories (title, status) VALUES ('s', 'ready')").lastrowid
        self.esc = self.conn.execute(
            "INSERT INTO escalations (story_id, kind, reason) VALUES (?, 'needs-info', 'why')",
            (sid,)).lastrowid

    def test_an_hour_and_a_half_is_ninety_minutes(self):
        out = control.decide(self.conn, self.esc, "defer", "", 1.5)
        self.assertIn("1.5h", out["outcome"])
        row = self.conn.execute(
            "SELECT raised_at, snoozed_until FROM escalations WHERE id = ?",
            (self.esc,)).fetchone()
        gap = (datetime.fromisoformat(row["snoozed_until"])
               - datetime.fromisoformat(row["raised_at"]))
        self.assertAlmostEqual(gap.total_seconds(), 90 * 60, delta=2)


if __name__ == "__main__":
    unittest.main()
