"""More than one specialist on one story.

For most of this project's life a project folder could hold exactly one
write-capable agent, forever. Nobody decided that. It fell out of one clause in
`stories_to_staff` -- skip any story whose PROJECT already has a writer -- and
its consequences reached everywhere: the second story in a folder never got
staffed, dispatch always handed the ticket to the oldest contract on the folder
whoever it was cut for, and the dashboard could only render a count.

These tests pin the replacement. A contract now records the story it was cut for
and the seat it holds, staffing asks whether THIS STORY has somebody, and
dispatch hands the ticket to seat 0. The backfill test is the load-bearing one:
without it, the day the guard changed, every story with a working agent would
have read as unstaffed and the next pulse would have paid to re-hire the people
already doing the work.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from colony import control, db, wake


class Ledger(unittest.TestCase):
    """A throwaway ledger with a projects root beside it. Never the real one."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "proj-a").mkdir()
        (self.root / "proj-b").mkdir()
        self.conn = db.open_ledger(self.root / "ledger.db")
        self.addCleanup(self.conn.close)
        for p in (mock.patch.object(db, "PROJECTS_ROOT", self.root),
                  mock.patch.object(control, "ROOT_POSIX", self.root.as_posix())):
            p.start()
            self.addCleanup(p.stop)

    def persona(self, slug: str, division: str = "engineering") -> str:
        self.conn.execute(
            "INSERT INTO roster (slug, name, division, description, source, path, "
            "                    body_hash) "
            "VALUES (?,?,?,'','agency','x','h')",
            (slug, slug.split("/")[-1].replace("-", " ").title(), division))
        return slug

    def story(self, project: str = "proj-a", status: str = "ready") -> int:
        cur = self.conn.execute(
            "INSERT INTO stories (title, status, project, project_source, "
            "                     acceptance_criteria) "
            "VALUES ('a story', ?, ?, 'confirmed', 'do the thing')",
            (status, project))
        return int(cur.lastrowid)


class StaffingIsPerStory(Ledger):

    def test_a_second_story_in_a_staffed_folder_still_gets_staffed(self):
        """The whole bug, in one assertion.

        Under the old guard the second story here was invisible to staffing for
        as long as the first story's agent stayed hired, which in practice was
        forever -- nothing retires an agent automatically.
        """
        first, second = self.story(), self.story()
        control.hire(self.conn, roster_slug=None, role="builder", project="proj-a",
                     write_capable=True, story_id=first)

        waiting = [s["id"] for s in wake.stories_to_staff(self.conn, limit=10)]
        self.assertEqual(waiting, [second])

    def test_a_story_that_already_has_its_own_agent_is_left_alone(self):
        story = self.story()
        control.hire(self.conn, roster_slug=None, role="builder", project="proj-a",
                     write_capable=True, story_id=story)
        self.assertEqual(wake.stories_to_staff(self.conn, limit=10), [])

    def test_a_hand_hire_with_no_story_does_not_hold_the_folder(self):
        """The folder-wide rule, coming back in through the side door.

        This asserted the opposite until `og-tracker-sync-verifier` proved the
        cost. It was hired onto `job-search` with no story, which made every
        story in `job-search` read as staffed, so the PO's "hire a specialist
        for each of items 12, 14 and 15" could not happen -- every dispatch
        re-used the one verifier hired weeks earlier for a different question.

        A hire with no story is a writer the folder can fall back on, and
        `control.team` still ranks it last behind anyone hired for the
        story. It is not evidence that this story has who it needs.
        """
        story = self.story()
        control.hire(self.conn, roster_slug=None, role="builder", project="proj-a",
                     write_capable=True)
        self.assertEqual([s["id"] for s in wake.stories_to_staff(self.conn, 10)],
                         [story])
        # Still dispatchable in the meantime, which is why this is safe.
        self.assertEqual([a["role"] for a in control.team(self.conn, story)],
                         ["builder"])

    def test_a_retired_agent_does_not_hold_a_story(self):
        story = self.story()
        out = control.hire(self.conn, roster_slug=None, role="builder",
                           project="proj-a", write_capable=True, story_id=story)
        control.retire(self.conn, out["agent_id"])
        self.assertEqual([s["id"] for s in wake.stories_to_staff(self.conn, 10)], [story])

    def test_an_agent_on_another_project_holds_nothing_here(self):
        mine = self.story(project="proj-a")
        control.hire(self.conn, roster_slug=None, role="builder", project="proj-b",
                     write_capable=True)
        self.assertEqual([s["id"] for s in wake.stories_to_staff(self.conn, 10)], [mine])


