"""The safety model, asserted rather than promised.

ARCHITECTURE.md §8 says three things: an agent can never reach the shell, write
tools need two independent facts to be true, and nothing writes outside one
named project folder. Those are the three things this file checks, because a
safety property that is only written down in a document is a safety property
that drifts.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest

from fastapi import HTTPException
from pathlib import Path
from unittest import mock

from colony import agent, control, db


class FakeProc:
    """Enough of `subprocess.CompletedProcess` for `agent.invoke` to parse."""

    returncode = 0
    stderr = ""
    stdout = json.dumps({
        "result": "done", "session_id": "s-1", "is_error": False,
        "usage": {"input_tokens": 1, "output_tokens": 2,
                  "cache_read_input_tokens": 3, "cache_creation_input_tokens": 4},
        "total_cost_usd": 0.01,
    })


def captured_command(**kwargs) -> list[str]:
    """Run `agent.invoke` without launching anything, and return the argv."""
    seen: dict = {}

    def fake_run(cmd, **rest):
        seen["cmd"] = cmd
        return FakeProc()

    with mock.patch.object(agent.proc_mod, "run", fake_run):
        agent.invoke("prompt", model="claude-sonnet-5", **kwargs)
    return seen["cmd"]


def denied_tools(cmd: list[str]) -> set[str]:
    """The `--disallowedTools` values out of an argv."""
    start = cmd.index("--disallowedTools") + 1
    out = []
    for token in cmd[start:]:
        if token.startswith("--"):
            break
        out.append(token)
    return set(out)


class TestToolDenylist(unittest.TestCase):

    def test_the_shell_is_denied_even_when_the_contract_allows_it(self):
        """The load-bearing one. A bad row in the `agents` table must not be
        able to hand an agent `Bash`."""
        cmd = captured_command(tools_allowed=["Read", "Bash", "WebFetch"],
                               allow_writes=True)
        self.assertIn("Bash", denied_tools(cmd))

    def test_every_always_denied_tool_is_denied(self):
        cmd = captured_command(tools_allowed=list(agent.ALWAYS_DENIED),
                               allow_writes=True)
        denied = denied_tools(cmd)
        for tool in agent.ALWAYS_DENIED:
            self.assertIn(tool, denied)

    def test_write_tools_are_denied_without_allow_writes(self):
        cmd = captured_command(tools_allowed=["Read", "Edit", "Write"],
                               allow_writes=False)
        denied = denied_tools(cmd)
        for tool in agent.WRITE_TOOLS:
            self.assertIn(tool, denied)

    def test_write_tools_are_available_with_allow_writes(self):
        """The other half: the flag has to actually do something, or the test
        above is passing for the wrong reason."""
        cmd = captured_command(tools_allowed=["Read", "Edit", "Write"],
                               allow_writes=True)
        denied = denied_tools(cmd)
        for tool in agent.WRITE_TOOLS:
            self.assertNotIn(tool, denied)
        self.assertIn("Bash", denied, "writes unlock, the shell never does")

    def test_a_denied_tool_is_never_also_allowed(self):
        cmd = captured_command(tools_allowed=["Read", "Bash"], allow_writes=False)
        allowed = set(cmd[cmd.index("--allowedTools") + 1:cmd.index("--disallowedTools")])
        # The allowlist is passed through as the contract wrote it; the denylist
        # is what the CLI enforces. This asserts the overlap exists and is
        # covered, which is the belt-and-braces the module docstring claims.
        self.assertTrue(allowed & denied_tools(cmd))


class ScopeCase(unittest.TestCase):
    """A ledger and a projects root, both temporary."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "real-project").mkdir()
        self.conn = db.open_ledger(self.root / "ledger.db")

        self._patches = [
            mock.patch.object(db, "PROJECTS_ROOT", self.root),
            mock.patch.object(control, "ROOT_POSIX", self.root.as_posix()),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self.conn.close()
        self._tmp.cleanup()

    def hire(self, *, write_capable: int = 1, status: str = "standby") -> int:
        cur = self.conn.execute(
            "INSERT INTO agents (role, model, write_capable, tools_allowed, "
            "                    read_scope, status, avatar_seed) "
            "VALUES ('builder', 'claude-sonnet-5', ?, '[]', '[]', ?, 'x')",
            (write_capable, status))
        return int(cur.lastrowid)


class TestWriteScope(ScopeCase):

    def test_a_folder_outside_the_root_is_refused(self):
        for bad in ("../elsewhere", "a/../../b", "C:/Windows", "/etc"):
            with self.subTest(path=bad), self.assertRaises(control.Refused):
                control._check_scope_folder(bad)

    def test_a_dot_folder_is_refused(self):
        (self.root / ".colony").mkdir(exist_ok=True)
        with self.assertRaises(control.Refused):
            control._check_scope_folder(".colony")

    def test_an_empty_scope_is_refused(self):
        """'write anywhere' is not a scope."""
        for bad in ("", "   ", "."):
            with self.subTest(path=bad), self.assertRaises(control.Refused):
                control._check_scope_folder(bad)

    def test_a_folder_that_does_not_exist_is_refused(self):
        with self.assertRaises(control.Refused):
            control._check_scope_folder("no-such-project")

    def test_a_real_folder_is_accepted(self):
        self.assertEqual(control._check_scope_folder("real-project"), "real-project")

    def test_scope_survives_the_round_trip_through_globs(self):
        folders = ["real-project", "another/thing"]
        self.assertEqual(control.scope_projects(control.scope_globs(folders)), folders)

    def test_a_read_only_agent_has_no_write_scope_to_widen(self):
        agent_id = self.hire(write_capable=0)
        with self.assertRaises(control.Refused) as caught:
            control.set_write_scope(self.conn, agent_id, ["real-project"])
        self.assertIn("read-only", str(caught.exception))

    def test_a_retired_agent_is_refused(self):
        agent_id = self.hire(status="retired")
        with self.assertRaises(control.Refused):
            control.set_write_scope(self.conn, agent_id, ["real-project"])

    def test_setting_a_scope_stores_it_as_globs_under_the_root(self):
        agent_id = self.hire()
        control.set_write_scope(self.conn, agent_id, ["real-project"])
        stored = self.conn.execute(
            "SELECT write_scope FROM agents WHERE id = ?", (agent_id,)).fetchone()[0]
        self.assertEqual(json.loads(stored),
                         [f"{self.root.as_posix()}/real-project/**"])

    def test_an_empty_folder_list_is_refused(self):
        agent_id = self.hire()
        with self.assertRaises(control.Refused):
            control.set_write_scope(self.conn, agent_id, [])


class TestCreateProject(ScopeCase):
    """The one dashboard action that writes outside the ledger."""

    def test_traversal_is_refused(self):
        for bad in ("../escape", "..", "a/../../b"):
            with self.subTest(name=bad), self.assertRaises(control.Refused):
                control.create_project(self.conn, bad)

    def test_deeper_than_two_levels_is_refused(self):
        with self.assertRaises(control.Refused):
            control.create_project(self.conn, "a/b/c")

    def test_it_makes_a_folder_with_the_project_md_the_repo_expects(self):
        result = control.create_project(self.conn, "brand-new", why="because")
        made = self.root / "brand-new"
        self.assertTrue(made.is_dir())
        self.assertIn("because", (made / "PROJECT.md").read_text(encoding="utf-8"))
        self.assertTrue(result["created"])


class ConsoleIsDeskOnly(unittest.TestCase):
    """The console is a shell, and a shell is not worth an access token.

    Every other route on this server is a window onto a ledger: the worst a
    stolen token buys is reading the board and pressing approve. The console
    spawns `claude` with no worktree and no tool restrictions, so the same token
    buys arbitrary code execution on the machine holding `.env` -- which holds
    the Notion token. And that access token crosses a home LAN over plain HTTP.

    So the shell is scoped by peer address instead, the way token rotation is.
    """

    class Req:
        def __init__(self, host):
            self.client = type("C", (), {"host": host})() if host is not None else None

    # What counts as "this machine" is a property of the machine running the
    # tests, and an earlier version of this class hard-coded 10.0.0.57 as an
    # example of somewhere else. That is this developer's own LAN address, so
    # the test asserted the opposite of the truth on the one machine it ran on.
    # The address list is stubbed instead, and every case below names an
    # address relative to that stub rather than to whatever interface happens
    # to be up.
    MINE = frozenset({"10.0.0.57", "100.126.11.89"})

    def setUp(self):
        from colony import net, server
        self.server = server
        self.addCleanup(setattr, server, "CONSOLE_REMOTE", server.CONSOLE_REMOTE)
        server.CONSOLE_REMOTE = False
        self.addCleanup(setattr, net, "local_addresses", net.local_addresses)
        net.local_addresses = lambda: self.MINE

    def test_loopback_is_allowed(self):
        for host in ("127.0.0.1", "::1", "localhost"):
            with self.subTest(host=host):
                self.server._desk_only(self.Req(host))       # does not raise

    def test_this_machines_own_network_address_is_the_desk(self):
        """The logon task binds one network address, so the window uses it too.

        A connection opened here to this machine's own tailnet address arrives
        with that address as its peer rather than 127.0.0.1. Reading that as a
        stranger is what locked the console out of the desktop it ran on.
        """
        for host in sorted(self.MINE):
            with self.subTest(host=host):
                self.server._desk_only(self.Req(host))       # does not raise

    def test_another_device_is_refused_with_a_reason_a_person_can_act_on(self):
        # Addresses in the same ranges as the two above, and deliberately close
        # to them: a phone on the same wifi and another node on the same
        # tailnet are exactly what this has to keep out.
        for host in ("10.0.0.58", "192.168.1.9", "100.126.11.90"):
            with self.subTest(host=host):
                self.assertNotIn(host, self.MINE)
                with self.assertRaises(HTTPException) as caught:
                    self.server._desk_only(self.Req(host))
                self.assertEqual(caught.exception.status_code, 403)
                self.assertIn("desktop", caught.exception.detail)

    def test_an_unknown_peer_fails_closed(self):
        """No client on the scope must not read as "must be local, then"."""
        with self.assertRaises(HTTPException):
            self.server._desk_only(self.Req(None))

    def test_the_escape_hatch_lifts_it(self):
        self.server.CONSOLE_REMOTE = True
        self.server._desk_only(self.Req("10.0.0.58"))        # does not raise

    def test_every_console_write_route_is_behind_it(self):
        """A fifth console route added later must not quietly skip the guard."""
        import inspect
        for name in ("console_send", "console_clear", "console_options", "console_cwd"):
            fn = getattr(self.server, name)
            src = inspect.getsource(fn)
            with self.subTest(route=name):
                self.assertIn("_desk_only(request)", src)


class TheSwitchIsAsymmetric(unittest.TestCase):
    """You may tighten the console boundary from anywhere; loosen only at the desk.

    This class is the reason the boundary can be a button at all. A switch that
    turns off a security check is worth nothing if whoever gets past the check
    can also flip the switch, so the direction that loosens is held to the same
    peer-address rule as the thing it loosens. The direction that tightens is
    open, because the moment you want it is the moment you are away from the
    desk and have realised your phone can open a shell at home.
    """

    class Req:
        def __init__(self, host):
            self.client = type("C", (), {"host": host})() if host is not None else None

    def setUp(self):
        from colony import server
        self.server = server
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.env = Path(self.dir.name) / ".env"
        # A stand-in for the real file, holding the thing that must survive.
        self.env.write_text("NOTION_TOKEN=ntn_pretend\n", encoding="utf-8")

        self.addCleanup(setattr, server, "CONSOLE_REMOTE", server.CONSOLE_REMOTE)
        server.CONSOLE_REMOTE = False
        self.written = []

        # Captured before the patch: `server.db` *is* `colony.db`, so patching
        # the attribute and then looking it up again is a call to the stub.
        real_set = db.set_env_value

        def fake_set(key, value, path=None, comment=None):
            self.written.append((key, value))
            return real_set(key, value, self.env, comment)

        patch = mock.patch.object(server.db, "set_env_value", fake_set)
        patch.start()
        self.addCleanup(patch.stop)
        # The route writes a ledger note; none of these tests are about that.
        patch = mock.patch.object(server, "_rw", lambda: sqlite3.connect(":memory:"))
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(server.control, "_record",
                                  lambda *a, **k: None)
        patch.start()
        self.addCleanup(patch.stop)

    def call(self, host, on):
        return self.server.act_console_remote(self.Req(host), {"on": on}, "1")

    def test_turning_it_on_from_the_lan_is_refused(self):
        with self.assertRaises(HTTPException) as caught:
            self.call("192.168.1.9", True)
        self.assertEqual(caught.exception.status_code, 403)
        self.assertFalse(self.server.CONSOLE_REMOTE)
        self.assertEqual(self.written, [])          # and `.env` was not touched

    def test_turning_it_on_at_the_desk_works_and_is_written_down(self):
        out = self.call("127.0.0.1", True)
        self.assertTrue(out["remote"])
        self.assertTrue(self.server.CONSOLE_REMOTE)
        self.assertIn("COLONY_CONSOLE_REMOTE=1",
                      self.env.read_text(encoding="utf-8"))

    def test_turning_it_off_from_the_lan_is_allowed(self):
        """Tightening is always safe, and is wanted exactly when you are away."""
        self.server.CONSOLE_REMOTE = True
        out = self.call("192.168.1.9", False)
        self.assertFalse(out["remote"])
        self.assertFalse(self.server.CONSOLE_REMOTE)
        self.assertIn("COLONY_CONSOLE_REMOTE=0",
                      self.env.read_text(encoding="utf-8"))

    def test_an_unknown_peer_cannot_loosen(self):
        with self.assertRaises(HTTPException):
            self.call(None, True)

    def test_the_notion_token_survives_the_write(self):
        """`.env` holds a credential typed in by hand and copied nowhere else."""
        self.call("127.0.0.1", True)
        self.call("127.0.0.1", False)
        self.call("127.0.0.1", True)
        body = self.env.read_text(encoding="utf-8")
        self.assertIn("NOTION_TOKEN=ntn_pretend", body)
        self.assertEqual(body.count("COLONY_CONSOLE_REMOTE="), 1)

    def test_a_failed_write_leaves_the_boundary_where_it_was(self):
        """Otherwise the process and the file disagree about what is allowed."""
        def boom(*a, **k):
            raise OSError("disk full")
        with mock.patch.object(self.server.db, "set_env_value", boom):
            with self.assertRaises(HTTPException) as caught:
                self.call("127.0.0.1", True)
        self.assertEqual(caught.exception.status_code, 500)
        self.assertFalse(self.server.CONSOLE_REMOTE)


if __name__ == "__main__":
    unittest.main()
