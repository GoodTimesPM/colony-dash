"""The ledger: migrations, pragmas, and the environment lookup that finds the root.

The migration tests are the load-bearing ones. Every other guarantee in this
system is written down in SQL, and the append-only rule is what lets a checkout
at any commit open a ledger from an earlier one.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from colony import db


class LedgerCase(unittest.TestCase):
    """A throwaway ledger per test. Never the real one under `.colony/`."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "ledger.db"

    def tearDown(self) -> None:
        self._tmp.cleanup()


class TestMigrations(LedgerCase):

    def test_fresh_ledger_applies_every_migration_in_order(self):
        conn = db.connect(self.path)
        ran = db.migrate(conn, verbose=False)

        on_disk = sorted(p.name for p in db.MIGRATIONS_DIR.glob("*.sql"))
        self.assertEqual(ran, on_disk, "every migration should run on a fresh ledger")
        self.assertEqual(ran, sorted(ran), "migrations must apply in filename order")
        self.assertTrue(on_disk, "there should be migrations to apply at all")
        conn.close()

    def test_migrate_is_idempotent(self):
        conn = db.connect(self.path)
        first = db.migrate(conn, verbose=False)
        second = db.migrate(conn, verbose=False)

        self.assertTrue(first)
        self.assertEqual(second, [], "a second migrate should apply nothing")
        conn.close()

    def test_an_edited_migration_is_refused(self):
        """The sha256 guard. Editing an applied migration is the mistake that
        silently gives two machines different schemas, so it fails loudly."""
        conn = db.connect(self.path)
        db.migrate(conn, verbose=False)

        name = sorted(p.name for p in db.MIGRATIONS_DIR.glob("*.sql"))[0]
        conn.execute("UPDATE _migrations SET sha256 = 'not-the-real-digest' "
                     "WHERE filename = ?", (name,))

        with self.assertRaises(RuntimeError) as caught:
            db.migrate(conn, verbose=False)
        self.assertIn("append-only", str(caught.exception))
        conn.close()

    def test_a_superseded_digest_is_accepted_once_and_then_rewritten(self):
        """Comment-only rewrites of migrations that have already run.

        Ten of them happened at once when this repo was prepared to be
        published, on files that had been applied months earlier. The schema was
        untouched; the bytes above it were not, so the guard saw a fork.
        """
        conn = db.connect(self.path)
        db.migrate(conn, verbose=False)

        name = next(iter(db.SUPERSEDED))
        stale = db.SUPERSEDED[name][0]
        conn.execute("UPDATE _migrations SET sha256 = ? WHERE filename = ?",
                     (stale, name))

        self.assertEqual(db.migrate(conn, verbose=False), [])

        # And the row is current again, so the exact check is back on from the
        # next start. This is what keeps the list from growing into a hole.
        now = conn.execute("SELECT sha256 FROM _migrations WHERE filename = ?",
                           (name,)).fetchone()["sha256"]
        self.assertNotEqual(now, stale)
        self.assertEqual(db.migrate(conn, verbose=False), [])
        conn.close()

    def test_forgiveness_is_per_file(self):
        """A digest listed for one migration does not excuse another."""
        conn = db.connect(self.path)
        db.migrate(conn, verbose=False)

        listed = next(iter(db.SUPERSEDED))
        other = next(p.name for p in sorted(db.MIGRATIONS_DIR.glob("*.sql"))
                     if p.name != listed)
        conn.execute("UPDATE _migrations SET sha256 = ? WHERE filename = ?",
                     (db.SUPERSEDED[listed][0], other))

        with self.assertRaises(RuntimeError):
            db.migrate(conn, verbose=False)
        conn.close()

    def test_every_superseded_entry_names_a_migration_that_exists(self):
        """A typo here would be silent: the entry simply never matches."""
        on_disk = {p.name for p in db.MIGRATIONS_DIR.glob("*.sql")}
        for name in db.SUPERSEDED:
            self.assertIn(name, on_disk)

    def test_the_schema_has_the_tables_the_system_reads(self):
        conn = db.open_ledger(self.path)
        names = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}

        for table in ("sprints", "stories", "tickets", "runs", "pulses",
                      "agents", "console_turns", "console_state"):
            self.assertIn(table, names)
        conn.close()

    def test_console_state_has_exactly_one_row(self):
        """`console_state` is a singleton by CHECK constraint, and the console
        reads `id = 1` without looking first."""
        conn = db.open_ledger(self.path)
        rows = conn.execute("SELECT id FROM console_state").fetchall()
        self.assertEqual([r["id"] for r in rows], [1])

        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO console_state (id) VALUES (2)")
        conn.close()


    def test_over_budget_runs_keep_their_rows_under_the_new_name(self):
        """030 renames a status in place. A ledger from before it has runs
        recorded as `killed-over-budget`, and the rebuild must carry them over."""
        import shutil
        from unittest import mock

        older = Path(self._tmp.name) / "older"
        older.mkdir()
        for sql in db.MIGRATIONS_DIR.glob("*.sql"):
            if sql.name < "030":
                shutil.copy(sql, older / sql.name)

        conn = db.connect(self.path)
        try:
            with mock.patch.object(db, "MIGRATIONS_DIR", older):
                db.migrate(conn, verbose=False)
            conn.execute("INSERT INTO stories (id, title) VALUES (1, 't')")
            conn.execute("INSERT INTO tickets (id, story_id, title, intent) "
                         "VALUES (1, 1, 't', 'implement')")
            conn.execute("INSERT INTO runs (id, ticket_id, agent_role, model, status, "
                         "chargeable_tokens) VALUES (7, 1, 'dev', 'm', 'killed-over-budget', 99)")

            self.assertEqual(db.migrate(conn, verbose=False), ["030_over_budget_status.sql"])
            row = conn.execute(
                "SELECT status, chargeable_tokens FROM runs WHERE id = 7").fetchone()
            self.assertEqual((row["status"], row["chargeable_tokens"]), ("over-budget", 99))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE runs SET status = 'killed-over-budget' WHERE id = 7")

        finally:
            conn.close()

