"""The console's one-turn-at-a-time lock.

The console is the deliberate exception to the colony's safety model: a real
shell, no allowlist, no worktree. What keeps that honest is that exactly one
turn may be in flight, two shells writing one tree is the failure this whole
system exists to prevent, so the lock is the thing worth pinning down.

Nothing here spawns `claude`. `console._answer` is replaced with a stub that
blocks until the test lets it go, which is what "a turn in flight" means from
`send`'s point of view.
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from colony import console, db


class ConsoleCase(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = db.open_ledger(Path(self._tmp.name) / "ledger.db")
        self.launched: list[tuple] = []
        self.release = threading.Event()
        console._running = False

        def stub_answer(*args, **kwargs):
            """Stand in for the thread that would run `claude`."""
            self.launched.append(args)
            self.release.wait(timeout=10)
            with console._lock:
                console._running = False

        self._patch = mock.patch.object(console, "_answer", stub_answer)
        self._patch.start()

    def tearDown(self) -> None:
        self.release.set()
        self._patch.stop()
        console._running = False
        self.conn.close()
        self._tmp.cleanup()

    def finish(self, turn_id: int) -> None:
        """What `_answer` would have done to the row, without running a turn."""
        self.release.set()
        self.conn.execute("UPDATE console_turns SET status = 'done', body = 'ok' "
                          "WHERE id = ?", (turn_id,))
        for _ in range(200):
            if not console._running:
                return
            threading.Event().wait(0.01)
        self.fail("the stub never released the lock")


class TestOneTurnAtATime(ConsoleCase):

    def test_a_second_send_while_one_is_in_flight_is_refused(self):
        console.send(self.conn, "first")
        with self.assertRaises(console.Busy):
            console.send(self.conn, "second")

    def test_a_refused_send_writes_nothing(self):
        console.send(self.conn, "first")
        before = self.conn.execute("SELECT count(*) FROM console_turns").fetchone()[0]
        with self.assertRaises(console.Busy):
            console.send(self.conn, "second")
        after = self.conn.execute("SELECT count(*) FROM console_turns").fetchone()[0]
        self.assertEqual(before, after)

    def test_a_pending_row_blocks_a_send_even_with_the_lock_free(self):
        """The lock is per-process; the ledger outlives it. A turn left pending
        by a crashed process has to block the next send too, or the answer lands
        in a conversation that has moved on."""
        first = console.send(self.conn, "first")
        self.release.set()
        console._running = False                     # as if the process restarted

        with self.assertRaises(console.Busy) as caught:
            console.send(self.conn, "second")
        self.assertIn("still open", str(caught.exception))
        self.assertEqual(
            self.conn.execute("SELECT status FROM console_turns WHERE id = ?",
                              (first["turn_id"],)).fetchone()[0], "pending")

    def test_a_send_is_allowed_once_the_turn_has_finished(self):
        first = console.send(self.conn, "first")
        self.finish(first["turn_id"])
        self.release.clear()

        second = console.send(self.conn, "second")
        self.assertNotEqual(second["turn_id"], first["turn_id"])


class TestSessionContinuity(ConsoleCase):

    def test_the_first_turn_mints_a_session_and_the_next_resumes_it(self):
        first = console.send(self.conn, "first")
        session_id, resume = self.launched[0][2], self.launched[0][3]
        self.assertTrue(session_id)
        self.assertFalse(resume, "the first turn of an epoch starts a session")

        self.finish(first["turn_id"])
        self.release.clear()
        console.send(self.conn, "second")
        self.assertEqual(self.launched[1][2], session_id)
        self.assertTrue(self.launched[1][3], "every turn after the first resumes")


class TestClear(ConsoleCase):

    def test_clear_is_refused_while_a_turn_is_in_flight(self):
        console.send(self.conn, "first")
        with self.assertRaises(console.Busy):
            console.clear(self.conn)

    def test_clear_starts_a_new_epoch_without_deleting_the_spend(self):
        """Clearing must not erase what a conversation cost. A chat that can
        delete its own bill is a chat that can lie about it."""
        first = console.send(self.conn, "first")
        self.finish(first["turn_id"])
        self.release.clear()

        before = self.conn.execute("SELECT count(*) FROM console_turns").fetchone()[0]
        result = console.clear(self.conn)
        after = self.conn.execute("SELECT count(*) FROM console_turns").fetchone()[0]

        self.assertEqual(result["epoch"], 2)
        self.assertEqual(before, after, "the old turns stay, addressable by epoch")
        row = self.conn.execute("SELECT * FROM console_state WHERE id = 1").fetchone()
        self.assertIsNone(row["session_id"], "a cleared chat resumes nothing")

    def test_the_turn_after_a_clear_starts_a_fresh_session(self):
        first = console.send(self.conn, "first")
        self.finish(first["turn_id"])
        self.release.clear()
        console.clear(self.conn)

        console.send(self.conn, "after the clear")
        self.assertNotEqual(self.launched[1][2], self.launched[0][2])
        self.assertFalse(self.launched[1][3])


class TestMessageLimits(ConsoleCase):

    def test_an_empty_message_is_refused(self):
        for text in ("", "   ", None):
            with self.subTest(text=text), self.assertRaises(ValueError):
                console.send(self.conn, text)

    def test_a_message_over_the_cap_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            console.send(self.conn, "x" * (console.MAX_CHARS + 1))
        self.assertIn(str(console.MAX_CHARS), str(caught.exception))

    def test_a_refused_message_leaves_the_lock_free(self):
        with self.assertRaises(ValueError):
            console.send(self.conn, "")
        self.assertFalse(console._running)
        console.send(self.conn, "a real one")       # would raise Busy if stuck


if __name__ == "__main__":
    unittest.main()
