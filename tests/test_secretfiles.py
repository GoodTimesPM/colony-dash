"""One answer to "is this a credential file", used by the worktree seeder, the
file viewer and the tree listing."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from colony import projects, worktree
from colony.secretfiles import is_secret


class TestIsSecret(unittest.TestCase):

    def test_credential_files_match(self):
        for name in (".env", ".env.apply", ".env.radar", ".ENV.Local",
                     "job-search/jobdesk/.env.apply", "id_ed25519", "server.pem",
                     "tls.key", ".pypirc", "credentials-prod.json", "token.json",
                     "client_secret_123.json", r"a\b\.env.production"):
            with self.subTest(name=name):
                self.assertTrue(is_secret(name))

    def test_ordinary_files_and_templates_do_not(self):
        for name in (".env.example", ".env.sample", "README.md", "env.py",
                     "keys.py", "tokenizer.json", "id_rsa.pub", ""):
            with self.subTest(name=name):
                self.assertFalse(is_secret(name))


class TestWorktreeSeeding(unittest.TestCase):

    def test_ignored_env_variants_never_reach_the_worktree(self):
        listed = "\0".join([
            "job-search/jobdesk/.env.apply",
            "job-search/jobdesk/.env.radar",
            "job-search/jobdesk/data/state.json",
        ])
        with mock.patch.object(worktree, "_git", return_value=listed):
            keep, _ = worktree._ignored_in(["job-search/jobdesk"])
        self.assertEqual(keep, ["job-search/jobdesk/data/state.json"])

    def test_drop_secrets_removes_every_variant(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp)
            for name in (".env.apply", "nested/.env.radar", "keep.txt"):
                (tree / name).parent.mkdir(parents=True, exist_ok=True)
                (tree / name).write_text("x", encoding="utf-8")
            with mock.patch.object(worktree, "path_for", return_value=tree):
                worktree._drop_secrets(1)
            left = sorted(p.relative_to(tree).as_posix()
                          for p in tree.rglob("*") if p.is_file())
        self.assertEqual(left, ["keep.txt"])


class TestSafePath(unittest.TestCase):

    def test_a_symlink_to_a_secret_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "proj").mkdir()
            (root / "proj" / ".env").write_text("TOKEN=x", encoding="utf-8")
            link = root / "proj" / "notes.txt"
            try:
                link.symlink_to(root / "proj" / ".env")
            except OSError:
                self.skipTest("symlinks need developer mode on Windows")
            with mock.patch.object(projects, "ROOT", root):
                with self.assertRaises(ValueError):
                    projects.safe_path("proj/notes.txt")

    def test_env_variants_are_refused_by_name(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(projects, "ROOT", Path(tmp)):
            with self.assertRaises(ValueError):
                projects.safe_path("proj/.env.apply")


if __name__ == "__main__":
    unittest.main()
