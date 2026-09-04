"""What counts as "the dashboard is already running".

One function, and it earns a file because getting it wrong produced the only
bug in this project so far that reported success at every layer while doing
nothing: phone access switched on, a task installed, a log line saying the
server was up, a QR code on screen, and a phone that loaded forever.
"""

from __future__ import annotations

import contextlib
import threading
import unittest
from unittest import mock

import colony
from colony import desktop

LAN = "10.0.0.57"


class Reusable(unittest.TestCase):

    def setUp(self):
        self.free = mock.patch.object(desktop, "_port_is_free", lambda h, p: True)
        self.free.start()
        self.addCleanup(self.free.stop)

    def marker(self, address):
        return mock.patch.object(desktop, "_already_serving", lambda p: address)

    def test_a_live_socket_on_the_asked_for_address_is_reused(self):
        with mock.patch.object(desktop, "_port_is_free", lambda h, p: False):
            self.assertEqual(desktop._reusable(LAN, 8787), LAN)

    def test_a_loopback_server_does_not_satisfy_a_network_request(self):
        """The regression. The marker says 127.0.0.1; the caller asked for the
        LAN address; the answer is no, and the caller binds it itself."""
        with self.marker("127.0.0.1"):
            self.assertIsNone(desktop._reusable(LAN, 8787))

    def test_a_network_server_still_satisfies_a_loopback_request(self):
        """The case the marker file was written for, which must keep working:
        the shortcut probes loopback, the logon task bound the LAN."""
        with self.marker(LAN):
            self.assertEqual(desktop._reusable(desktop.HOST, 8787), LAN)

    def test_no_marker_means_nothing_to_reuse(self):
        with self.marker(None):
            self.assertIsNone(desktop._reusable(desktop.HOST, 8787))
            self.assertIsNone(desktop._reusable(LAN, 8787))


class HeadlessLaunchExits(unittest.TestCase):
    """A headless launch that does not own the port has to end.

    It used to log "reusing it" and then fall into the sleep loop meant for a
    process holding its own daemon server thread. Nine of those had accumulated
    before anyone counted the processes.
    """

    def setUp(self):
        self.idled = []
        # Lets a blocking stub server end with the test instead of
        # spinning for the rest of the run.
        self.released = threading.Event()
        self.addCleanup(self.released.set)
        idle = mock.patch.object(desktop, "_idle",
                                 lambda: self.idled.append(True) or 0)
        idle.start()
        self.addCleanup(idle.stop)
        for name, value in (("_wait_for_port", lambda h, p: True),
                            ("_mark", lambda h, p: None),
                            ("log", lambda *a, **k: None)):
            patch = mock.patch.object(desktop, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def serve(self, blocking: bool):
        """Stand in for `server.serve`, either holding the thread or returning.

        `from . import server` reads the attribute off the package when there is
        one, and falls back to `sys.modules` otherwise, so both have to be
        patched. Patching only `sys.modules` passes on its own and fails under
        `unittest discover`, where an earlier test has already imported the real
        module -- and then these tests bind port 8787 for real.
        """
        def _serve(host=None, port=None):
            if blocking:
                self.released.wait(10)

        stub = mock.Mock(serve=_serve)
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.dict("sys.modules",
                                            {"colony.server": stub}))
        stack.enter_context(mock.patch.object(colony, "server", stub,
                                              create=True))
        return stack

    def test_a_reused_dashboard_returns_instead_of_sleeping(self):
        with mock.patch.object(desktop, "_reusable", lambda h, p: LAN), \
             self.serve(blocking=False):
            self.assertEqual(desktop.launch(8787, window=False), 0)
        self.assertEqual(self.idled, [])

    def test_losing_the_bind_race_returns_instead_of_sleeping(self):
        """The port is free when asked, and taken by the time we bind: the
        serving thread ends, and so should the process."""
        with mock.patch.object(desktop, "_reusable", lambda h, p: None), \
             self.serve(blocking=False):
            self.assertEqual(desktop.launch(8787, window=False), 0)
        self.assertEqual(self.idled, [])

    def test_owning_the_port_still_holds_the_process_open(self):
        with mock.patch.object(desktop, "_reusable", lambda h, p: None), \
             self.serve(blocking=True):
            self.assertEqual(desktop.launch(8787, window=False), 0)
        self.assertEqual(self.idled, [True])


class ANetworkBindKeepsLoopback(unittest.TestCase):
    """Binding the network must not cost this machine its own address.

    `--host auto` resolves to one address and binds that alone, which is what
    the logon task runs. Nothing was left on 127.0.0.1, so the desktop window
    had to open the tailnet address, every request from it arrived looking like
    it came off the network, and the console -- which is scoped to the machine
    rather than to the token -- refused to take a message from the desktop it
    was running on. It then advised turning the boundary off from the desktop,
    which is where the person already was.
    """

    def setUp(self):
        self.marks = []
        self.released = threading.Event()
        self.addCleanup(self.released.set)
        for name, value in (("_wait_for_port", lambda h, p: True),
                            ("_mark", lambda h, p: self.marks.append(h)),
                            ("_idle", lambda: 0),
                            ("_reusable", lambda h, p: None),
                            ("log", lambda *a, **k: None)):
            patch = mock.patch.object(desktop, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def serve(self, extra):
        """`server.serve` holds its thread; `serve_extra` does whatever `extra` does."""
        def _serve(host=None, port=None):
            self.released.wait(10)

        # `serve_extra` has to stay a Mock rather than the bare callable, or the
        # call assertions below have nothing to read.
        stub = mock.Mock(serve=_serve, serve_extra=mock.Mock(side_effect=extra))
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.dict("sys.modules",
                                            {"colony.server": stub}))
        stack.enter_context(mock.patch.object(colony, "server", stub, create=True))
        return stack, stub

    def test_loopback_is_bound_too_and_becomes_the_recorded_address(self):
        stack, stub = self.serve(extra=lambda h, p: None)
        with stack:
            self.assertEqual(desktop.launch(8787, host=LAN, window=False), 0)
        stub.serve_extra.assert_called_once_with(desktop.HOST, 8787)
        # The marker is what the next launch reuses, so it has to end up
        # pointing at loopback rather than at the address bound first.
        self.assertEqual(self.marks, [LAN, desktop.HOST])

    def test_a_loopback_bind_that_fails_does_not_take_the_dashboard_down(self):
        """The network server is up and works. Losing the second socket costs
        the shortcut a nicer address, and that is not worth an exit."""
        def boom(h, p):
            raise OSError("something else is on 8787")

        stack, _ = self.serve(extra=boom)
        with stack:
            self.assertEqual(desktop.launch(8787, host=LAN, window=False), 0)
        self.assertEqual(self.marks, [LAN])

    def test_a_loopback_launch_does_not_bind_a_second_socket(self):
        stack, stub = self.serve(extra=lambda h, p: None)
        with stack:
            self.assertEqual(desktop.launch(8787, window=False), 0)
        stub.serve_extra.assert_not_called()


if __name__ == "__main__":
    unittest.main()
