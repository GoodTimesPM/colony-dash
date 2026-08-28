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
    def _request(path: str, *, accept: str = "", cookie: str = "", query: str = ""):
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
            "server": ("127.0.0.1", 8787), "client": ("127.0.0.1", 51234),
            "app": server.app,
        })

    def _gate(self, request, *, token="s3cret"):
        """Run the middleware with the gate armed. `call_next` returns a marker,
        so 'the request was let through' is distinguishable from 'the request
        was answered with something that happens to be 200'."""
        import asyncio

        from fastapi.responses import PlainTextResponse

        async def call_next(_request):
            return PlainTextResponse("PASSED")

        with mock.patch.object(server, "REQUIRE_TOKEN", True), with_token(token):
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


if __name__ == "__main__":
    unittest.main()