class TheTeamOnAStory(Ledger):

    def test_seats_come_back_lead_first(self):
        story = self.story()
        control.hire(self.conn, roster_slug=None, role="reviewer-seat",
                     project="proj-a", write_capable=True, story_id=story, seat=2)
        control.hire(self.conn, roster_slug=None, role="lead-seat",
                     project="proj-a", write_capable=True, story_id=story, seat=0)
        self.assertEqual([a["role"] for a in control.team(self.conn, story)],
                         ["lead-seat", "reviewer-seat"])

    def test_a_deliberate_hire_outranks_an_inherited_one(self):
        """A story-scoped contract was cut for this work. A project-scoped one
        was cut for the folder. When both exist the specific one leads."""
        story = self.story()
        control.hire(self.conn, roster_slug=None, role="folder-wide",
                     project="proj-a", write_capable=True)
        control.hire(self.conn, roster_slug=None, role="on-this-story",
                     project="proj-a", write_capable=True, story_id=story)
        self.assertEqual([a["role"] for a in control.team(self.conn, story)],
                         ["on-this-story", "folder-wide"])

    def test_another_storys_agent_is_not_on_this_team(self):
        mine, theirs = self.story(), self.story()
        control.hire(self.conn, roster_slug=None, role="not-mine", project="proj-a",
                     write_capable=True, story_id=theirs)
        self.assertEqual(control.team(self.conn, mine), [])

    def test_a_story_moved_to_another_folder_leaves_its_team_behind(self):
        """The write scope on those rows names the old folder. Carrying the
        contract across would be authorising a write outside the project."""
        story = self.story(project="proj-a")
        control.hire(self.conn, roster_slug=None, role="builder", project="proj-a",
                     write_capable=True, story_id=story)
        self.conn.execute("UPDATE stories SET project = 'proj-b' WHERE id = ?", (story,))
        self.assertEqual(control.team(self.conn, story), [])

    def test_read_only_agents_are_not_on_the_team(self):
        """`investigator` and `reviewer` carry no project and never write."""
        story = self.story()
        control.hire(self.conn, roster_slug=None, role="looker", project="proj-a",
                     write_capable=False, story_id=story)
        self.assertEqual(control.team(self.conn, story), [])


class DispatchGoesToTheLead(Ledger):

    def dispatch(self, story_id: int):
        return control.dispatch(self.conn, story_id)

    def test_the_ticket_goes_to_seat_zero_not_to_the_oldest_row(self):
        """The old query was `ORDER BY id LIMIT 1`, so the first agent ever
        hired onto a folder took every ticket in it regardless of the work."""
        story = self.story()
        control.hire(self.conn, roster_slug=None, role="hired-first",
                     project="proj-a", write_capable=True, story_id=story, seat=1)
        control.hire(self.conn, roster_slug=None, role="the-lead",
                     project="proj-a", write_capable=True, story_id=story, seat=0)

        self.dispatch(story)
        ticket = self.conn.execute(
            "SELECT role FROM tickets WHERE story_id = ? AND intent = 'implement'",
            (story,)).fetchone()
        self.assertEqual(ticket["role"], "the-lead")

    def test_an_unstaffed_story_is_still_refused(self):
        story = self.story()
        with self.assertRaises(control.Refused):
            self.dispatch(story)

    def test_an_agent_hired_for_another_story_cannot_be_dispatched_to(self):
        mine, theirs = self.story(), self.story()
        control.hire(self.conn, roster_slug=None, role="theirs", project="proj-a",
                     write_capable=True, story_id=theirs)
        with self.assertRaises(control.Refused):
            self.dispatch(mine)


