"""One snapshot per interval, and the roster kept out of it."""

from __future__ import annotations

import json
import unittest
from unittest import mock

from starlette.requests import Request

from colony import server


def _request(etag: str | None = None) -> Request:
    headers = [(b"if-none-match", etag.encode())] if etag else []
    return Request({"type": "http", "method": "GET", "path": "/api/roster/summary",
                    "headers": headers, "query_string": b""})


class Frame(unittest.TestCase):

    def setUp(self):
        server._expire_frame()
        self.addCleanup(server._expire_frame)
        self.calls = 0

        def fake_snapshot():
            self.calls += 1
            return {"n": self.calls, "roster": {"total": 1, "mine": 0, "rev": "r1"}}

        patcher = mock.patch.object(server, "snapshot", fake_snapshot)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_clients_share_one_snapshot_per_interval(self):
        first = server.frame()
        second = server.frame()
        self.assertEqual(self.calls, 1)
        self.assertEqual(first, second)
        self.assertEqual(json.loads(first[1])["n"], 1)

    def test_an_action_expires_the_frame(self):
        server.frame()
        server._expire_frame()
        server.frame()
        self.assertEqual(self.calls, 2)


class RosterSummary(unittest.TestCase):

    def setUp(self):
        with server._roster_lock:
            saved = dict(server._roster_cache)
            server._roster_cache.update(rev="abc", text='{"total": 3}')
        self.addCleanup(server._roster_cache.update, saved)

    def test_a_fresh_client_gets_the_body_and_an_etag(self):
        response = server.api_roster_summary(_request())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["etag"], '"abc"')
        self.assertEqual(json.loads(response.body), {"total": 3})

    def test_a_matching_etag_is_a_304(self):
        response = server.api_roster_summary(_request('"abc"'))
        self.assertEqual(response.status_code, 304)
        self.assertEqual(response.body, b"")


if __name__ == "__main__":
    unittest.main()
