"""The gate that opens when the dashboard stops being loopback-only.

The property worth asserting is not "the token works" but "there is no way to
end up serving without one". `serve()` calls `access.check`, and `check` raises
rather than returning a permissive answer, so these tests are mostly about the
refusal.
"""

from __future__ import annotations

import unittest
from unittest import mock

from colony import access, server


def with_token(value):
    """Patch the configured token without touching `.env` or the environment."""
    return mock.patch.object(access, "token", lambda: value)


class TestLoopback(unittest.TestCase):

    def test_the_loopback_addresses_need_no_token(self):
        for host in ("127.0.0.1", "localhost", "::1", "127.5.5.5", ""):
            with self.subTest(host=host):
                self.assertTrue(access.is_loopback(host))
                with with_token(None):
                    self.assertFalse(access.check(host))

    def test_everything_else_is_not_loopback(self):
        for host in ("0.0.0.0", "100.94.3.11", "192.168.1.40", "::",
                     "example.com", "not-an-address"):
            with self.subTest(host=host):
                self.assertFalse(access.is_loopback(host))

    def test_a_hostname_that_will_not_parse_fails_towards_the_token(self):
        """The unknown case has to be the guarded one."""
        self.assertFalse(access.is_loopback("desktop.tail1234.ts.net"))


class TestCheck(unittest.TestCase):

    def test_a_network_bind_without_a_token_is_refused(self):
        with with_token(None), self.assertRaises(access.Unconfigured) as caught:
            access.check("0.0.0.0")
        message = str(caught.exception)
        self.assertIn("COLONY_ACCESS_TOKEN", message)
        self.assertIn("0.0.0.0", message)

    def test_a_blank_token_counts_as_no_token(self):
        """Patched one layer lower than the other tests, at the `.env` read, so
        this exercises the real `token()` and its strip. `COLONY_ACCESS_TOKEN=`
        left in a copied `.env.example` is the shape this is guarding against."""
        for blank in ("", "   ", "\n", None):
            with self.subTest(blank=blank), \
                 mock.patch.object(access.db, "_env_value", lambda key: blank):
                self.assertIsNone(access.token())
                with self.assertRaises(access.Unconfigured):
                    access.check("100.94.3.11")

    def test_a_network_bind_with_a_token_turns_the_gate_on(self):
        with with_token("s3cret"):
            self.assertTrue(access.check("100.94.3.11"))

    def test_serve_never_reaches_uvicorn_without_a_token(self):
        """The one that matters. `serve` must raise before it binds anything."""
        ran = []
        fake_uvicorn = mock.MagicMock()
        fake_uvicorn.run.side_effect = lambda *a, **k: ran.append(k)

        with with_token(None), mock.patch.dict("sys.modules", {"uvicorn": fake_uvicorn}):
            with self.assertRaises(access.Unconfigured):
                server.serve(host="0.0.0.0", port=8899)
        self.assertEqual(ran, [], "uvicorn should never have been started")


class TestMatches(unittest.TestCase):

    def test_the_right_token_matches_and_nothing_else_does(self):
        with with_token("the-real-one"):
            self.assertTrue(access.matches("the-real-one"))
            for wrong in ("the-real-onx", "the-real-on", "", None, "THE-REAL-ONE"):
                with self.subTest(wrong=wrong):
                    self.assertFalse(access.matches(wrong))

    def test_nothing_matches_when_no_token_is_configured(self):
        """Belt and braces: `matches` is only reached when the gate is on, but
        an unconfigured token must never be the one that lets someone in."""
        with with_token(None):
            self.assertFalse(access.matches(""))
            self.assertFalse(access.matches("anything"))


class TestMint(unittest.TestCase):

    def test_a_minted_token_is_long_and_never_the_same_twice(self):
        one, two = access.mint(), access.mint()
        self.assertNotEqual(one, two)
        self.assertGreaterEqual(len(one), 40)


