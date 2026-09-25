"""Restarting the dashboard has to actually restart the dashboard.

The bug these cover: a launch found a live server on the port, attached a window
to it and reported success. The page's scripts are read off disk on every request and
Python is not, so the page was new and the routes were old -- a button that
visibly existed answered 404, and "I restarted it" and "it is running my change"
had quietly stopped being the same sentence.
"""

from __future__ import annotations

import unittest
from unittest import mock

from colony import desktop


class TheStamp(unittest.TestCase):

    def test_it_is_stable_when_nothing_has_changed(self):
        self.assertEqual(desktop.stamp(), desktop.stamp())

    def test_it_is_short_enough_to_read_in_a_log_line(self):
        got = desktop.stamp()
        self.assertEqual(len(got), 16)
        self.assertTrue(all(c in "0123456789abcdef" for c in got))

    def test_it_moves_when_a_source_file_does(self):
        """Size and mtime, not contents. An editor writing a change moves at
        least one of them, and reading every file on every launch would not be
        worth what it buys."""
        real = desktop.Path.stat

        def bigger(self, *a, **kw):
            st = real(self, *a, **kw)
            if self.name != "server.py":
                return st
            # `is_file()` stats too, so the stand-in has to answer that as well
            # or the file drops out of the walk instead of changing size.
            return type("Stat", (), {"st_size": st.st_size + 1,
                                     "st_mtime": st.st_mtime,
                                     "st_mode": st.st_mode})()

        before = desktop.stamp()
        with mock.patch.object(desktop.Path, "stat", bigger):
            after = desktop.stamp()
        self.assertNotEqual(before, after)


class IsItStale(unittest.TestCase):
    """`_stale` decides whether a live dashboard gets replaced, so every way of
    not knowing has to come back False. Killing a working dashboard on a guess
    is worse than attaching to one that turns out to be current."""

    def stale(self, reply):
        with mock.patch.object(desktop, "_ask", return_value=reply):
            return desktop._stale("127.0.0.1", 8787)

    def test_the_same_build_is_not_stale(self):
        self.assertFalse(self.stale({"stamp": desktop.stamp()}))

    def test_a_different_build_is_stale(self):
        self.assertTrue(self.stale({"stamp": "0000000000000000"}))

    def test_a_server_with_no_such_route_is_stale(self):
        """Every build that can answer this has the route. A 404 is therefore
        proof of age, and it is the only signal available for the one upgrade
        that introduces the route."""
        self.assertTrue(self.stale({"status": 404, "error": "Not Found"}))

    def test_a_server_that_does_not_answer_is_left_alone(self):
        self.assertFalse(self.stale(None))

    def test_a_server_that_refuses_the_question_is_left_alone(self):
        self.assertFalse(self.stale({"status": 403, "error": "nope"}))

    def test_an_answer_with_no_stamp_in_it_is_left_alone(self):
        self.assertFalse(self.stale({"pid": 123}))


class StoppingTheOldOne(unittest.TestCase):

    def test_it_waits_for_the_port_to_actually_free(self):
        """The point of stopping it is the port. A server that says yes and
        keeps the socket has not stopped, and binding on its word would fail in
        a thread whose only output is a log file."""
        with (
            mock.patch.object(desktop, "_ask", return_value={"ok": True}),
            mock.patch.object(desktop, "_port_is_free", side_effect=[False, False, True]),
        ):
            self.assertTrue(desktop._stop("127.0.0.1", 8787))

    def test_a_port_that_never_frees_is_a_failure(self):
        with (
            mock.patch.object(desktop, "_ask", return_value={"ok": True}),
            mock.patch.object(desktop, "_port_is_free", return_value=False),
            mock.patch.object(desktop, "log"),
        ):
            self.assertFalse(desktop._stop("127.0.0.1", 8787, timeout_s=0.3))

    def test_a_refusal_is_reported_not_forced(self):
        """The server refuses while an agent run is open. That is a real answer
        and the launch has to respect it, because the alternative is dropping
        the interpreter out from under a write in progress."""
        with (
            mock.patch.object(desktop, "_ask", return_value={
                "status": 409, "error": "1 agent run still going"}),
            mock.patch.object(desktop, "log") as said,
        ):
            self.assertFalse(desktop._stop("127.0.0.1", 8787))
        self.assertIn("still going", " ".join(str(c) for c in said.call_args_list))

    def test_no_answer_at_all_is_a_failure(self):
        with (
            mock.patch.object(desktop, "_ask", return_value=None),
            mock.patch.object(desktop, "log"),
        ):
            self.assertFalse(desktop._stop("127.0.0.1", 8787))


