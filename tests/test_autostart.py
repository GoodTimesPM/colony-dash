"""Picking an address to bind, and not raising a second server on it.

Nothing here talks to Task Scheduler. Registering a real task is a change to the
machine running the tests, and the parts of `autostart.py` worth asserting are
the two decisions it makes before it gets there: what `--host auto` resolves to,
and whether a dashboard is already up. The PowerShell is a string; the judgement
is here.
"""

from __future__ import annotations

import unittest
from unittest import mock

from colony import access, autostart, desktop, net


def with_addresses(*values):
    """Pretend this machine answers to exactly these addresses."""
    return mock.patch.object(net, "_candidates", lambda: list(values))


class TestChoosingAnAddress(unittest.TestCase):

    def test_a_tailnet_address_wins(self):
        with with_addresses("192.168.1.40", "100.94.3.11", "10.0.0.57"):
            self.assertEqual(net.tailnet(), "100.94.3.11")
            self.assertEqual(net.auto(), ("100.94.3.11", "tailnet"))

    def test_a_private_lan_address_is_the_fallback(self):
        for value in ("192.168.1.40", "10.0.0.57", "172.16.4.9"):
            with self.subTest(value=value), with_addresses("127.0.0.1", value):
                self.assertEqual(net.auto(), (value, "lan"))

    def test_the_tailnet_range_is_not_mistaken_for_a_lan(self):
        """100.64/10 is private but it is not RFC 1918, and it is labelled."""
        with with_addresses("100.94.3.11"):
            self.assertIsNone(net.lan())
            self.assertEqual(net.auto()[1], "tailnet")

    def test_a_public_address_is_never_chosen(self):
        """Better to refuse than to serve the ledger to the internet."""
        with with_addresses("203.0.113.7", "8.8.8.8"):
            self.assertIsNone(net.tailnet())
            self.assertIsNone(net.lan())
            with self.assertRaises(net.NoAddress):
                net.auto()

    def test_reserved_is_not_the_same_as_private(self):
        """`.is_private` answers True for all of these. None of them is a LAN."""
        for value in ("203.0.113.7", "198.18.0.1", "192.0.2.5", "169.254.10.2"):
            with self.subTest(value=value), with_addresses(value):
                self.assertIsNone(net.lan())

    def test_loopback_and_link_local_are_not_addresses_a_phone_can_reach(self):
        with with_addresses("127.0.0.1", "169.254.10.2"):
            with self.assertRaises(net.NoAddress):
                net.auto()

    def test_garbage_is_skipped_rather_than_raised_on(self):
        with with_addresses("", "not-an-address", "10.0.0.57"):
            self.assertEqual(net.auto(), ("10.0.0.57", "lan"))

    def test_the_refusal_says_what_to_do(self):
        with with_addresses("203.0.113.7"):
            with self.assertRaises(net.NoAddress) as caught:
                net.auto()
        self.assertIn("--host", str(caught.exception))


class TestResolve(unittest.TestCase):

    def test_no_host_is_loopback(self):
        self.assertEqual(net.resolve(None), ("127.0.0.1", "loopback"))

    def test_auto_is_the_only_magic_word(self):
        with with_addresses("100.94.3.11"):
            self.assertEqual(net.resolve("auto"), ("100.94.3.11", "tailnet"))
            self.assertEqual(net.resolve("AUTO "), ("100.94.3.11", "tailnet"))

    def test_a_literal_address_passes_straight_through(self):
        self.assertEqual(net.resolve("192.168.1.40"), ("192.168.1.40", "given"))

    def test_every_interface_is_labelled_and_warned_about(self):
        self.assertEqual(net.resolve("0.0.0.0"), ("0.0.0.0", "every interface"))
        self.assertIn("tailnet", net.advice("every interface"))

    def test_a_lan_bind_says_the_token_is_the_only_lock(self):
        self.assertIn("only lock", net.advice("lan"))

    def test_a_tailnet_bind_needs_no_warning(self):
        self.assertEqual(net.advice("tailnet"), "")
        self.assertEqual(net.advice("loopback"), "")


