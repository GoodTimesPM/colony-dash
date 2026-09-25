"""A timeout ends the whole process tree, not only the direct child."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from colony import proc


def _alive(pid: int) -> bool:
    if proc.IS_WINDOWS:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True, **proc.hidden()).stdout
        return str(pid) in out
    import os
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class TestRun(unittest.TestCase):
    def test_captures_like_subprocess_run(self):
        out = proc.run([sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"],
                       input="hi", capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0)
        self.assertEqual(out.stdout.strip(), "HI")

    def test_timeout_kills_grandchildren(self):
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = Path(tmp) / "pid"
            parent = (
                "import subprocess, sys, time; "
                "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
                f"open({str(pidfile)!r}, 'w').write(str(c.pid)); time.sleep(60)"
            )
            with self.assertRaises(subprocess.TimeoutExpired):
                proc.run([sys.executable, "-c", parent], capture_output=True, timeout=3)
            grandchild = int(pidfile.read_text())
            for _ in range(20):
                if not _alive(grandchild):
                    break
                time.sleep(0.25)
            self.assertFalse(_alive(grandchild))


if __name__ == "__main__":
    unittest.main()