class TestPublicPaths(unittest.TestCase):

    def test_nothing_that_reads_the_ledger_is_public(self):
        """The allowlist exists so a phone can render the login screen. If an
        `/api/` path ever lands in it, the gate is decorative."""
        for path in server.PUBLIC_PATHS:
            with self.subTest(path=path):
                self.assertFalse(path.startswith("/api/"))
                self.assertNotEqual(path, "/")
                self.assertNotEqual(path, "/events")
                self.assertNotEqual(path, "/app.js")

    def test_the_gate_is_off_by_default(self):
        """Importing the module must not arm anything. Only `serve()` does."""
        self.assertFalse(server.REQUIRE_TOKEN)


class TestGate(unittest.TestCase):
    """The middleware itself, driven by hand.

    `starlette.testclient` wants httpx on this machine and does not get it, so
    these build the ASGI scope directly and await the middleware. That is a
    thinner test than a real request, but the branch being checked -- which of
    the two refusals a caller gets -- is decided entirely from the scope.
    """

    @staticmethod
    def _request(path: str, *, accept: str = "", cookie: str = "", query: str = "",
                 client: str = "127.0.0.1"):
        from starlette.requests import Request
        headers = []
        if accept:
            headers.append((b"accept", accept.encode()))
        if cookie:
            headers.append((b"cookie", f"{access.COOKIE}={cookie}".encode()))
        return Request({
            "type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
            "path": path, "raw_path": path.encode(), "root_path": "",
            "query_string": query.encode(), "headers": headers,
            "server": ("127.0.0.1", 8787), "client": (client, 51234),
            "app": server.app,
        })

    def _gate(self, request, *, token="s3cret", trust_loopback=False):
        """Run the middleware with the gate armed. `call_next` returns a marker,
        so 'the request was let through' is distinguishable from 'the request
        was answered with something that happens to be 200'."""
        import asyncio

        from fastapi.responses import PlainTextResponse

        async def call_next(_request):
            return PlainTextResponse("PASSED")

        with mock.patch.object(server, "REQUIRE_TOKEN", True),              mock.patch.object(server, "TRUST_LOOPBACK", trust_loopback),              with_token(token):
            return asyncio.run(server.gate(request, call_next))

    def test_a_navigation_gets_the_login_page(self):
        response = self._gate(self._request("/", accept="text/html,*/*;q=0.8"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn(b"token", response.body)
        self.assertNotIn(b"PASSED", response.body)

    def test_a_subresource_gets_a_401_not_a_page(self):
        """A browser asks for `text/html` on a navigation and on nothing else.
        Handing the login page to `fetch` reads as a JSON parse error, and
        handing it to a `<script src>` reads as a syntax error on line 1 of a
        file that is fine -- both of them three layers from the cause."""
        for path in ("/app.js", "/app.css", "/api/state", "/events"):
            with self.subTest(path=path):
                response = self._gate(self._request(path, accept="*/*"))
                self.assertEqual(response.status_code, 401)
                self.assertIn("application/json", response.headers["content-type"])

    def test_an_api_path_never_gets_the_page_even_asking_for_html(self):
        response = self._gate(self._request("/api/state", accept="text/html"))
        self.assertEqual(response.status_code, 401)

    def test_the_right_cookie_is_let_through(self):
        response = self._gate(self._request("/", accept="text/html", cookie="s3cret"))
        self.assertEqual(response.body, b"PASSED")

    def test_a_link_token_is_let_through_and_swapped_for_a_cookie(self):
        response = self._gate(self._request("/", accept="text/html", query="k=s3cret"))
        self.assertEqual(response.body, b"PASSED")
        cookie = response.headers.get("set-cookie", "")
        self.assertIn(f"{access.COOKIE}=s3cret", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Lax", cookie.replace("lax", "Lax"))
        # No `Secure`: a tailnet address is plain http, and a cookie the browser
        # refuses to store is a login screen that never goes away.
        self.assertNotIn("Secure", cookie)

    def test_a_wrong_link_token_is_not(self):
        response = self._gate(self._request("/", accept="text/html", query="k=nope"))
        self.assertNotIn(b"PASSED", response.body)

    def test_loopback_is_gated_unless_the_process_serves_loopback(self):
        """The default. A server bound only to the network has no loopback
        socket, so a loopback peer cannot arrive -- and if one somehow does, an
        address is not a credential."""
        response = self._gate(self._request("/api/state", accept="*/*"))
        self.assertEqual(response.status_code, 401)

    def test_a_dual_bound_process_lets_its_own_machine_through(self):
        """`serve_extra` sets `TRUST_LOOPBACK`, because the loopback half of
        that process was already open and unguarded before phone access was
        switched on. Demanding the token from it would log out the dashboard the
        switch was pressed in."""
        response = self._gate(self._request("/api/state", accept="*/*"),
                              trust_loopback=True)
        self.assertEqual(response.body, b"PASSED")

    def test_trusting_loopback_trusts_nothing_else(self):
        """The whole exemption in one assertion: it is the peer address, and the
        phone is never one of these."""
        for peer in ("10.0.0.14", "100.94.3.11", "192.168.1.9"):
            with self.subTest(peer=peer):
                response = self._gate(
                    self._request("/api/state", accept="*/*", client=peer),
                    trust_loopback=True)
                self.assertEqual(response.status_code, 401)

    def test_a_public_path_skips_the_gate_entirely(self):
        for path in sorted(server.PUBLIC_PATHS):
            with self.subTest(path=path):
                response = self._gate(self._request(path, accept="*/*"))
                self.assertEqual(response.body, b"PASSED")

    def test_nothing_is_gated_when_the_gate_is_off(self):
        import asyncio

        from fastapi.responses import PlainTextResponse

        async def call_next(_request):
            return PlainTextResponse("PASSED")

        with mock.patch.object(server, "REQUIRE_TOKEN", False):
            response = asyncio.run(server.gate(self._request("/api/state"), call_next))
        self.assertEqual(response.body, b"PASSED")


class TestArrivals(unittest.TestCase):
    """The record that tells a dropped packet apart from a rejected token.

    Both failures look identical from the desktop: the address is bound, the
    probe answers, the firewall rule is there, and the phone shows nothing. The
    only thing that separates them is whether a request arrived, so the gate
    records that on both paths and the panel reads it.
    """

    def setUp(self):
        access._ARRIVALS.clear()
        self.addCleanup(access._ARRIVALS.clear)

    def test_this_machine_is_never_recorded(self):
        """Loopback reaching itself was never the question being asked."""
        for host in ("127.0.0.1", "localhost", "::1", ""):
            access.note_arrival(host, True)
        self.assertEqual(access.arrivals(), [])

    def test_a_refused_device_is_recorded_as_having_arrived(self):
        access.note_arrival("10.0.0.31", False)
        (seen,) = access.arrivals()
        self.assertEqual(seen["host"], "10.0.0.31")
        self.assertFalse(seen["ok"])
        self.assertEqual((seen["hits"], seen["accepted"], seen["refused"]),
                         (1, 0, 1))

    def test_one_device_reloading_is_one_entry(self):
        """Forty reloads is a count on one row, not forty rows."""
        for _ in range(40):
            access.note_arrival("10.0.0.31", False)
        (seen,) = access.arrivals()
        self.assertEqual(seen["hits"], 40)

    def test_a_device_that_pairs_reads_as_connected_afterwards(self):
        """The last answer wins, so scanning a fresh code clears the warning."""
        access.note_arrival("10.0.0.31", False)
        access.note_arrival("10.0.0.31", True)
        (seen,) = access.arrivals()
        self.assertTrue(seen["ok"])
        self.assertEqual((seen["accepted"], seen["refused"]), (1, 1))

    def test_the_record_is_bounded(self):
        for n in range(access.ARRIVALS_MAX + 6):
            access.note_arrival(f"10.0.0.{n}", True)
        self.assertEqual(len(access.arrivals()), access.ARRIVALS_MAX)

    def test_the_gate_records_both_answers(self):
        """The point of the whole record: it is written by the live path."""
        import asyncio

        from fastapi.responses import PlainTextResponse

        async def call_next(_request):
            return PlainTextResponse("PASSED")

        with mock.patch.object(server, "REQUIRE_TOKEN", True), with_token("s3cret"):
            asyncio.run(server.gate(
                TestGate._request("/api/state", accept="*/*", query="k=nope",
                                     client="10.0.0.31"), call_next))
            asyncio.run(server.gate(
                TestGate._request("/api/state", accept="*/*", query="k=s3cret",
                                     client="10.0.0.31"), call_next))

        (seen,) = access.arrivals()
        self.assertEqual((seen["accepted"], seen["refused"]), (1, 1))


class TestSameHost(unittest.TestCase):
    """DNS rebinding: a page on evil.example resolves itself to 127.0.0.1 and
    calls the API. Its requests arrive with `Host: evil.example`."""

    def test_addresses_and_known_names_pass(self):
        for host in ("127.0.0.1:8787", "localhost:8787", "[::1]:8787",
                     "100.94.3.11:8787", "desk.tail1234.ts.net:8787",
                     "192.168.1.40"):
            with self.subTest(host=host):
                self.assertTrue(access.host_allowed(host))

    def test_foreign_names_are_refused(self):
        for host in ("evil.example", "evil.example:8787", "127.0.0.1.evil.example",
                     "", None):
            with self.subTest(host=host):
                self.assertFalse(access.host_allowed(host))

    def test_the_env_list_extends_the_names(self):
        with mock.patch.object(access.db, "_env_value",
                               lambda key: "colony.lan, other.lan"):
            self.assertTrue(access.host_allowed("colony.lan:8787"))
            self.assertFalse(access.host_allowed("evil.example"))

    def test_origin_must_match_host(self):
        self.assertTrue(access.origin_matches(None, "127.0.0.1:8787"))
        self.assertTrue(access.origin_matches("http://127.0.0.1:8787", "127.0.0.1:8787"))
        self.assertFalse(access.origin_matches("http://evil.example", "127.0.0.1:8787"))
        self.assertFalse(access.origin_matches("null", "127.0.0.1:8787"))

    def _run(self, host, *, method="GET", origin=None, path="/api/console/send"):
        import asyncio

        from fastapi.responses import PlainTextResponse
        from starlette.requests import Request

        headers = [(b"host", host.encode())]
        if origin:
            headers.append((b"origin", origin.encode()))
        request = Request({
            "type": "http", "http_version": "1.1", "method": method,
            "scheme": "http", "path": path, "raw_path": path.encode(),
            "root_path": "", "query_string": b"", "headers": headers,
            "server": ("127.0.0.1", 8787), "client": ("127.0.0.1", 51234),
            "app": server.app,
        })

        async def call_next(_request):
            return PlainTextResponse("PASSED")

        return asyncio.run(server.same_host(request, call_next))

    def test_a_rebound_host_gets_403_before_any_route(self):
        response = self._run("evil.example:8787", method="POST")
        self.assertEqual(response.status_code, 403)

    def test_a_cross_origin_write_gets_403(self):
        response = self._run("127.0.0.1:8787", method="POST",
                             origin="http://evil.example")
        self.assertEqual(response.status_code, 403)

    def test_the_dashboard_itself_passes(self):
        response = self._run("127.0.0.1:8787", method="POST",
                             origin="http://127.0.0.1:8787")
        self.assertEqual(response.body, b"PASSED")

    def test_the_middleware_runs_before_the_token_gate(self):
        """Starlette runs the last-registered middleware first."""
        names = [m.kwargs.get("dispatch").__name__ for m in server.app.user_middleware]
        self.assertEqual(names[0], "same_host")


if __name__ == "__main__":
    unittest.main()
