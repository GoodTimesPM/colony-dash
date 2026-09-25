"""The run-command rules refuse what they name and nothing that merely looks like it."""

from __future__ import annotations

import unittest

from colony import runner


class TestForbidden(unittest.TestCase):
    def test_ordinary_flags_pass(self):
        for cmd in ("tail -f server.log", "grep -f patterns.txt notes.md",
                    "docker compose -f dev.yml up", "npm install --force-color",
                    "py -m pytest -x", "rm -r build"):
            with self.subTest(cmd=cmd):
                self.assertEqual(runner.check(cmd), cmd)

    def test_destructive_combos_refused(self):
        for cmd in ("rm -rf build", "rm -fr build", "rm -r -f build", "rm -Rf x",
                    "rm --recursive --force x", "git push -f", "git checkout -f main",
                    "git rebase --force", "git branch -D old",
                    "Remove-Item x -Force -Recurse", "rmdir /s /q build"):
            with self.subTest(cmd=cmd):
                with self.assertRaises(runner.RunRefused):
                    runner.check(cmd)


class TestJudge(unittest.TestCase):
    def verdict(self, out, code=0):
        return runner.judge({"code": code, "out": out, "err": "", "timed_out": False})[0]

    def test_exit_code_decides_first(self):
        self.assertEqual(self.verdict("all good", code=1), "failed")

    def test_passing_summaries_stay_clean(self):
        for out in ("12 passed, 0 errors in 0.4s", "OK, no errors found",
                    "error handling module loaded", "checked 4 files, no failures",
                    "Ran 30 tests\n\nOK"):
            with self.subTest(out=out):
                self.assertEqual(self.verdict(out), "clean")

    def test_real_error_reports_are_suspect(self):
        for out in ("ValueError: bad input", "error: could not resolve host",
                    "Traceback (most recent call last):\n  x", "NOTION_TOKEN not set",
                    "0 rows written", "config file is missing"):
            with self.subTest(out=out):
                self.assertEqual(self.verdict(out), "suspect")


if __name__ == "__main__":
    unittest.main()
