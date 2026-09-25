"""Tests for the phone-access switch.

The two things worth pinning here are both about `.env`, because `.env` is the
one file in this project where a bug costs more than a rerun: it holds the
Notion token beside the access token, and the switch writes to it from a web
request. So the rules from `colony/phone.py` get tests rather than only a
docstring -- a token is written only when there is not one, and the write is an
append that leaves every existing line exactly as it was.

Nothing here registers a scheduled task or touches the real `.env`. `ENV_PATH`
is pointed at a temp file and `autostart` is intercepted; a test that installed
a logon task would be a test that changed the machine it ran on.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from colony import access, net, phone

EXISTING = (
    "NOTION_TOKEN=ntn_pretend\n"
    "NOTION_STORIES_DB=1234\n"
)


class EnvWriting(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.env = Path(self.dir.name) / ".env"
        patch = mock.patch.object(phone, "ENV_PATH", self.env)
        patch.start()
        self.addCleanup(patch.stop)

    def token_is(self, value):
        """Make `access.token()` answer as if `.env` said so."""
        return mock.patch.object(access, "token", lambda: value)

    def test_mints_and_appends_when_absent(self):
        self.env.write_text(EXISTING, encoding="utf-8")
        with self.token_is(None):
            token, minted = phone._ensure_token()
        self.assertTrue(minted)
        body = self.env.read_text(encoding="utf-8")
        self.assertTrue(body.startswith(EXISTING), "existing lines were rewritten")
        self.assertIn(f"{access.TOKEN_ENV}={token}", body)

    def test_never_overwrites_an_existing_token(self):
        """The rule the whole module is built around.

        Rewriting the token from a web request would log out a phone that is
        already paired, and the failure would look like the button working.
        """
        self.env.write_text(EXISTING + f"{access.TOKEN_ENV}=already-set\n",
                            encoding="utf-8")
        before = self.env.read_text(encoding="utf-8")
        with self.token_is("already-set"):
            token, minted = phone._ensure_token()
        self.assertEqual(token, "already-set")
        self.assertFalse(minted)
        self.assertEqual(self.env.read_text(encoding="utf-8"), before)

    def test_no_blank_line_pile_up(self):
        """A file that already ends in a newline does not grow one per call."""
        self.env.write_text(EXISTING, encoding="utf-8")
        with self.token_is(None):
            phone._ensure_token()
        first = self.env.read_text(encoding="utf-8")
        self.assertNotIn("\n\n\n", first)

    def test_file_without_trailing_newline(self):
        """The token must not end up glued to the end of the last value."""
        self.env.write_text("NOTION_TOKEN=ntn_pretend", encoding="utf-8")
        with self.token_is(None):
            token, _ = phone._ensure_token()
        body = self.env.read_text(encoding="utf-8")
        self.assertIn("NOTION_TOKEN=ntn_pretend\n", body)
        self.assertNotIn(f"ntn_pretend{access.TOKEN_ENV}", body)
        self.assertIn(f"\n{access.TOKEN_ENV}={token}\n", body)

    def test_missing_file_is_created(self):
        with self.token_is(None):
            token, minted = phone._ensure_token()
        self.assertTrue(minted)
        self.assertIn(token, self.env.read_text(encoding="utf-8"))

    def test_the_token_is_worth_having(self):
        with self.token_is(None):
            token, _ = phone._ensure_token()
        # 32 bytes url-safe. Short enough for a QR code at version 5, long
        # enough that guessing it is not a strategy.
        self.assertGreaterEqual(len(token), 40)
        self.assertNotIn("=", token)


class Rotating(unittest.TestCase):
    """`rotate()` is the one call in this module that rewrites `.env`.

    Every test here is really the same test asked five ways: the Notion token
    on the line above has to still be there afterwards. That file is not
    recoverable from anywhere -- it is the only copy of a credential the user
    pasted in by hand -- so the rewrite gets pinned harder than the append does.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.env = Path(self.dir.name) / ".env"
        for target, attr in ((phone, "ENV_PATH"),):
            patch = mock.patch.object(target, attr, self.env)
            patch.start()
            self.addCleanup(patch.stop)
        # `rotate` ends by calling `state()`, which shells out to Task Scheduler
        # and PowerShell. None of these tests are about either.
        for attr, value in (("state", lambda port: "open"),):
            patch = mock.patch.object(phone.firewall, attr, value)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(phone, "state", lambda port=8787, pairing=False: {})
        patch.start()
        self.addCleanup(patch.stop)

    def test_replaces_the_line_and_keeps_the_rest(self):
        self.env.write_text(EXISTING + f"{access.TOKEN_ENV}=old-one\n",
                            encoding="utf-8")
        phone.rotate()
        body = self.env.read_text(encoding="utf-8")
        self.assertNotIn("old-one", body)
        self.assertIn("NOTION_TOKEN=ntn_pretend\n", body)
        self.assertIn("NOTION_STORIES_DB=1234\n", body)
        self.assertEqual(body.count(f"{access.TOKEN_ENV}="), 1)

    def test_order_and_comments_survive(self):
        """No parse, no dict, no re-serialise -- so nothing gets reordered."""
        before = ("# top comment\n"
                  "NOTION_TOKEN=ntn_pretend\n"
                  "\n"
                  "# phone\n"
                  f"{access.TOKEN_ENV}=old-one\n"
                  "# trailing comment\n"
                  "LAST=9\n")
        self.env.write_text(before, encoding="utf-8")
        phone.rotate()
        after = self.env.read_text(encoding="utf-8")
        self.assertEqual(
            [line for line in after.splitlines()
             if not line.startswith(f"{access.TOKEN_ENV}=")],
            [line for line in before.splitlines()
             if not line.startswith(f"{access.TOKEN_ENV}=")])

    def test_crlf_endings_are_not_converted(self):
        """A file edited in Notepad has \\r\\n, and must keep it.

        Rewriting the whole file in "\\n" would leave every other line changed
        in a diff, which is exactly the noise that makes someone stop reading
        diffs of their credential file.
        """
        self.env.write_text("NOTION_TOKEN=ntn_pretend\r\n"
                            f"{access.TOKEN_ENV}=old-one\r\n",
                            encoding="utf-8", newline="")
        phone.rotate()
        raw = self.env.read_bytes()
        self.assertEqual(raw.count(b"\r\n"), 2)
        self.assertNotIn(b"old-one", raw)

    def test_a_file_with_no_token_gets_one(self):
        """Rotating on a machine that never had a token leaves one."""
        self.env.write_text(EXISTING, encoding="utf-8")
        phone.rotate()
        body = self.env.read_text(encoding="utf-8")
        self.assertTrue(body.startswith(EXISTING))
        self.assertIn(f"{access.TOKEN_ENV}=", body)

    def test_the_new_token_is_not_the_old_one(self):
        self.env.write_text(f"{access.TOKEN_ENV}=old-one\n", encoding="utf-8")
        phone.rotate()
        first = self.env.read_text(encoding="utf-8")
        phone.rotate()
        second = self.env.read_text(encoding="utf-8")
        self.assertNotEqual(first, second)
        self.assertNotIn("old-one", second)

    def test_environment_is_updated_only_when_it_was_already_set(self):
        """`db._env_value` prefers `os.environ`, so a stale export wins.

        And the other half matters more: a token put into `os.environ` that was
        not there before is a token handed to the `claude` subprocess the
        console spawns, which is not allowed to read credentials.
        """
        self.env.write_text(f"{access.TOKEN_ENV}=old-one\n", encoding="utf-8")
        with mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop(access.TOKEN_ENV, None)
            phone.rotate()
            self.assertNotIn(access.TOKEN_ENV, os.environ)
        with mock.patch.dict("os.environ", {access.TOKEN_ENV: "old-one"}):
            import os
            phone.rotate()
            self.assertNotEqual(os.environ[access.TOKEN_ENV], "old-one")
            self.assertIn(os.environ[access.TOKEN_ENV],
                          self.env.read_text(encoding="utf-8"))

    def test_no_temp_file_is_left_behind(self):
        self.env.write_text(f"{access.TOKEN_ENV}=old-one\n", encoding="utf-8")
        phone.rotate()
        leftovers = [p.name for p in Path(self.dir.name).iterdir()
                     if p.name != ".env"]
        self.assertEqual(leftovers, [])


