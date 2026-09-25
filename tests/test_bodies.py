"""Action bodies with a missing or malformed field are a 400, not a 500."""

from __future__ import annotations

import unittest

from fastapi import HTTPException

from colony.web import common, controls, stories


class ActionBodies(unittest.TestCase):

    def _status(self, fn, body):
        with self.assertRaises(HTTPException) as caught:
            fn(body, x_colony="1")
        return caught.exception.status_code

    def test_a_missing_id_is_a_400(self):
        self.assertEqual(self._status(controls.act_retire, {}), 400)

    def test_a_non_numeric_id_is_a_400(self):
        self.assertEqual(self._status(controls.act_retire, {"agent_id": "seven"}), 400)

    def test_a_boolean_is_not_an_id(self):
        self.assertEqual(self._status(controls.act_retire, {"agent_id": True}), 400)

    def test_a_missing_decision_is_a_400(self):
        self.assertEqual(self._status(stories.act_decide, {"escalation_id": 1}), 400)

    def test_zero_snooze_hours_is_kept(self):
        self.assertEqual(common._num({"snooze_hours": 0}, "snooze_hours", float, 8), 0.0)
        self.assertEqual(common._num({}, "snooze_hours", float, 8), 8)
        self.assertEqual(common._num({"h": "1.5"}, "h", float), 1.5)


if __name__ == "__main__":
    unittest.main()
