"""The colony asks a person to run a command, and has to get that right twice.

Twice, because two separate things were wrong and either one alone was enough to
make the feature useless.

The command has to be runnable. A build agent was told its command would start
at the top of the write scope and not to write a `cd`, so with the scope on
`job-search/` it handed over `py -m apply.main auto` -- and `apply` is a package
in `job-search/assisted-apply/`. Every approval died on `No module named
'apply'` before running a line of the thing it was meant to prove.

And it has to be asked once. Story #1 collected twelve run-request cards for
that same command. It was run, it was rejected, it was finally waived out loud,
and the next build raised it again every time, because nothing looked at the
answers already on the story before spending the PO's attention on the question
again.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from colony import build, db, runner

EXISTS = 'py -c "import os; print(os.path.exists(chr(109) + chr(46) + chr(116)))"'


class Folders(unittest.TestCase):
    """A projects root with a package one folder down, like the real one."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        (self.root / "proj" / "inner").mkdir(parents=True)
        (self.root / "elsewhere").mkdir()
        for p in (mock.patch.object(runner, "ROOT", self.root),
                  mock.patch.object(db, "PROJECTS_ROOT", self.root)):
            p.start()
            self.addCleanup(p.stop)


class WhereTheCommandRuns(Folders):

    def test_a_bare_command_runs_at_the_top_of_the_scope(self):
        command, where = runner.resolve_cd("py -m apply.main auto",
                                           self.root / "proj")
        self.assertEqual(command, "py -m apply.main auto")
        self.assertEqual(where, self.root / "proj")

    def test_a_cd_into_a_subfolder_moves_the_run_there(self):
        """The fix. This is the shape of every card story #1 ever raised."""
        command, where = runner.resolve_cd("cd inner; py -m apply.main auto",
                                           self.root / "proj")
        self.assertEqual(command, "py -m apply.main auto")
        self.assertEqual(where, self.root / "proj" / "inner")

    def test_a_cd_written_from_the_projects_root_lands_the_same_place(self):
        """An agent cannot tell which of the two folders it is being started in."""
        command, where = runner.resolve_cd("cd proj/inner && py -m apply.main auto",
                                           self.root / "proj")
        self.assertEqual(command, "py -m apply.main auto")
        self.assertEqual(where, self.root / "proj" / "inner")

    def test_a_cd_to_the_folder_it_is_already_in_is_just_dropped(self):
        command, where = runner.resolve_cd('cd "proj"; py -m thing',
                                           self.root / "proj")
        self.assertEqual(command, "py -m thing")
        self.assertEqual(where, self.root / "proj")

    def test_a_cd_out_of_the_write_scope_is_still_refused(self):
        """Descending is a detail of where the code sits. Leaving is not."""
        with self.assertRaises(runner.RunRefused) as caught:
            runner.resolve_cd("cd ../elsewhere; py -m thing", self.root / "proj")
        self.assertIn("outside", str(caught.exception))

    def test_a_cd_to_a_subfolder_that_is_not_there_says_so(self):
        with self.assertRaises(runner.RunRefused) as caught:
            runner.resolve_cd("cd nope; py -m thing", self.root / "proj")
        self.assertIn("no folder", str(caught.exception))

    def test_a_cd_and_nothing_after_it_is_not_a_command(self):
        with self.assertRaises(runner.RunRefused):
            runner.resolve_cd("cd inner;", self.root / "proj")

    def test_the_run_actually_happens_in_the_subfolder(self):
        """End to end through `execute`, because the cwd is the whole point."""
        (self.root / "proj" / "inner" / "m.t").write_text("here")
        result = runner.execute("cd inner; " + EXISTS, "proj")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["out"].strip(), "True")
        self.assertEqual(Path(result["cwd"]), self.root / "proj" / "inner")


