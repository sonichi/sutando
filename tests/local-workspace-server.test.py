#!/usr/bin/env python3
"""Hardened local workspace server: every guard on the owner's checklist,
driven over real HTTP against the shipped handler (no stubs).

Run: python3 tests/local-workspace-server.test.py   (stdlib only)
"""
import http.client
import importlib.util
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "lws_serve", REPO / "skills" / "local-workspace-server" / "scripts" / "serve.py")
serve = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(serve)


class ServerHarness(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        (root / "index.html").write_text("<h1>deck</h1>")
        (root / "app.js").write_text("console.log('x')")
        (root / "sub").mkdir()
        (root / "sub" / "a.css").write_text("body{}")
        (root / "secret-outside.txt")  # never created — target of traversal
        cls.srv, cls.cap = serve.build_server(root, port=0, ttl=3600)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.tmp.cleanup()

    def _req(self, path, method="GET", host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Host": host if host is not None
                   else f"127.0.0.1:{self.port}"}
        c.request(method, path, headers=headers)
        r = c.getresponse()
        body = r.read()
        hdrs = {k.lower(): v for k, v in r.getheaders()}
        c.close()
        return r.status, body, hdrs

    def test_binds_loopback_only(self):
        self.assertEqual(self.srv.server_address[0], "127.0.0.1")

    def test_capability_url_serves_with_mime_nosniff_and_sandbox_csp(self):
        st, body, h = self._req(f"/{self.cap}/index.html")
        self.assertEqual((st, body), (200, b"<h1>deck</h1>"))
        self.assertEqual(h["content-type"], "text/html")
        self.assertEqual(h["x-content-type-options"], "nosniff")
        self.assertIn("sandbox allow-scripts", h["content-security-policy"])
        st, _, h = self._req(f"/{self.cap}/app.js")
        self.assertEqual(h["content-type"], "text/javascript")
        self.assertNotIn("content-security-policy", h)  # CSP is HTML-only
        st, _, h = self._req(f"/{self.cap}/sub/a.css")
        self.assertEqual((st, h["content-type"]), (200, "text/css"))

    def test_root_of_capability_serves_index(self):
        st, body, _ = self._req(f"/{self.cap}/")
        self.assertEqual((st, body), (200, b"<h1>deck</h1>"))

    def test_wrong_or_missing_capability_is_404(self):
        self.assertEqual(self._req("/wrong-token/index.html")[0], 404)
        self.assertEqual(self._req("/index.html")[0], 404)

    def test_traversal_is_refused(self):
        for p in (f"/{self.cap}/../secret-outside.txt",
                  f"/{self.cap}/sub/../../secret-outside.txt",
                  f"/{self.cap}/%2e%2e/secret-outside.txt"):
            self.assertEqual(self._req(p)[0], 404, p)

    def test_no_directory_listing(self):
        self.assertEqual(self._req(f"/{self.cap}/sub/")[0], 404)
        self.assertEqual(self._req(f"/{self.cap}/sub")[0], 404)

    def test_writes_and_other_methods_are_405(self):
        for m in ("POST", "PUT", "DELETE", "PATCH"):
            self.assertEqual(self._req(f"/{self.cap}/index.html", m)[0],
                             405, m)

    def test_head_matches_get_without_body(self):
        st, body, h = self._req(f"/{self.cap}/index.html", "HEAD")
        self.assertEqual((st, body), (200, b""))
        self.assertEqual(h["content-length"], str(len(b"<h1>deck</h1>")))

    def test_host_header_gate_blocks_dns_rebinding(self):
        self.assertEqual(
            self._req(f"/{self.cap}/index.html", host="evil.example")[0], 403)
        self.assertEqual(
            self._req(f"/{self.cap}/index.html",
                      host=f"localhost:{self.port}")[0], 200)

    def test_expired_capability_is_403(self):
        srv2, cap2 = serve.build_server(Path(self.tmp.name), port=0, ttl=0)
        port2 = srv2.server_address[1]
        threading.Thread(target=srv2.serve_forever, daemon=True).start()
        try:
            time.sleep(0.05)
            c = http.client.HTTPConnection("127.0.0.1", port2, timeout=5)
            c.request("GET", f"/{cap2}/index.html",
                      headers={"Host": f"127.0.0.1:{port2}"})
            self.assertEqual(c.getresponse().status, 403)
            c.close()
        finally:
            srv2.shutdown()

    def test_positive_control_wrong_capability_differs_from_right(self):
        """The 404s above are the guard firing, not a broken server."""
        self.assertEqual(self._req(f"/{self.cap}/index.html")[0], 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