class State(unittest.TestCase):

    def setUp(self):
        # `state()` asks the firewall, which is a PowerShell call. Nothing here
        # is about the firewall, and a unit test should not depend on how this
        # machine's is configured.
        patch = mock.patch.object(phone.firewall, "state", lambda port: "open")
        patch.start()
        self.addCleanup(patch.stop)

    def test_url_needs_both_halves(self):
        with mock.patch.object(access, "token", lambda: None), \
             mock.patch.object(net, "auto", lambda: ("100.1.2.3", "tailnet")):
            self.assertIsNone(phone.url())
        with mock.patch.object(access, "token", lambda: "abc"), \
             mock.patch.object(net, "auto",
                               mock.Mock(side_effect=net.NoAddress("nope"))):
            self.assertIsNone(phone.url())
        with mock.patch.object(access, "token", lambda: "abc"), \
             mock.patch.object(net, "auto", lambda: ("100.1.2.3", "tailnet")):
            self.assertEqual(phone.url(8080), "http://100.1.2.3:8080/?k=abc")

    def test_no_address_is_a_state_not_an_exception(self):
        """A laptop on a plane is a normal laptop, and the panel has to draw."""
        with mock.patch.object(net, "auto",
                               mock.Mock(side_effect=net.NoAddress("on a plane"))), \
             mock.patch.object(phone.autostart, "describe", lambda: {}):
            out = phone.state()
        self.assertIn("on a plane", out["problem"])
        self.assertIsNone(out["address"])
        self.assertIsNone(out["url"])
        self.assertFalse(out["on"])

    def test_state_reports_a_time_limited_task_as_limited(self):
        """An installed task that Task Scheduler will kill is not "on" quietly."""
        task = {"state": "Running", "time_limit": "P3D", "last_run": "x"}
        with mock.patch.object(phone.autostart, "describe", lambda: task), \
             mock.patch.object(access, "token", lambda: "abc"), \
             mock.patch.object(net, "auto", lambda: ("100.1.2.3", "tailnet")), \
             mock.patch.object(phone, "_reachable", lambda a, p: True):
            out = phone.state()
        self.assertTrue(out["on"])
        self.assertFalse(out["unlimited"])
        self.assertTrue(out["serving"])

    def test_turn_on_refuses_before_it_writes_anything(self):
        """No address means no token minted and no task registered.

        The order matters: a switch that wrote a credential and installed a task
        before discovering it had nowhere to bind would leave a machine set up
        for a feature that cannot work.
        """
        install = mock.Mock()
        with mock.patch.object(net, "auto",
                               mock.Mock(side_effect=net.NoAddress("nope"))), \
             mock.patch.object(phone, "_ensure_token") as ensure, \
             mock.patch.object(phone.autostart, "install", install):
            with self.assertRaises(net.NoAddress):
                phone.turn_on()
        ensure.assert_not_called()
        install.assert_not_called()

    def test_turn_off_leaves_the_token_alone(self):
        """"Off" means the server stops coming up, not "forget the phone"."""
        remove = mock.Mock()
        with mock.patch.object(phone.autostart, "remove", remove), \
             mock.patch.object(phone.autostart, "describe", lambda: {}), \
             mock.patch.object(access, "token", lambda: "abc"), \
             mock.patch.object(net, "auto", lambda: ("100.1.2.3", "tailnet")), \
             mock.patch.object(phone, "_reachable", lambda a, p: False):
            out = phone.turn_off()
        remove.assert_called_once()
        self.assertFalse(out["on"])
        self.assertTrue(out["token_set"])


if __name__ == "__main__":
    unittest.main()
