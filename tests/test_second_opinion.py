"""Asking a second persona to audit a hire, without letting it decide one.

The whole value of this button is that it changes nothing. `agents-orchestrator`
reads the story, the picked persona and the roster, writes a paragraph, and the
Product Owner still answers the card. Most of these tests exist to hold that
line: after a second opinion runs, the escalation must still be open, no agent
may exist, and no story status may have moved.

The rest are the refusals. Every one of them costs nothing and saves a full
roster digest, which is the most expensive read this program makes.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from colony import control, db, wake

TERMS = {"role": "investigator", "model": "claude-sonnet-5",
         "tools_allowed": ["Read"], "max_tokens_run": 400000}


class Answer:
    """Stands in for a finished agent run."""

    status = "ok"
    chargeable_tokens = 900
    error = None
    text = ""

    def __init__(self, payload):
        self._payload = payload

    def json_payload(self):
        return self._payload


class Audit(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "proj-a").mkdir()
        self.conn = db.open_ledger(self.root / "ledger.db")
        self.addCleanup(self.conn.close)

        # A persona root of our own, with the orchestrator's file in it. The
        # real one lives in a cloned repo that may not be on this machine.
        self.personas = self.root / "personas"
        (self.personas / "specialized").mkdir(parents=True)
        (self.personas / "specialized" / "agents-orchestrator.md").write_text(
            "---\nname: Agents Orchestrator\n---\nPicks agents.\n", encoding="utf-8")

        for p in (mock.patch.object(db, "PROJECTS_ROOT", self.root),
                  mock.patch.object(control, "ROOT_POSIX", self.root.as_posix()),
                  mock.patch.object(wake, "PERSONA_ROOT", self.personas)):
            p.start()
            self.addCleanup(p.stop)

    # -- fixtures ------------------------------------------------------------

    def persona(self, slug: str, division: str = "engineering") -> str:
        self.conn.execute(
            "INSERT INTO roster (slug, name, division, description, source, path, "
            "                    body_hash) "
            "VALUES (?,?,?,'','agency','x','h')",
            (slug, slug.split("/")[-1].replace("-", " ").title(), division))
        return slug

    def story(self) -> int:
        cur = self.conn.execute(
            "INSERT INTO stories (title, status, project, project_source, "
            "                     acceptance_criteria, description) "
            "VALUES ('a story', 'ready', 'proj-a', 'confirmed', 'do it', 'the brief')")
        return int(cur.lastrowid)

    def pending_hire(self, slug: str = "engineering/backend") -> tuple[int, int]:
        """A story with one undecided hire card on it."""
        self.persona(slug)
        story_id = self.story()
        esc_id = control.propose_hire(
            self.conn, roster_slug=slug, role="lead", project="proj-a",
            reason="Hire Backend as lead.", write_capable=True,
            story_id=story_id, seat=0)
        return story_id, esc_id

    def audit(self, esc_id: int, payload):
        with mock.patch.object(wake.agent, "run_ticket",
                               lambda *a, **k: Answer(payload)):
            return wake.second_opinion(self.conn, esc_id, TERMS)

    def opinion_on(self, esc_id: int):
        return self.conn.execute(
            "SELECT second_opinion, second_opinion_at, resolved_at FROM escalations "
            "WHERE id = ?", (esc_id,)).fetchone()

    # -- the happy path ------------------------------------------------------

    def test_the_opinion_lands_on_the_card_it_was_asked_about(self):
        _, esc_id = self.pending_hire()
        out = self.audit(esc_id, {"verdict": "agree",
                                  "opinion": "Right call, a Python edit.",
                                  "instead": None, "watch": "scope creep"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["verdict"], "agree")
        row = self.opinion_on(esc_id)
        self.assertIn("Right call", row["second_opinion"])
        self.assertIn("[agree]", row["second_opinion"])
        self.assertIn("Watch: scope creep", row["second_opinion"])
        self.assertIsNotNone(row["second_opinion_at"])

    def test_it_decides_nothing(self):
        """The point of the whole feature. A second opinion that could resolve
        the card would be a second Product Owner, which is one too many."""
        story_id, esc_id = self.pending_hire()
        before = self.conn.execute("SELECT status FROM stories WHERE id = ?",
                                   (story_id,)).fetchone()["status"]
        self.audit(esc_id, {"verdict": "disagree", "opinion": "Wrong person."})

        row = self.opinion_on(esc_id)
        self.assertIsNone(row["resolved_at"], "the card must still be open")
        self.assertIsNone(self.conn.execute(
            "SELECT po_decision FROM escalations WHERE id = ?",
            (esc_id,)).fetchone()["po_decision"])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) AS n FROM agents").fetchone()["n"], 0,
            "nothing may be hired by an audit")
        self.assertEqual(self.conn.execute(
            "SELECT status FROM stories WHERE id = ?",
            (story_id,)).fetchone()["status"], before)

    def test_it_raises_no_card_of_its_own(self):
        """A read-only opinion that could add to the Inbox would be a way for
        the colony to ask the PO questions the PO did not ask for."""
        _, esc_id = self.pending_hire()
        self.audit(esc_id, {"verdict": "agree", "opinion": "fine"})
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) AS n FROM escalations").fetchone()["n"], 1)

    def test_a_real_alternative_is_named_in_full(self):
        _, esc_id = self.pending_hire()
        self.persona("testing/qa-lead", "testing")
        self.audit(esc_id, {"verdict": "disagree", "opinion": "A test problem.",
                            "instead": "testing/qa-lead"})
        text = self.opinion_on(esc_id)["second_opinion"]
        self.assertIn("Would take instead: Qa Lead (testing/qa-lead, testing)", text)

    def test_an_invented_alternative_is_labelled_as_noise(self):
        """An auditor that names a slug nobody has reads to the PO exactly like
        one they could act on. Saying so is cheaper than letting them look."""
        _, esc_id = self.pending_hire()
        self.audit(esc_id, {"verdict": "disagree", "opinion": "Not this one.",
                            "instead": "made-up/nobody"})
        text = self.opinion_on(esc_id)["second_opinion"]
        self.assertIn("made-up/nobody", text)
        self.assertIn("not a slug in the roster", text)
        self.assertNotIn("Would take instead", text)

    def test_a_verdict_it_invented_reads_as_unclear(self):
        _, esc_id = self.pending_hire()
        out = self.audit(esc_id, {"verdict": "maybe-ish", "opinion": "hard to say"})
        self.assertEqual(out["verdict"], "unclear")

    def test_an_unreadable_answer_leaves_the_card_askable_again(self):
        _, esc_id = self.pending_hire()
        out = self.audit(esc_id, {"verdict": "agree", "opinion": "   "})
        self.assertFalse(out["ok"])
        self.assertIsNone(self.opinion_on(esc_id)["second_opinion"])
        self.assertEqual(self.conn.execute(
            "SELECT status FROM tickets WHERE intent = 'research'").fetchone()["status"],
            "blocked")

    def test_the_prompt_carries_the_roster_and_the_pick(self):
        """Without the roster this is one persona's taste, not an audit."""
        _, esc_id = self.pending_hire()
        self.persona("design/ux", "design")
        seen = {}

        def spy(conn, **kw):
            seen["prompt"] = kw["prompt"]
            return Answer({"verdict": "agree", "opinion": "yes"})

        with mock.patch.object(wake.agent, "run_ticket", spy):
            wake.second_opinion(self.conn, esc_id, TERMS)

        prompt = seen["prompt"]
        self.assertIn("engineering/backend", prompt)
        self.assertIn("design/ux", prompt, "the alternatives have to be in there")
        self.assertIn("READ-ONLY", prompt)
        self.assertIn("agents-orchestrator", prompt)

    # -- the refusals --------------------------------------------------------

    def refused(self, esc_id, payload=None):
        with self.assertRaises(control.Refused) as caught:
            self.audit(esc_id, payload or {"verdict": "agree", "opinion": "x"})
        return str(caught.exception)

    def test_no_such_card(self):
        self.assertIn("no such escalation", self.refused(999))

    def test_a_card_that_is_not_a_hire(self):
        story_id = self.story()
        cur = self.conn.execute(
            "INSERT INTO escalations (story_id, kind, reason) "
            "VALUES (?, 'decision', 'Accept these criteria?')", (story_id,))
        self.assertIn("This card is a decision", self.refused(int(cur.lastrowid)))

    def test_a_hire_that_is_already_decided(self):
        _, esc_id = self.pending_hire()
        control.decide(self.conn, esc_id, "approve")
        self.assertIn("already decided", self.refused(esc_id))

    def test_a_hire_that_already_has_an_opinion(self):
        """Asking twice buys the same paragraph for another full roster read."""
        _, esc_id = self.pending_hire()
        self.audit(esc_id, {"verdict": "agree", "opinion": "the first answer"})
        self.assertIn("already has a second opinion", self.refused(esc_id))
        self.assertIn("the first answer", self.opinion_on(esc_id)["second_opinion"])

    def test_a_hire_with_no_story_behind_it(self):
        self.persona("engineering/backend")
        esc_id = control.propose_hire(
            self.conn, roster_slug="engineering/backend", role="hand", project="proj-a",
            reason="A hire somebody typed in by hand.", write_capable=True)
        self.assertIn("no work to judge the pick against", self.refused(esc_id))

    def test_a_pick_that_has_since_left_the_roster(self):
        _, esc_id = self.pending_hire()
        self.conn.execute("DELETE FROM roster WHERE slug = 'engineering/backend'")
        self.assertIn("no longer in the roster", self.refused(esc_id))

    def test_the_orchestrator_file_is_not_on_this_machine(self):
        """The personas are somebody else's repo and are not vendored here. No
        file means a generic opinion, and a generic second opinion is worse
        than none: it looks like an audit and is a coin flip."""
        _, esc_id = self.pending_hire()
        (self.personas / "specialized" / "agents-orchestrator.md").unlink()
        self.assertIn("is not on disk", self.refused(esc_id))

    def test_a_refusal_costs_nothing(self):
        _, esc_id = self.pending_hire()
        self.conn.execute("DELETE FROM roster WHERE slug = 'engineering/backend'")

        def explode(*a, **k):
            raise AssertionError("a refused audit must not reach the model")

        with mock.patch.object(wake.agent, "run_ticket", explode):
            with self.assertRaises(control.Refused):
                wake.second_opinion(self.conn, esc_id, TERMS)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) AS n FROM tickets").fetchone()["n"], 0)

    # -- what it leaves behind -----------------------------------------------

    def test_the_run_is_recorded_as_a_finished_research_ticket(self):
        story_id, esc_id = self.pending_hire()
        self.audit(esc_id, {"verdict": "agree-with-caveat",
                            "opinion": "fine, but slow"})
        ticket = self.conn.execute(
            "SELECT * FROM tickets WHERE intent = 'research'").fetchone()
        self.assertEqual(ticket["status"], "done")
        self.assertEqual(ticket["story_id"], story_id)
        self.assertIn("agree-with-caveat", ticket["findings"])

    def test_the_story_timeline_shows_it_happened(self):
        story_id, esc_id = self.pending_hire()
        self.audit(esc_id, {"verdict": "agree", "opinion": "yes"})
        ev = self.conn.execute(
            "SELECT * FROM story_events WHERE story_id = ? ORDER BY id DESC LIMIT 1",
            (story_id,)).fetchone()
        self.assertIn("Second opinion", ev["summary"])
        self.assertEqual(ev["tokens"], 900)

    def test_approval_after_an_opinion_still_hires_onto_the_seat(self):
        """The opinion is advice written beside the card. It must not have
        touched the proposal the approval is going to execute."""
        story_id, esc_id = self.pending_hire()
        self.audit(esc_id, {"verdict": "disagree", "opinion": "I would not."})
        control.decide(self.conn, esc_id, "approve")
        agent_row = self.conn.execute(
            "SELECT story_id, seat, project FROM agents WHERE role = 'lead'").fetchone()
        self.assertEqual(agent_row["story_id"], story_id)
        self.assertEqual(agent_row["seat"], 0)
        self.assertEqual(agent_row["project"], "proj-a")

    def test_the_proposal_json_is_untouched(self):
        _, esc_id = self.pending_hire()
        before = self.conn.execute("SELECT proposal FROM escalations WHERE id = ?",
                                   (esc_id,)).fetchone()["proposal"]
        self.audit(esc_id, {"verdict": "disagree", "opinion": "no",
                            "instead": "engineering/backend"})
        after = self.conn.execute("SELECT proposal FROM escalations WHERE id = ?",
                                  (esc_id,)).fetchone()["proposal"]
        self.assertEqual(json.loads(before), json.loads(after))


if __name__ == "__main__":
    unittest.main()
