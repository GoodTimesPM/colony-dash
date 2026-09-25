"""Write scope is enforced on the patch, not only stated in the work order."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from colony import worktree
from colony.web import reads

PATCH = """diff --git a/app/one.py b/app/one.py
index 1111111..2222222 100644
--- a/app/one.py
+++ b/app/one.py
@@ -1 +1 @@
-a
+b
diff --git a/app/new file.txt b/app/new file.txt
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/app/new file.txt
@@ -0,0 +1 @@
+hi
diff --git a/other/gone.py b/other/gone.py
deleted file mode 100644
index 4444444..0000000
--- a/other/gone.py
+++ /dev/null
@@ -1 +0,0 @@
-x
diff --git a/app/pic.png b/app/pic.png
index 5555555..6666666 100644
Binary files a/app/pic.png and b/app/pic.png differ
diff --git a/app/old.py b/app/moved.py
similarity index 100%
rename from app/old.py
rename to app/moved.py
"""


class TestPatchFiles(unittest.TestCase):
    def test_every_kind_of_entry(self):
        self.assertEqual(worktree.patch_files(PATCH),
                         ["app/one.py", "app/new file.txt", "other/gone.py",
                          "app/pic.png", "app/moved.py"])

    def test_outside_scope(self):
        self.assertEqual(worktree.outside_scope(worktree.patch_files(PATCH), ["app"]),
                         ["other/gone.py"])

    def test_prefix_is_not_a_folder_match(self):
        self.assertEqual(worktree.outside_scope(["apple/x.py"], ["app"]), ["apple/x.py"])

    def test_quoted_path(self):
        text = ('diff --git "a/app/caf\303\251.txt" "b/app/caf\303\251.txt"\n'
                '--- "a/app/caf\303\251.txt"\n+++ "b/app/caf\303\251.txt"\n')
        self.assertEqual(worktree.patch_files(text), ["app/café.txt"])


class TestApplyRefusesOutOfScope(unittest.TestCase):
    def test_refused_before_git_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "ticket-7.patch").write_text(PATCH, encoding="utf-8")
            with mock.patch.object(worktree, "PATCH_DIR", Path(tmp)), \
                 mock.patch.object(worktree, "_git") as git:
                with self.assertRaises(worktree.OutOfScope) as ctx:
                    worktree.apply_patch(7, scope=["app"])
                git.assert_not_called()
        self.assertEqual(ctx.exception.paths, ["other/gone.py"])


class TestDiffstat(unittest.TestCase):
    def test_marks_outside_files(self):
        stat = reads._diffstat(PATCH, ["app"])
        self.assertEqual(stat["total"]["files"], 5)
        self.assertEqual(stat["total"]["outside"], 1)
        self.assertEqual([f["path"] for f in stat["files"] if f["outside"]], ["other/gone.py"])


if __name__ == "__main__":
    unittest.main()
