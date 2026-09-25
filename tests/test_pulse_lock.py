"""The pulse lock is stale when the process that wrote it is gone."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from colony import control, proc


class PulseLock(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.object(control, "PULSE_LOCK", Path(self._tmp.name) / "pulse.lock")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write(self, pid):
        control.PULSE_LOCK.write_text(f"pid {pid} at 2026-09-25 10:00:00\n", encoding="utf-8")

    def test_a_live_owner_holds_the_lock(self):
        self._write(os.getpid())
        self.assertTrue(control.pulse_running())

    def test_a_dead_owner_frees_the_lock(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        self._write(child.pid)
        self.assertFalse(proc.alive(child.pid))
        self.assertFalse(control.pulse_running())
        with control.pulse_lock():
            self.assertIn(f"pid {os.getpid()}", control.PULSE_LOCK.read_text(encoding="utf-8"))

    def test_an_unreadable_lock_counts_as_held(self):
        control.PULSE_LOCK.write_text("", encoding="utf-8")
        self.assertTrue(control.pulse_running())

    def test_no_lock_is_not_running(self):
        self.assertFalse(control.pulse_running())


if __name__ == "__main__":
    unittest.main()
