#!/usr/bin/env python3
"""post_feedback must follow a 307/308 POST redirect, and must not leak the
owner's token to a host outside the trusted set."""
from __future__ import annotations

import importlib.util
import json
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "report_feedback", REPO / "skills" / "report-feedback" / "report-feedback.py"
)
rf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rf)

RECEIVED: list[dict] = []


class Handler(BaseHTTPRequestHandler):
    redirect_to: str | None = None

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/api/feedback" and type(self).redirect_to:
            self.send_response(307)
            self.send_header("Location", type(self).redirect_to)
            self.end_headers()
            return
        RECEIVED.append(
            {
                "path": self.path,
                "auth": self.headers.get("Authorization"),
                "payload": json.loads(body or b"{}"),
            }
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *a):  # silence
        pass


def serve():
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


class TestRedirect(unittest.TestCase):
    def setUp(self):
        RECEIVED.clear()
        Handler.redirect_to = None
        self.srv, self.base = serve()
        self.addCleanup(self.srv.shutdown)
        self.host = urllib.parse.urlsplit(self.base).hostname
        self._orig = rf.TRUSTED_API_HOSTS
        rf.TRUSTED_API_HOSTS = frozenset({self.host})
        self.addCleanup(lambda: setattr(rf, "TRUSTED_API_HOSTS", self._orig))
        # The loopback server speaks http, so these cases opt into the seam
        # explicitly. Production leaves it empty; see the downgrade tests below.
        self._orig_insecure = rf.INSECURE_REDIRECT_HOSTS
        rf.INSECURE_REDIRECT_HOSTS = frozenset({self.host})
        self.addCleanup(
            lambda: setattr(rf, "INSECURE_REDIRECT_HOSTS", self._orig_insecure))

    def test_no_redirect_posts_once(self):
        st = rf.post_feedback(f"{self.base}/api/feedback", {"title": "x"}, "tok")
        self.assertEqual(st, 200)
        self.assertEqual(len(RECEIVED), 1)
        self.assertEqual(RECEIVED[0]["auth"], "Bearer tok")

    def test_307_is_followed_with_payload_and_auth(self):
        """The regression: urllib alone raises HTTPError(307) and files nothing."""
        Handler.redirect_to = f"{self.base}/api/feedback2"

        # Control: plain urllib (what the code did before) refuses to follow.
        req = urllib.request.Request(
            f"{self.base}/api/feedback",
            data=json.dumps({"title": "x"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(cm.exception.code, 307)
        self.assertEqual(RECEIVED, [], "control must not have delivered anything")

        st = rf.post_feedback(f"{self.base}/api/feedback", {"title": "x"}, "tok")
        self.assertEqual(st, 200)
        self.assertEqual(len(RECEIVED), 1)
        self.assertEqual(RECEIVED[0]["path"], "/api/feedback2")
        self.assertEqual(RECEIVED[0]["auth"], "Bearer tok", "auth must survive the hop")
        self.assertEqual(RECEIVED[0]["payload"]["title"], "x", "body must be re-sent")

    def test_untrusted_redirect_target_does_not_get_the_token(self):
        Handler.redirect_to = "https://evil.example.com/api/feedback"
        with self.assertRaises(RuntimeError) as cm:
            rf.post_feedback(f"{self.base}/api/feedback", {"title": "x"}, "tok")
        self.assertIn("untrusted redirect host", str(cm.exception))
        self.assertEqual(RECEIVED, [])

    def test_downgrade_to_http_makes_no_second_request(self):
        """A 307 to http:// on a TRUSTED host must not replay the token."""
        rf.INSECURE_REDIRECT_HOSTS = frozenset()          # production shape
        Handler.redirect_to = f"http://{self.host}:{self.srv.server_port}/api/feedback2"
        with self.assertRaises(RuntimeError) as cm:
            rf.post_feedback(f"{self.base}/api/feedback", {"title": "x"}, "tok")
        self.assertIn("scheme", str(cm.exception))
        self.assertEqual(RECEIVED, [], "no second request may be made")

    def test_production_seam_is_empty(self):
        self.assertEqual(self._orig_insecure, frozenset(),
                         "INSECURE_REDIRECT_HOSTS must ship empty")

    def test_userinfo_in_redirect_target_is_refused(self):
        rf.INSECURE_REDIRECT_HOSTS = frozenset()
        Handler.redirect_to = f"https://attacker@{self.host}/api/feedback"
        with self.assertRaises(RuntimeError) as cm:
            rf.post_feedback(f"{self.base}/api/feedback", {"title": "x"}, "tok")
        self.assertIn("userinfo", str(cm.exception))
        self.assertEqual(RECEIVED, [])

    def test_redirect_loop_is_bounded(self):
        Handler.redirect_to = f"{self.base}/api/feedback"  # points at itself
        with self.assertRaises(urllib.error.HTTPError) as cm:
            rf.post_feedback(f"{self.base}/api/feedback", {"title": "x"}, "tok")
        self.assertEqual(cm.exception.code, 307)


class TestTrustedHosts(unittest.TestCase):
    def test_both_cloud_hosts_are_trusted(self):
        self.assertIn("sutando.ag2.ai", rf.TRUSTED_API_HOSTS)
        self.assertIn("sutando.ag2.space", rf.TRUSTED_API_HOSTS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