class TestPreflight(unittest.TestCase):
    """Installing the task must fail in the terminal, not at the next logon."""

    def test_no_token_refuses_before_anything_is_registered(self):
        with with_addresses("100.94.3.11"), mock.patch.object(access, "token", lambda: None):
            with self.assertRaises(access.Unconfigured):
                autostart.preflight()

    def test_a_token_and_an_address_is_enough(self):
        with with_addresses("100.94.3.11"), mock.patch.object(access, "token", lambda: "s3cret"):
            self.assertEqual(autostart.preflight(), ("100.94.3.11", "tailnet"))

    def test_no_address_refuses_even_with_a_token(self):
        with with_addresses("203.0.113.7"), mock.patch.object(access, "token", lambda: "s3cret"):
            with self.assertRaises(net.NoAddress):
                autostart.preflight()

    def test_an_explicit_loopback_bind_needs_no_token(self):
        """Pointless as an autostart, but it must not be the thing that raises."""
        with mock.patch.object(access, "token", lambda: None):
            self.assertEqual(autostart.preflight("127.0.0.1"), ("127.0.0.1", "given"))


class TestNotServingTwice(unittest.TestCase):
    """A server bound to the network is invisible to a loopback probe.

    That is the whole bug: the logon task binds 100.x.y.z, the desktop shortcut
    probes 127.0.0.1, finds it free, and raises a second dashboard on the same
    port on a different interface. Two servers, one ledger, no error.
    """

    def setUp(self):
        self._patch = mock.patch.object(desktop, "ADDRESS_PATH",
                                        desktop.db.RUNTIME_DIR / "dash.url.test")
        self.path = self._patch.start()
        self.addCleanup(self._patch.stop)
        self.addCleanup(lambda: self.path.unlink(missing_ok=True))
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def test_a_marked_address_is_found_when_the_port_answers(self):
        desktop._mark("100.94.3.11", 8787)
        with mock.patch.object(desktop, "_port_is_free", lambda h, p: False):
            self.assertEqual(desktop._already_serving(8787), "100.94.3.11")

    def test_a_stale_marker_is_not_believed(self):
        """The file outlives the process that wrote it every single time."""
        desktop._mark("100.94.3.11", 8787)
        with mock.patch.object(desktop, "_port_is_free", lambda h, p: True):
            self.assertIsNone(desktop._already_serving(8787))

    def test_a_marker_for_another_port_is_ignored(self):
        """Two dashboards on two ports is something someone may have meant."""
        desktop._mark("100.94.3.11", 8787)
        with mock.patch.object(desktop, "_port_is_free", lambda h, p: False):
            self.assertIsNone(desktop._already_serving(9999))

    def test_no_marker_at_all_is_not_an_error(self):
        self.path.unlink(missing_ok=True)
        self.assertIsNone(desktop._already_serving(8787))

    def test_a_junk_marker_is_not_an_error(self):
        self.path.write_text("garbage\n", encoding="utf-8")
        self.assertIsNone(desktop._already_serving(8787))

    def test_marking_survives_an_unwritable_path(self):
        """A marker is a convenience; failing to write one must never stop a launch.

        The marker is pointed *inside* a file here, so `mkdir` raises
        NotADirectoryError for real rather than by patch.
        """
        blocked = self.path.with_suffix(".file") / "dash.url"
        blocked.parent.write_text("not a directory\n", encoding="utf-8")
        self.addCleanup(lambda: blocked.parent.unlink(missing_ok=True))
        with mock.patch.object(desktop, "ADDRESS_PATH", blocked):
            desktop._mark("100.94.3.11", 8787)  # must not raise
            self.assertIsNone(desktop._already_serving(8787))


if __name__ == "__main__":
    unittest.main()
