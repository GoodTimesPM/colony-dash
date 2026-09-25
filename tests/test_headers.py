"""Response headers that keep uploaded files and the page from running foreign script."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from colony import attachments, server


class TestAttachmentHeaders(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        patcher = mock.patch.object(attachments, "ATTACHMENTS_DIR", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def serve(self, name: str, body: bytes = b"x"):
        (self.root / name).write_bytes(body)
        return server.attachment(name)

    def test_png_renders_inline_but_sandboxed(self):
        resp = self.serve("abc-shot.png")
        self.assertEqual(resp.media_type, "image/png")
        self.assertTrue(resp.headers["content-disposition"].startswith("inline"))
        self.assertIn("sandbox", resp.headers["content-security-policy"])
        self.assertEqual(resp.headers["x-content-type-options"], "nosniff")

    def test_html_downloads_instead_of_rendering(self):
        resp = self.serve("abc-page.html", b"<script>alert(1)</script>")
        self.assertEqual(resp.media_type, "application/octet-stream")
        self.assertTrue(resp.headers["content-disposition"].startswith("attachment"))

    def test_svg_downloads_instead_of_rendering(self):
        resp = self.serve("abc-logo.svg", b"<svg onload='alert(1)'/>")
        self.assertTrue(resp.headers["content-disposition"].startswith("attachment"))


def _get(path: str, etag: str | None = None):
    from starlette.requests import Request
    headers = [(b"if-none-match", etag.encode())] if etag else []
    return Request({"type": "http", "method": "GET", "path": path,
                    "headers": headers, "query_string": b""})


class TestPageCsp(unittest.TestCase):
    def test_index_sends_a_policy_without_inline_script(self):
        resp = server.index(_get("/"))
        csp = resp.headers["content-security-policy"]
        self.assertIn("script-src 'self'", csp)
        self.assertNotIn("unsafe-inline';", csp.split("script-src")[1].split(";")[0] + ";")
        self.assertIn("frame-ancestors 'none'", csp)


class TestAssetCaching(unittest.TestCase):
    def test_an_unchanged_file_is_a_304(self):
        first = server.app_js(_get("/app.js"))
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.headers["cache-control"], "no-cache")
        again = server.app_js(_get("/app.js", first.headers["etag"]))
        self.assertEqual(again.status_code, 304)
        self.assertEqual(again.body, b"")

    def test_the_theme_boot_script_is_served_and_loaded_in_head(self):
        resp = server.boot_js(_get("/boot.js"))
        self.assertIn(b"colony-theme", resp.body)
        page = server.index(_get("/")).body.decode()
        self.assertLess(page.index('src="boot.js"'), page.index('href="app.css"') + 40)
        self.assertLess(page.index('src="boot.js"'), page.index('src="app.js"'))

    def test_a_stale_etag_gets_the_file(self):
        resp = server.app_css(_get("/app.css", '"old"'))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.body)


if __name__ == "__main__":
    unittest.main()
