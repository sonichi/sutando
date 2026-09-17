#!/usr/bin/env python3
"""Tests for src/cloud_auth.py — the shared Sutando Cloud session + request path."""

import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import cloud_auth  # noqa: E402


class TestKeyDerivation(unittest.TestCase):
    def test_matches_desktop_host_keys(self):
        # cloud_session.rs origin_key_suffix: a drifted slug or FNV hash orphans every session.
        self.assertEqual(
            cloud_auth.origin_vault_key("https://sutando.ag2.space"),
            "AG2_CLOUD_TOKEN_HTTPS___SUTANDO_AG2_SPACE_F35E6C6AC0ABE4A4",
        )

    def test_injected_reader_and_signed_out_sentinel(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(cloud_auth.read_keychain_auth(get=lambda k: cloud_auth.SIGNED_OUT_SENTINEL), (None, None))
            self.assertEqual(cloud_auth.read_keychain_auth(get=lambda k: "sutk_x"), ("https://sutando.ag2.space", "sutk_x"))


class TestCloudRequest(unittest.TestCase):
    def test_refuses_untrusted_or_plaintext_hosts(self):
        for base in ("https://evil.example", "http://sutando.ag2.space", "https://u:p@sutando.ag2.space"):
            with self.assertRaises(cloud_auth.CloudError) as ctx:
                cloud_auth.cloud_request(base, "sutk_x", "GET", "/api/me")
            self.assertEqual(ctx.exception.code, "untrusted_host")

    def test_path_must_be_api(self):
        with self.assertRaises(ValueError):
            cloud_auth.cloud_request("https://sutando.ag2.space", "t", "GET", "https://evil/x")

    def _opener(self, response=None, error=None):
        opener = mock.MagicMock()
        if error:
            opener.open.side_effect = error
        else:
            opener.open.return_value.__enter__.return_value.read.return_value = response
        return mock.patch.object(cloud_auth.urllib.request, "build_opener", return_value=opener), opener

    def test_sends_bearer_and_json_body(self):
        patch, opener = self._opener(b'{"ok": true}')
        with patch:
            out = cloud_auth.cloud_request("https://sutando.ag2.ai", "sutk_x", "POST", "/api/skills/a/install", {"agentId": "@b"})
        self.assertEqual(out, {"ok": True})
        req = opener.open.call_args.args[0]
        self.assertEqual(req.full_url, "https://sutando.ag2.space/api/skills/a/install")  # retired origin normalized
        self.assertEqual(req.get_header("Authorization"), "Bearer sutk_x")
        self.assertEqual(json.loads(req.data), {"agentId": "@b"})

    def test_http_error_carries_server_code(self):
        err = urllib.error.HTTPError("u", 402, "x", {}, io.BytesIO(b'{"error":"insufficient_credits","required":40}'))
        patch, _ = self._opener(error=err)
        with patch, self.assertRaises(cloud_auth.CloudError) as ctx:
            cloud_auth.cloud_request("https://sutando.ag2.space", "t", "POST", "/api/skills/a/install")
        self.assertEqual((ctx.exception.status, ctx.exception.code, ctx.exception.body["required"]), (402, "insufficient_credits", 40))

    def test_network_error(self):
        patch, _ = self._opener(error=urllib.error.URLError("down"))
        with patch, self.assertRaises(cloud_auth.CloudError) as ctx:
            cloud_auth.cloud_request("https://sutando.ag2.space", "t", "GET", "/api/me")
        self.assertEqual(ctx.exception.code, "network")



class TestCloudRequestEdges(unittest.TestCase):
    def test_insecure_test_host_allowed_only_when_opted_in(self):
        cloud_auth.check_trusted_base("http://127.0.0.1:9", frozenset({"127.0.0.1"}))

    def test_unreadable_error_body_timeout_and_decoding(self):
        err = urllib.error.HTTPError("u", 500, "x", {}, None)
        opener = mock.MagicMock()
        opener.open.side_effect = err
        with mock.patch.object(cloud_auth.urllib.request, "build_opener", return_value=opener), \
                mock.patch.object(err, "read", side_effect=OSError("closed")), \
                self.assertRaises(cloud_auth.CloudError) as ctx:
            cloud_auth.cloud_request("https://sutando.ag2.space", "t", "GET", "/api/me")
        self.assertEqual(ctx.exception.code, "http_500")

        opener.open.side_effect = TimeoutError()
        with mock.patch.object(cloud_auth.urllib.request, "build_opener", return_value=opener), \
                self.assertRaises(cloud_auth.CloudError) as ctx:
            cloud_auth.cloud_request("https://sutando.ag2.space", None, "GET", "/api/me")
        self.assertEqual(ctx.exception.detail, "request timed out")

        self.assertIsNone(cloud_auth._decode(b""))
        self.assertEqual(cloud_auth._decode(b"<html>"), {"detail": "<html>"})
        self.assertIsNone(cloud_auth._NoRedirect().redirect_request(None, None, 307, "", {}, "https://evil"))


if __name__ == "__main__":
    unittest.main()