class TheBackfill(unittest.TestCase):
    """Migration 028 against a ledger that already has hires in it.

    Built by running every migration up to 027, inserting the shape the real
    ledger was in, then letting 028 run. Doing it any other way would test the
    schema rather than the upgrade, and the upgrade is the risky half: get the
    backfill wrong and the first pulse after it re-hires everyone.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "ledger.db"

    def before_teams(self) -> sqlite3.Connection:
        """A ledger on the last schema that had no seats.

        The real sha256 of each file goes into `_migrations`, not a placeholder.
        `db.migrate` refuses to run against a recorded hash it does not
        recognise, so a fake one turns the very next call into the append-only
        error instead of applying the migration this class exists to test.
        """
        import hashlib

        conn = db.connect(self.path)
        self.addCleanup(conn.close)
        db._ensure_migrations_table(conn)
        conn.execute("PRAGMA foreign_keys = OFF")
        for f in sorted(q for q in db.MIGRATIONS_DIR.glob("*.sql")
                        if q.name < "028_agent_teams.sql"):
            text = f.read_text(encoding="utf-8")
            conn.executescript(f"BEGIN;\n{text}\nCOMMIT;")
            conn.execute("INSERT INTO _migrations (filename, sha256) VALUES (?, ?)",
                         (f.name, hashlib.sha256(text.encode("utf-8")).hexdigest()))
        conn.execute("PRAGMA foreign_keys = ON")
        conn.commit()
        return conn

    def test_an_approved_hire_gives_its_agent_the_story_it_was_cut_for(self):
        conn = self.before_teams()
        conn.execute("INSERT INTO stories (id, title, status) VALUES (7, 's', 'ready')")
        conn.execute(
            "INSERT INTO agents (role, project, model, write_capable, tools_allowed, "
            "                    read_scope, avatar_seed, status) "
            "VALUES ('builder', 'proj-a', 'm', 1, '[]', '[]', 'x', 'standby')")
        conn.execute(
            """INSERT INTO escalations (story_id, kind, reason, po_decision, proposal)
               VALUES (7, 'hire', 'Hire?', 'approve',
                       '{"role":"builder","project":"proj-a"}')""")
        conn.commit()

        # The migration under test, plus whatever follows it.
        db.migrate(conn, verbose=False)

        got = conn.execute("SELECT story_id, seat FROM agents "
                           "WHERE role = 'builder'").fetchone()
        self.assertEqual(got["story_id"], 7)
        self.assertEqual(got["seat"], 0, "an existing hire is the lead of its story")

        # And the point of all of it: that story must not read as unstaffed.
        conn.execute("UPDATE stories SET project = 'proj-a', project_source = 'confirmed', "
                     "acceptance_criteria = 'x' WHERE id = 7")
        self.assertEqual(wake.stories_to_staff(conn, 10), [])

    def test_an_agent_whose_hire_was_never_approved_keeps_no_story(self):
        """A rejected proposal is not a contract. Reading one as a backfill
        source would attach an agent to work nobody agreed to give it."""
        conn = self.before_teams()
        conn.execute("INSERT INTO stories (id, title, status) VALUES (9, 's', 'ready')")
        conn.execute(
            "INSERT INTO agents (role, project, model, write_capable, tools_allowed, "
            "                    read_scope, avatar_seed, status) "
            "VALUES ('builder', 'proj-a', 'm', 1, '[]', '[]', 'x', 'standby')")
        conn.execute(
            """INSERT INTO escalations (story_id, kind, reason, po_decision, proposal)
               VALUES (9, 'hire', 'Hire?', 'reject',
                       '{"role":"builder","project":"proj-a"}')""")
        conn.commit()
        db.migrate(conn, verbose=False)
        got = conn.execute("SELECT story_id FROM agents WHERE role = 'builder'").fetchone()
        self.assertIsNone(got["story_id"])

    def test_the_structural_agents_are_left_alone(self):
        """`investigator` and `reviewer` carry project NULL and belong to no
        story. The backfill is scoped to project-bearing rows for that reason."""
        conn = db.open_ledger(self.path)
        self.addCleanup(conn.close)
        conn.execute(
            "INSERT INTO agents (role, model, write_capable, tools_allowed, "
            "                    read_scope, avatar_seed, status) "
            "VALUES ('investigator', 'm', 0, '[]', '[]', 'x', 'active')")
        got = conn.execute("SELECT story_id, seat FROM agents "
                           "WHERE role = 'investigator'").fetchone()
        self.assertIsNone(got["story_id"])
        self.assertEqual(got["seat"], 0)


class ProposingACrew(Ledger):
    """`staff_stories` with the model's answer faked, so the shaping is what is
    under test rather than anything that costs tokens."""

    TERMS = {"role": "investigator", "model": "claude-sonnet-5",
             "tools_allowed": ["Read"], "max_tokens_run": 400000}

    def run_with(self, payload, story_id=None):
        story_id = story_id or self.story()

        class Result:
            status = "ok"
            chargeable_tokens = 1234
            error = None
            text = ""

            def json_payload(self):
                return payload

        with mock.patch.object(wake.agent, "run_ticket", lambda *a, **k: Result()), \
             mock.patch.object(wake.attach, "for_story", lambda *a, **k: []):
            return wake.staff_stories(self.conn, self.TERMS), story_id

    def escalations(self, story_id):
        return self.conn.execute(
            "SELECT * FROM escalations WHERE story_id = ? AND kind = 'hire' ORDER BY id",
            (story_id,)).fetchall()

    def test_a_three_seat_crew_raises_three_separate_cards(self):
        """One card per seat, so some can be taken and others refused. A single
        bundled yes/no would make 'no' the cheapest answer to any crew."""
        for slug, div in (("engineering/backend", "engineering"),
                          ("testing/qa", "testing"),
                          ("design/ux", "design")):
            self.persona(slug, div)
        out, story = self.run_with({"team": [
            {"roster_slug": "engineering/backend", "role": "lead", "why": "the code"},
            {"roster_slug": "testing/qa", "role": "qa", "why": "the tests"},
            {"roster_slug": "design/ux", "role": "ux", "why": "the screens"},
        ]})
        seats = [c["seat"] for c in
                 (__import__("json").loads(e["proposal"]) for e in self.escalations(story))]
        self.assertEqual(seats, [0, 1, 2])
        self.assertEqual(len(out), 1)
        self.assertEqual(len(out[0]["crew"]), 3)

    def test_nothing_is_hired(self):
        """The docstring's promise, asserted. Proposing is not hiring, and the
        gap between the two is the PO."""
        self.persona("engineering/backend")
        self.run_with({"team": [{"roster_slug": "engineering/backend", "role": "lead"}]})
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM agents").fetchone()[0], 0)

    def test_the_old_single_slug_answer_still_works(self):
        """A model that has read this repo's history will sometimes reply in the
        shape the prompt used to ask for. Losing a paid run to that would be an
        expensive way to be right about a schema."""
        self.persona("engineering/backend")
        out, story = self.run_with({"roster_slug": "engineering/backend", "role": "solo"})
        self.assertEqual(len(self.escalations(story)), 1)
        self.assertEqual(out[0]["role"], "solo")

    def test_a_crew_is_capped(self):
        for i in range(6):
            self.persona(f"engineering/e{i}")
        _, story = self.run_with({"team": [
            {"roster_slug": f"engineering/e{i}", "role": f"r{i}"} for i in range(6)]})
        self.assertEqual(len(self.escalations(story)), wake.STAFF_TEAM_MAX)

    def test_two_seats_that_ask_for_the_same_role_name_do_not_collide(self):
        """`hire` refuses a duplicate role on a project, and it refuses it at
        approval time -- so an un-deduped pair would look fine in the Inbox and
        fail on the second click."""
        self.persona("engineering/one")
        self.persona("testing/two", "testing")
        _, story = self.run_with({"team": [
            {"roster_slug": "engineering/one", "role": "builder"},
            {"roster_slug": "testing/two", "role": "builder"},
        ]})
        import json as _json
        roles = [_json.loads(e["proposal"])["role"] for e in self.escalations(story)]
        self.assertEqual(roles, ["builder", "builder-2"])

    def test_an_invented_slug_is_dropped_and_the_real_ones_survive(self):
        self.persona("engineering/real")
        _, story = self.run_with({"team": [
            {"roster_slug": "engineering/real", "role": "lead"},
            {"roster_slug": "engineering/does-not-exist", "role": "ghost"},
        ]})
        import json as _json
        slugs = [_json.loads(e["proposal"])["roster_slug"] for e in self.escalations(story)]
        self.assertEqual(slugs, ["engineering/real"])
        found = self.conn.execute(
            "SELECT findings FROM tickets WHERE intent = 'research'").fetchone()
        self.assertIn("does-not-exist", found["findings"])

    def test_a_crew_of_nothing_but_invented_slugs_blocks_the_ticket(self):
        # A persona has to exist or the roster digest is empty and staffing
        # never gets as far as reading an answer.
        self.persona("engineering/real")
        _, story = self.run_with({"team": [{"roster_slug": "nope/nope", "role": "x"}]})
        self.assertEqual(self.escalations(story), [])
        self.assertEqual(self.conn.execute(
            "SELECT status FROM tickets WHERE intent = 'research'").fetchone()["status"],
            "blocked")

    def test_the_proposal_carries_the_story_so_approval_lands_on_a_seat(self):
        """The escalation's own `story_id` says which card this is. The
        proposal's `story_id` is an argument to `hire`. They are not the same
        field and dropping the second one is how a crew becomes three agents
        all scoped to the folder."""
        self.persona("engineering/backend")
        _, story = self.run_with({"team": [
            {"roster_slug": "engineering/backend", "role": "lead"}]})
        import json as _json
        proposal = _json.loads(self.escalations(story)[0]["proposal"])
        self.assertEqual(proposal["story_id"], story)
        self.assertEqual(proposal["seat"], 0)

    def test_approving_a_seat_hires_onto_the_story(self):
        """End to end: propose, approve, and the contract knows its story."""
        self.persona("engineering/backend")
        _, story = self.run_with({"team": [
            {"roster_slug": "engineering/backend", "role": "lead"}]})
        esc = self.escalations(story)[0]
        control.decide(self.conn, esc["id"], "approve")
        agent_row = self.conn.execute(
            "SELECT story_id, seat, project FROM agents WHERE role = 'lead'").fetchone()
        self.assertEqual(agent_row["story_id"], story)
        self.assertEqual(agent_row["seat"], 0)
        self.assertEqual(agent_row["project"], "proj-a")


if __name__ == "__main__":
    unittest.main()
