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


class TestPageCsp(unittest.TestCase):
    def test_index_sends_a_policy_without_inline_script(self):
        resp = server.index()
        csp = resp.headers["content-security-policy"]
        self.assertIn("script-src 'self'", csp)
        self.assertNotIn("unsafe-inline';", csp.split("script-src")[1].split(";")[0] + ";")
        self.assertIn("frame-ancestors 'none'", csp)


if __name__ == "__main__":
    unittest.main()
