"""The rule that has to exist for any of the phone story to work.

Reading the firewall means shelling out to PowerShell, so these tests replace
that one call and check what is done with its answer. The behaviour worth
pinning is the third state: `unknown` is not a synonym for `blocked`, because
sending someone to fight their firewall with an admin prompt open when the
firewall is fine is worse than saying nothing.
"""

from __future__ import annotations

import unittest
from unittest import mock

from colony import firewall


def answering(text):
    return mock.patch.object(firewall, "_quiet_ps", lambda script: text)


class State(unittest.TestCase):

    def test_open_and_blocked_are_read_from_the_answer(self):
        with answering("open\n"):
            self.assertEqual(firewall.state(8787), "open")
        with answering("blocked\n"):
            self.assertEqual(firewall.state(8787), "blocked")

    def test_no_answer_is_unknown(self):
        """An unelevated caller cannot read port filters on some machines, and
        PowerShell may not run at all. Neither is evidence of a closed port."""
        with answering(""):
            self.assertEqual(firewall.state(8787), "unknown")

    def test_noise_before_the_answer_is_ignored(self):
        """PowerShell writes warnings to stdout more often than it should."""
        with answering("WARNING: something\nopen\n"):
            self.assertEqual(firewall.state(8787), "open")

    def test_anything_unrecognised_is_unknown_not_blocked(self):
        with answering("Access is denied.\n"):
            self.assertEqual(firewall.state(8787), "unknown")

    def test_quiet_ps_swallows_a_refusal(self):
        with mock.patch.object(firewall, "_run_ps",
                               mock.Mock(side_effect=RuntimeError("denied"))):
            self.assertEqual(firewall._quiet_ps("whatever"), "")


class RuleCommand(unittest.TestCase):

    def test_it_names_the_port_and_nothing_wider(self):
        cmd = firewall.rule_command(8899)
        self.assertIn("8899", cmd)
        self.assertIn("-Direction Inbound", cmd)
        self.assertIn("-Protocol TCP", cmd)
        # Not a rule for the program, which would open every port any Python
        # script on this machine ever binds. And never the public profile.
        self.assertNotIn("-Program", cmd)
        self.assertNotIn("Public", cmd)


if __name__ == "__main__":
    unittest.main()