class TestConnection(LedgerCase):

    def test_connect_sets_the_pragmas_the_dashboard_depends_on(self):
        conn = db.connect(self.path)
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        fks = conn.execute("PRAGMA foreign_keys").fetchone()[0]

        self.assertEqual(journal.lower(), "wal",
                         "WAL is what lets the dashboard read while the pulse writes")
        self.assertEqual(fks, 1)
        conn.close()

    def test_a_read_only_connection_cannot_write(self):
        db.open_ledger(self.path).close()
        conn = db.connect(self.path, read_only=True)

        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("INSERT INTO console_state (id) VALUES (9)")
        conn.close()


class TestEnvValue(unittest.TestCase):
    """`db._env_value` is deliberately not `mirror.load_env`.

    `load_env` pulls the whole `.env` into `os.environ`, and `db` is imported by
    the console, which hands its environment to a `claude` subprocess. These
    tests pin the difference: read one key, mutate nothing.
    """

    def test_the_live_environment_wins(self):
        os.environ["COLONY_TEST_KEY"] = "from-the-shell"
        try:
            self.assertEqual(db._env_value("COLONY_TEST_KEY"), "from-the-shell")
        finally:
            del os.environ["COLONY_TEST_KEY"]

    def test_a_missing_key_is_none(self):
        self.assertIsNone(db._env_value("COLONY_KEY_THAT_DOES_NOT_EXIST"))

    def test_reading_one_key_does_not_load_the_rest_of_the_env(self):
        """The leak this function exists to prevent.

        If `.env` is present and holds a token, looking up an unrelated key must
        not put that token in `os.environ` where a subprocess would inherit it.
        """
        before = set(os.environ)
        db._env_value("COLONY_PROJECTS_ROOT")
        self.assertEqual(set(os.environ), before,
                         "_env_value must not add anything to the environment")
        self.assertNotIn("NOTION_TOKEN", os.environ)


class TestProjectsRoot(unittest.TestCase):

    def test_the_default_root_contains_this_checkout(self):
        """The colony watches the folder its checkout sits in. This is the
        assertion that replaced a hardcoded drive letter."""
        self.assertEqual(db.PROJECTS_ROOT, db.PROJECT_DIR.parent)

    def test_the_env_override_moves_every_derived_root(self):
        """Six modules derive their root from `db.PROJECTS_ROOT`. Checked in a
        subprocess because they all read it at import time."""
        target = Path(tempfile.gettempdir()).resolve() / "colony-root-probe"
        env = dict(os.environ, COLONY_PROJECTS_ROOT=target.as_posix())
        code = (
            "from colony import db, control, projects, seed;"
            "print(db.PROJECTS_ROOT.as_posix());"
            "print(control.ROOT_POSIX);"
            "print(projects.ROOT.as_posix());"
            "print(seed.READ_SCOPE[0])"
        )
        out = subprocess.run(
            [sys.executable, "-c", code], env=env, capture_output=True, text=True,
            cwd=str(db.PROJECT_DIR), timeout=120,
        )
        self.assertEqual(out.returncode, 0, out.stderr)

        root, control_root, projects_root, read_scope = out.stdout.strip().splitlines()
        self.assertEqual(root, target.as_posix())
        self.assertEqual(control_root, target.as_posix())
        self.assertEqual(projects_root, target.as_posix())
        self.assertEqual(read_scope, f"{target.as_posix()}/**")


if __name__ == "__main__":
    unittest.main()
