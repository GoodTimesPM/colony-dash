"""Tests for the Tailscale panel's four answers.

Nothing here runs `tailscale`, installs anything, or asks the machine what
network it is on. Every test replaces `find` or `proc.run`, because a test that
passed only on a machine with Tailscale signed in would be a test that told two
different stories on two developers' laptops -- and the state this module exists
to describe is precisely the one where it is *not* set up.

The cases worth pinning are the ones where being wrong is quiet. A signed-out
daemon exits non-zero and still prints the JSON that says so, so reading the
exit code instead of the output would turn the most common state into "cannot
tell". A machine that has been upgraded rather than reinstalled keeps the CLI in
the old directory. And the trailing dot MagicDNS puts on a name is correct DNS
and a broken URL.
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from colony import tailscale


def _done(stdout, code=0):
    return subprocess.CompletedProcess(["tailscale"], code, stdout, "")


class Finding(unittest.TestCase):

    def test_the_old_install_directory_still_counts(self):
        """Upgraded machines keep the CLI under 'Tailscale IPN'."""
        old = tailscale.EXE_CANDIDATES[1]

        with mock.patch.object(Path, "exists", lambda self: str(self) == old):
            self.assertEqual(str(tailscale.find()), old)

    def test_nothing_installed_is_none_rather_than_a_raise(self):
        with mock.patch.object(Path, "exists", lambda self: False), \
             mock.patch.dict("os.environ", {"PATH": ""}, clear=False):
            self.assertIsNone(tailscale.find())

    def test_the_newest_installer_wins_by_date_not_by_name(self):
        """1.99.0 sorts after 1.102.3 as a string and is the older build."""
        with tempfile.TemporaryDirectory() as root:
            downloads = Path(root) / "Downloads"
            downloads.mkdir()
            older = downloads / "tailscale-setup-1.99.0.exe"
            newer = downloads / "tailscale-setup-1.102.3.exe"
            older.write_bytes(b"x")
            newer.write_bytes(b"x")
            import os
            os.utime(older, (1_000, 1_000))
            os.utime(newer, (2_000, 2_000))

            with mock.patch.dict("os.environ", {"USERPROFILE": root}):
                self.assertEqual(tailscale.installer(), newer)


class Reading(unittest.TestCase):

    def setUp(self):
        patch = mock.patch.object(tailscale, "find",
                                  lambda: Path("C:/fake/tailscale.exe"))
        patch.start()
        self.addCleanup(patch.stop)

    def answers(self, payload, code=0):
        return mock.patch.object(tailscale.proc, "run",
                                 lambda *a, **k: _done(json.dumps(payload), code))

    def test_signed_out_is_read_from_the_output_not_the_exit_code(self):
        """The state people are actually in when they open this panel."""
        with self.answers({"BackendState": "NeedsLogin", "Self": {}}, code=1):
            state = tailscale.state()

        self.assertTrue(state["installed"])
        self.assertTrue(state["needs_login"])
        self.assertFalse(state["connected"])

    def test_connected_reports_the_v4_address_and_a_clean_name(self):
        with self.answers({
            "BackendState": "Running",
            "Self": {"TailscaleIPs": ["fd7a:115c:a1e0::1", "100.83.12.4"],
                     "DNSName": "desk.tail1234.ts.net."},
        }):
            state = tailscale.state()

        # IPv6 first in the list, and the server binds v4.
        self.assertEqual(state["address"], "100.83.12.4")
        # The trailing dot is correct DNS and a broken URL.
        self.assertEqual(state["name"], "desk.tail1234.ts.net")
        self.assertTrue(state["connected"])

    def test_running_with_no_address_is_not_connected(self):
        """Mid-start. Claiming connected here sends someone to debug a phone."""
        with self.answers({"BackendState": "Running", "Self": {}}):
            self.assertFalse(tailscale.state()["connected"])

    def test_unreadable_output_is_an_answer_rather_than_a_crash(self):
        with mock.patch.object(tailscale.proc, "run",
                               lambda *a, **k: _done("not json at all")):
            state = tailscale.state()
        self.assertTrue(state["installed"])
        self.assertFalse(state["connected"])

    def test_a_timeout_is_an_answer_too(self):
        def boom(*a, **k):
            raise subprocess.TimeoutExpired("tailscale", 10)

        with mock.patch.object(tailscale.proc, "run", boom):
            self.assertFalse(tailscale.state()["connected"])


class Acting(unittest.TestCase):

    def test_install_without_an_installer_says_where_to_get_one(self):
        with mock.patch.object(tailscale, "installer", lambda: None):
            with self.assertRaises(tailscale.NotInstalled) as caught:
                tailscale.install()
        self.assertIn(tailscale.DOWNLOAD_PAGE, str(caught.exception))

    def test_login_without_the_cli_refuses_before_starting_anything(self):
        with mock.patch.object(tailscale, "find", lambda: None), \
             mock.patch.object(subprocess, "Popen") as popen:
            with self.assertRaises(tailscale.NotInstalled):
                tailscale.login()
        popen.assert_not_called()

    def test_the_auth_url_is_the_one_tailscale_printed(self):
        """Matched off the real output shape, blank line and tab included."""
        printed = ("\nTo authenticate, visit:\n\n"
                   "\thttps://login.tailscale.com/a/1a2b3c4d5e6f\n\n")
        self.assertEqual(
            tailscale.AUTH_URL.search(printed).group(0),
            "https://login.tailscale.com/a/1a2b3c4d5e6f")

    def test_no_url_falls_back_to_asking_the_daemon(self):
        """Already signed in: `up` reconnects, prints nothing, and exits."""
        class Exited:
            stdout = __import__("io").StringIO("")

            def poll(self):
                return 0

            def kill(self):
                pass

        with mock.patch.object(tailscale, "find",
                               lambda: Path("C:/fake/tailscale.exe")), \
             mock.patch.object(subprocess, "Popen", lambda *a, **k: Exited()), \
             mock.patch.object(tailscale, "state",
                               lambda: {"connected": True, "address": "100.1.2.3"}):
            out = tailscale.login(timeout=1.0)

        self.assertIsNone(out["url"])
        self.assertEqual(out["address"], "100.1.2.3")


class Advice(unittest.TestCase):
    """`net.advice` is the sentence that decides whether anyone bothers."""

    def test_a_lan_address_says_it_dies_outside_the_building(self):
        from colony import net
        line = net.advice("lan")
        self.assertIn("cellular", line)
        self.assertIn("Tailscale", line)

    def test_a_tailnet_address_stays_quiet(self):
        """Every caller prints this behind a warning sign. Good news must not be."""
        from colony import net
        self.assertEqual(net.advice("tailnet"), "")


if __name__ == "__main__":
    unittest.main()