class AskingTwice(Folders):
    """`_already_asked` is the thing that ends the twelve-card loop."""

    def setUp(self) -> None:
        super().setUp()
        self.conn = db.open_ledger(self.root / "ledger.db")
        self.addCleanup(self.conn.close)
        cur = self.conn.execute(
            "INSERT INTO stories (title, status, project, project_source, "
            "                     acceptance_criteria) "
            "VALUES ('a story', 'ready', 'proj', 'confirmed', 'prove the sync')")
        self.sid = int(cur.lastrowid)

    def card(self, command: str, *, resolved: bool, decision: str | None = None,
             dismissed: bool = False) -> None:
        self.conn.execute(
            "INSERT INTO escalations (story_id, kind, reason, proposal, "
            "                         resolved_at, po_decision, dismissed_at) "
            "VALUES (?, 'run-request', 'x', ?, ?, ?, ?)",
            (self.sid, json.dumps({"command": command, "project": "proj"}),
             "2026-09-08 10:00:00" if resolved else None, decision,
             "2026-09-08 10:00:00" if dismissed else None))

    def asked(self, command: str = "py -m apply.main auto",
              wrote_nothing: bool = False) -> str | None:
        return build._already_asked(self.conn, self.sid, command, wrote_nothing)

    def test_a_command_nobody_has_asked_about_goes_through(self):
        self.assertIsNone(self.asked())

    def test_the_same_command_is_not_raised_while_one_is_still_open(self):
        self.card("py -m apply.main auto", resolved=False)
        self.assertIn("Inbox", self.asked() or "")

    def test_a_rejected_command_is_never_raised_again(self):
        """The PO said no. Asking again is the colony not listening."""
        self.card("py -m apply.main auto", resolved=True, decision="reject")
        self.assertIn("declined", self.asked() or "")

    def test_a_dismissed_card_counts_as_declined(self):
        self.card("py -m apply.main auto", resolved=True, dismissed=True)
        self.assertIn("declined", self.asked() or "")

    def test_a_command_that_ran_is_not_re_raised_by_a_build_that_wrote_nothing(self):
        """Same tree, same command, same output. There is nothing to learn."""
        self.card("py -m apply.main auto", resolved=True, decision="approve")
        self.assertIn("already been run", self.asked(wrote_nothing=True) or "")

    def test_a_command_that_ran_may_be_asked_again_after_real_changes(self):
        """A build that wrote a patch has given the command something new to see."""
        self.card("py -m apply.main auto", resolved=True, decision="approve")
        self.assertIsNone(self.asked(wrote_nothing=False))

    def test_a_different_command_is_a_different_question(self):
        self.card("py -m apply.main auto", resolved=True, decision="reject")
        self.assertIsNone(self.asked("py -m apply.main backfill-locations"))

    def test_spacing_does_not_make_it_a_new_question(self):
        self.card("py  -m   apply.main auto", resolved=True, decision="reject")
        self.assertIn("declined", self.asked() or "")

    def test_a_story_with_no_id_is_left_alone(self):
        self.assertIsNone(build._already_asked(self.conn, 0, "py -m x", True))


class ApprovingARun(AskingTwice):
    """The command runs after the decision commits, never inside it."""

    def test_approve_queues_and_the_run_is_recorded_after(self):
        self.card(EXISTS, resolved=False)
        esc_id = self.conn.execute("SELECT max(id) FROM escalations").fetchone()[0]
        from colony import control
        with mock.patch.object(runner, "execute") as execute:
            self.conn.execute("BEGIN")
            out = control.decide(self.conn, esc_id, "approve")
            self.conn.execute("COMMIT")
            execute.assert_not_called()
        self.assertEqual(out["run_pending"], esc_id)

        result = control.run_command(self.conn, esc_id)
        self.assertTrue(result["ok"], result)
        self.conn.execute("BEGIN")
        said = control.record_run(self.conn, esc_id, result)
        self.conn.execute("COMMIT")
        self.assertIn("Exit 0", said)
        head = self.conn.execute(
            "SELECT summary FROM story_events WHERE story_id = ? ORDER BY id DESC LIMIT 1",
            (self.sid,)).fetchone()[0]
        self.assertIn("exit 0", head)

    def test_a_refused_command_keeps_the_card_open(self):
        self.card("git push origin main", resolved=False)
        esc_id = self.conn.execute("SELECT max(id) FROM escalations").fetchone()[0]
        from colony import control
        self.conn.execute("BEGIN")
        with self.assertRaises(control.Refused):
            control.decide(self.conn, esc_id, "approve")
        self.conn.execute("ROLLBACK")
        row = self.conn.execute("SELECT resolved_at FROM escalations WHERE id = ?",
                                (esc_id,)).fetchone()
        self.assertIsNone(row[0])
