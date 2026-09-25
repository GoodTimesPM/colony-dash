"""The pulse's promises about Notion: a dry run sends nothing, and a real run
sends the outbox once, after its transaction has committed."""

from __future__ import annotations

import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from colony import control, db, notion, outbox, pulse


class PulseCase(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.conn = db.open_ledger(Path(self._tmp.name) / "ledger.db")
        self.addCleanup(self.conn.close)
        outbox.queue(self.conn, story_id=None, page_id="page-1", kind="comment",
                     payload={"text": "hello"})

        self.sent: list[tuple] = []
        patches = [
            mock.patch.object(control, "pulse_lock", contextlib.nullcontext),
            mock.patch.object(pulse, "check_halt", lambda: False),
            mock.patch.object(pulse, "fetch_board_rows", lambda: ([], {})),
            mock.patch.object(pulse.projects_mod, "scan", lambda since=None: []),
            mock.patch.object(pulse.usage_mod, "read", lambda: None),
            mock.patch.object(notion, "status_property_kind", lambda: "select"),
            mock.patch.object(notion, "add_comment",
                              lambda page, text: self.sent.append((page, text))),
            mock.patch("builtins.print"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _unsent(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM notion_outbox WHERE sent_at IS NULL").fetchone()[0]

    def test_a_dry_run_sends_nothing_and_leaves_the_queue(self):
        pulse.run(self.conn, dry_run=True)
        self.assertEqual(self.sent, [])
        self.assertEqual(self._unsent(), 1)

    def test_a_real_run_sends_once(self):
        pulse.run(self.conn, allow_wake=False)
        pulse.run(self.conn, allow_wake=False)
        self.assertEqual(self.sent, [("page-1", "hello")])
        self.assertEqual(self._unsent(), 0)

    def test_the_outbox_sends_after_the_transaction_commits(self):
        seen = {}

        def record(page, text):
            seen["in_tx"] = self.conn.in_transaction

        with mock.patch.object(notion, "add_comment", record):
            pulse.run(self.conn, allow_wake=False)
        self.assertIs(seen["in_tx"], False)

    def test_the_notion_fetch_happens_outside_the_transaction(self):
        seen = {}

        def fetch():
            seen["in_tx"] = self.conn.in_transaction
            return [], {}

        with mock.patch.object(pulse, "fetch_board_rows", fetch):
            pulse.run(self.conn, dry_run=True)
        self.assertIs(seen["in_tx"], False)


class TestRateLimit(unittest.TestCase):

    def test_a_429_waits_and_retries(self):
        import io
        import urllib.error

        calls = {"n": 0}

        class Ok(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def urlopen(req, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.HTTPError(req.full_url, 429, "slow down",
                                             {"Retry-After": "0"}, io.BytesIO(b"{}"))
            return Ok(b'{"ok": true}')

        with mock.patch.dict("os.environ", {"NOTION_TOKEN": "t"}), \
                mock.patch.object(notion.urllib.request, "urlopen", urlopen), \
                mock.patch.object(notion.time, "sleep") as slept:
            self.assertEqual(notion._request("/x"), {"ok": True})
        self.assertEqual(calls["n"], 2)
        slept.assert_called_once_with(0.0)


if __name__ == "__main__":
    unittest.main()
