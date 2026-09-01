"""What counts as "the dashboard is already running".

One function, and it earns a file because getting it wrong produced the only
bug in this project so far that reported success at every layer while doing
nothing: phone access switched on, a task installed, a log line saying the
server was up, a QR code on screen, and a phone that loaded forever.
"""

from __future__ import annotations

import unittest
from unittest import mock

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


if __name__ == "__main__":
    unittest.main()
