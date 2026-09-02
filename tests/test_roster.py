"""Tests for the persona roster: two roots, and writing into exactly one of them.

The rule these all exist to pin is that `~/.agency-agents` is somebody else's
git clone and nothing in this project ever writes to it. Everything the
dashboard creates lands under `LOCAL_ROSTER_DIR`, which upstream has never heard
of, so a `git pull` in that clone can neither clobber a persona the user wrote
nor refuse to fast-forward past one.

The second rule is that a division name arrives from a web request and is used
as a path component. `_safe_name` is the whole of the defence, so it gets the
traversal cases rather than only the happy one.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from colony import roster

PERSONA = """---
name: Project Shepherd
description: Herds cross-functional chaos into on-time delivery.
color: blue
emoji: 🐑
vibe: Calm in a room that is not.
---
# Project Shepherd

You are **Project Shepherd**.
"""


def write(path: Path, text: str = PERSONA) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class Scanning(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.agency = Path(self.dir.name) / "agency"
        self.local = Path(self.dir.name) / "local"

    def test_both_roots_land_in_one_list(self):
        write(self.agency / "engineering" / "backend.md")
        write(self.local / "finance" / "analyst.md")
        found = {p["slug"]: p["source"] for p in roster.scan(self.agency, self.local)}
        self.assertEqual(found, {"engineering/backend": "agency",
                                 "finance/analyst": "local"})

    def test_a_missing_agency_clone_is_not_an_error(self):
        """The likely state for anyone who is not the PO."""
        write(self.local / "finance" / "analyst.md")
        found = roster.scan(self.agency, self.local)
        self.assertEqual([p["slug"] for p in found], ["finance/analyst"])

    def test_local_wins_a_slug_collision(self):
        """Shadowing an upstream persona is the supported way to override one."""
        write(self.agency / "engineering" / "backend.md")
        write(self.local / "engineering" / "backend.md",
              PERSONA.replace("Project Shepherd", "My Shepherd"))
        found = roster.scan(self.agency, self.local)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["source"], "local")
        self.assertEqual(found[0]["name"], "My Shepherd")

    def test_neither_root_is_a_real_error(self):
        """Silently empty would read as "the scan is broken" rather than "add one"."""
        with self.assertRaises(FileNotFoundError):
            roster.scan(self.agency, self.local)


class SafeNames(unittest.TestCase):
    """A division is typed into a web form and becomes a folder on disk."""

    def test_folds_rather_than_rejecting_ordinary_input(self):
        self.assertEqual(roster._safe_name("Data Engineering", "division"),
                         "data-engineering")
        self.assertEqual(roster._safe_name("  Finance  ", "division"), "finance")
        self.assertEqual(roster._safe_name("R&D // ops", "division"), "r-d-ops")

    def test_traversal_does_not_survive_the_fold(self):
        for evil in ("../..", "..\\..\\windows", "/etc/passwd", "..", "./."):
            name = roster._safe_name(evil, "division") if any(
                c in roster.SAFE for c in evil.lower()) else None
            if name is not None:
                self.assertNotIn("/", name)
                self.assertNotIn("\\", name)
                self.assertNotIn("..", name)

    def test_nothing_usable_is_refused(self):
        for empty in ("", "   ", "...", "///"):
            with self.assertRaises(roster.BadPersona):
                roster._safe_name(empty, "division")

    def test_a_reserved_folder_is_refused(self):
        # The scanner skips these, so a persona written into one would vanish.
        with self.assertRaises(roster.BadPersona):
            roster._safe_name("examples", "division")


class Writing(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name) / "local"

    def test_writes_a_file_the_scanner_can_read_back(self):
        roster.write_persona(division="Data Engineering", slug="Pipe Wrangler",
                             name="Pipe Wrangler", description="Moves rows.",
                             emoji="🔧", color="teal", vibe="Unblocks.",
                             body="# Pipe Wrangler\n\nYou are…", root=self.root)
        found = roster.scan(self.root / "nope", self.root)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["slug"], "data-engineering/pipe-wrangler")
        self.assertEqual(found[0]["name"], "Pipe Wrangler")
        self.assertEqual(found[0]["vibe"], "Unblocks.")
        self.assertEqual(found[0]["source"], "local")

    def test_governance_keys_do_not_survive_the_round_trip(self):
        """A persona file claiming tool access it does not have gets believed."""
        path = roster.write_persona(
            division="engineering", slug="sneaky", name="Sneaky",
            body="tools: Bash\nmodel: opus\n\n# body", root=self.root)
        text = path.read_text(encoding="utf-8")
        front = text.split("---")[1]
        self.assertNotIn("tools:", front)
        self.assertNotIn("model:", front)

    def test_a_multiline_description_is_folded(self):
        """`parse_persona` is a line reader; a newline here truncates the block."""
        path = roster.write_persona(
            division="engineering", slug="wordy", name="Wordy",
            description="one\ntwo\nthree", root=self.root)
        self.assertIn("description: one two three\n", path.read_text(encoding="utf-8"))

    def test_refuses_to_clobber_without_being_asked(self):
        roster.write_persona(division="engineering", slug="dup", name="First",
                             root=self.root)
        with self.assertRaises(roster.BadPersona):
            roster.write_persona(division="engineering", slug="dup", name="Second",
                                 root=self.root)
        roster.write_persona(division="engineering", slug="dup", name="Second",
                             overwrite=True, root=self.root)
        found = roster.scan(self.root / "nope", self.root)
        self.assertEqual(found[0]["name"], "Second")

    def test_a_nameless_persona_is_refused(self):
        with self.assertRaises(roster.BadPersona):
            roster.write_persona(division="engineering", slug="x", name="  ",
                                 root=self.root)

    def test_nothing_is_written_outside_the_root(self):
        for division in ("../../escape", "..", "/etc"):
            try:
                path = roster.persona_path(division, "x", root=self.root)
            except roster.BadPersona:
                continue
            self.assertTrue(path.resolve().is_relative_to(self.root.resolve()))

    def test_no_temp_file_is_left_behind(self):
        roster.write_persona(division="engineering", slug="clean", name="Clean",
                             root=self.root)
        leftovers = sorted(p.name for p in (self.root / "engineering").iterdir())
        self.assertEqual(leftovers, ["clean.md"])


class Deleting(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name) / "local"
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("CREATE TABLE roster (slug TEXT PRIMARY KEY, path TEXT, source TEXT)")
        self.addCleanup(self.conn.close)

    def row(self, slug, path, source):
        self.conn.execute("INSERT INTO roster VALUES (?,?,?)", (slug, str(path), source))

    def test_removes_the_file_and_the_row(self):
        path = roster.write_persona(division="engineering", slug="temp",
                                    name="Temp", root=self.root)
        self.row("engineering/temp", path, "local")
        roster.delete_persona(self.conn, "engineering/temp", root=self.root)
        self.assertFalse(path.exists())
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM roster WHERE slug='engineering/temp'").fetchone())

    def test_refuses_an_agency_persona(self):
        """Deleting one dirties a git clone the user did not think they were editing."""
        elsewhere = Path(self.dir.name) / "agency" / "engineering" / "theirs.md"
        write(elsewhere)
        self.row("engineering/theirs", elsewhere, "agency")
        with self.assertRaises(roster.BadPersona):
            roster.delete_persona(self.conn, "engineering/theirs", root=self.root)
        self.assertTrue(elsewhere.exists())

    def test_refuses_a_row_pointing_outside_the_root(self):
        """`path` is a column, and a column is a thing that can be wrong."""
        elsewhere = Path(self.dir.name) / "somewhere" / "else.md"
        write(elsewhere)
        self.row("engineering/odd", elsewhere, "local")
        with self.assertRaises(roster.BadPersona):
            roster.delete_persona(self.conn, "engineering/odd", root=self.root)
        self.assertTrue(elsewhere.exists())

    def test_an_unknown_slug_says_so(self):
        with self.assertRaises(roster.BadPersona):
            roster.delete_persona(self.conn, "engineering/ghost", root=self.root)


if __name__ == "__main__":
    unittest.main()
