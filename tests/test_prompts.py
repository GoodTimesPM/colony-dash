"""Every work order renders from its template in `colony/prompts/`.

A field the builder forgets to pass is a KeyError at run time, after the pulse
has already spent the wake, so each builder is rendered once here with
stand-in rows.
"""

from __future__ import annotations

import re
import string
import unittest

from colony import attachments, build, console, prompt, voice, wake

STORY = {"id": 7, "sid": 7, "title": "Export the ledger", "story_title": "Export the ledger",
         "notion_status": "Backlog", "project": "colony-dash", "project_source": "po",
         "status": "backlog", "description": "Brief {with braces}.",
         "acceptance_criteria": "- one\n- two", "po_answers": ""}
ESC = {"kind": "hire", "raised_at": "2026-09-25", "reason": "needs a hire",
       "recommendation": "engineering/backend"}
PICK = {"name": "Backend", "slug": "engineering/backend", "division": "engineering",
        "times_hired": 2, "last_hired_at": None}
FILES = [{"path": "C:/a.png", "label": "shot", "kind": "image", "at": "today"}]


def rendered() -> dict[str, str]:
    return {
        "build": build.build_prompt(STORY, "C:/wt", FILES),
        "groom": wake.groom_prompt(STORY, ["colony-dash", "job-search"], FILES),
        "reply": wake.reply_prompt({"body": "go"}, ESC, STORY, ["colony-dash"],
                                   [{"author": "po", "at": "now", "body": "hi"}], FILES),
        "staff": wake.staff_prompt(STORY, "engineering/backend  Backend", FILES),
        "second_opinion": wake.second_opinion_prompt(STORY, ESC, PICK, "p.md", "digest"),
        "console": console.SYSTEM,
        "attachments": attachments.evidence(FILES),
    }


class Prompts(unittest.TestCase):
    def test_every_template_has_a_builder(self):
        names = {p.stem for p in prompt.DIR.glob("*.md")}
        self.assertEqual(names - set(rendered()) - {"style", "forge_draft"}, set())

    def test_no_field_is_left_unfilled(self):
        fields = {f for p in prompt.DIR.glob("*.md")
                  for _, f, _, _ in string.Formatter().parse(prompt.load(p.stem)) if f}
        for name, text in rendered().items():
            for field in fields:
                self.assertNotIn("{" + field + "}", text, f"{name} left {{{field}}}")

    def test_json_braces_survive(self):
        self.assertIn('"done": [', rendered()["build"])
        self.assertIn('{"criterion": "...", "why": "..."}', rendered()["build"])

    def test_the_story_text_is_not_reformatted(self):
        self.assertIn("Brief {with braces}.", rendered()["groom"])

    def test_no_shouting(self):
        for name, text in rendered().items():
            self.assertIsNone(re.search(r"READ-ONLY|READ EVERY|ISOLATED GIT|WRITE SCOPE", text),
                              name)

    def test_style_is_in_the_prose_prompts(self):
        self.assertTrue(voice.STYLE.startswith("--- how to write"))
        for name in ("build", "groom", "reply", "staff", "second_opinion"):
            self.assertIn(voice.STYLE, rendered()[name], name)


if __name__ == "__main__":
    unittest.main()