class WhatLaunchDoesAboutIt(unittest.TestCase):
    """`launch` is mostly window and thread management, so these drive it with
    everything below it stubbed and assert only on the decision: attach, or take
    the port."""

    def run_launch(self, *, stale, stopped, replace=False):
        stops = []
        with (
            mock.patch.object(desktop, "_reusable", return_value="127.0.0.1"),
            mock.patch.object(desktop, "_stale", return_value=stale),
            mock.patch.object(desktop, "_stop",
                              side_effect=lambda *a, **k: (stops.append(a), stopped)[1]),
            mock.patch.object(desktop, "_wait_for_port", return_value=True),
            mock.patch.object(desktop, "_mark"),
            mock.patch.object(desktop, "_idle", return_value=0),
            mock.patch.object(desktop, "log"),
            mock.patch("threading.Thread") as thread,
        ):
            thread.return_value.is_alive.return_value = True
            desktop.launch(port=8787, host="127.0.0.1", window=False,
                           replace=replace)
        return stops

    def test_a_current_dashboard_is_reused_and_not_touched(self):
        self.assertEqual(self.run_launch(stale=False, stopped=True), [])

    def test_a_stale_dashboard_is_stopped(self):
        self.assertEqual(len(self.run_launch(stale=True, stopped=True)), 1)

    def test_restart_stops_one_that_is_not_stale(self):
        """`--restart` is for when the stamp cannot see the change: a dependency
        moved, an env var changed, or the process has simply gone wrong."""
        self.assertEqual(len(self.run_launch(stale=False, stopped=True, replace=True)), 1)

    def test_a_stale_one_that_will_not_stop_is_still_used(self):
        """Better a dashboard on old code than no dashboard. The log is what
        carries the bad news; the window still opens."""
        self.assertEqual(len(self.run_launch(stale=True, stopped=False)), 1)


class TheQuitRoute(unittest.TestCase):
    """The one route that ends the process. Everything else under `/api/act/*`
    leaves a row you can go and read afterwards; this one leaves nothing, so
    both of its refusals are tested for the refusal *and* for not exiting."""

    def setUp(self):
        import sqlite3
        import tempfile
        from pathlib import Path

        from colony.web import controls
        self.controls = controls
        # On disk rather than in memory: the route closes the connection it is
        # handed, and `:memory:` disappears with the first close.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = str(Path(tmp.name) / "t.db")
        # `with sqlite3.connect(...)` commits but does not close, and a handle
        # left open is a directory Windows will not delete.
        boot = sqlite3.connect(self.path)
        boot.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY, ended_at TEXT)")
        boot.commit()
        boot.close()

        def fresh():
            c = sqlite3.connect(self.path)
            c.row_factory = sqlite3.Row
            return c

        self.db = fresh()
        self.addCleanup(self.db.close)
        self.exits = []
        self.enter = mock.patch.object(controls, "_conn", fresh)
        self.timer = mock.patch("threading.Timer",
                                lambda *a, **k: mock.Mock(start=lambda: self.exits.append(1)))
        self.enter.start(); self.addCleanup(self.enter.stop)
        self.timer.start(); self.addCleanup(self.timer.stop)

    def quit_from(self, host):
        req = mock.Mock()
        req.client.host = host
        return self.controls.act_quit(req, x_colony="1")

    def test_a_quiet_dashboard_on_loopback_stops(self):
        out = self.quit_from("127.0.0.1")
        self.assertTrue(out["ok"])
        self.assertEqual(len(self.exits), 1)

    def test_it_will_not_be_stopped_from_the_network(self):
        """A phone on the tailnet pressing this would take the dashboard down
        with nothing left on that phone to bring it back."""
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as caught:
            self.quit_from("100.64.0.9")
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(self.exits, [])

    def test_it_will_not_be_stopped_mid_run(self):
        """A run with no `ended_at` is a write the colony has started. Ending
        the interpreter under one leaves a worktree and a half-written ticket."""
        from fastapi import HTTPException

        self.db.execute("INSERT INTO runs (ended_at) VALUES (NULL)")
        self.db.commit()
        with self.assertRaises(HTTPException) as caught:
            self.quit_from("127.0.0.1")
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("still going", str(caught.exception.detail))
        self.assertEqual(self.exits, [])

    def test_a_finished_run_does_not_hold_it_open(self):
        self.db.execute("INSERT INTO runs (ended_at) VALUES ('2026-09-05 12:00:00')")
        self.db.commit()
        self.assertTrue(self.quit_from("127.0.0.1")["ok"])

    def test_the_build_it_reports_is_the_one_it_started_on(self):
        """Captured at import, never recomputed. Asking the filesystem again
        would answer for the checkout and every server would look current --
        which is the entire bug."""
        self.assertEqual(self.controls.BUILD_STAMP, self.quit_from("127.0.0.1")["stamp"])


if __name__ == "__main__":
    unittest.main()
