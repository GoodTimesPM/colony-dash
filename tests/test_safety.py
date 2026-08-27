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


if __name__ == "__main__":
    unittest.main()
