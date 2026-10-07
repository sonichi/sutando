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


class TestResolveCloudOrigin(unittest.TestCase):
    """A retired production origin, from any source, reads as the current one.

    A skill manifest's `config` block is surfaced to process.env for the whole
    engine, so one manifest can name the origin every consumer resolves. The
    session lives under the current origin's key; resolving the retired one
    made read_keychain_auth look for a key that never exists."""

    def test_default_when_unset(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            os_env = __import__("os").environ
            os_env.pop("AG2_CLOUD_ORIGIN", None)
            self.assertEqual(cloud_auth.resolve_cloud_origin(), cloud_auth.DEFAULT_CLOUD_ORIGIN)

    def test_retired_origin_reads_as_current(self):
        for retired in cloud_auth.RETIRED_CLOUD_ORIGINS:
            with mock.patch.dict("os.environ", {"AG2_CLOUD_ORIGIN": retired + "/"}):
                self.assertEqual(cloud_auth.resolve_cloud_origin(), cloud_auth.DEFAULT_CLOUD_ORIGIN)

    def test_other_origin_is_kept(self):
        with mock.patch.dict("os.environ", {"AG2_CLOUD_ORIGIN": "http://127.0.0.1:3000/"}):
            self.assertEqual(cloud_auth.resolve_cloud_origin(), "http://127.0.0.1:3000")

    def test_session_found_under_current_key_when_env_names_retired_origin(self):
        current_key = cloud_auth.origin_vault_key(cloud_auth.DEFAULT_CLOUD_ORIGIN)
        store = {current_key: "tok-current"}
        with mock.patch.dict("os.environ", {"AG2_CLOUD_ORIGIN": cloud_auth.RETIRED_CLOUD_ORIGINS[0]}):
            origin, tok = cloud_auth.read_keychain_auth(get=store.get)[:2]
        self.assertEqual((origin, tok), (cloud_auth.DEFAULT_CLOUD_ORIGIN, "tok-current"))

    def test_no_skill_manifest_names_a_retired_origin(self):
        offenders = []
        for m in (ROOT / "skills").glob("*/manifest.json"):
            cfg = json.loads(m.read_text()).get("config") or {}
            if cfg.get("AG2_CLOUD_ORIGIN") in cloud_auth.RETIRED_CLOUD_ORIGINS:
                offenders.append(str(m.relative_to(ROOT)))
        self.assertEqual(offenders, [])


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


class TestReadCloudAuthOrder(unittest.TestCase):
    """P1-11: under the desktop host the Keychain session outranks a leftover
    cloud-auth.json; elsewhere the file order is unchanged."""

    def _ws(self, tmp, token="sutk_file"):
        ws = Path(tmp) / "ws"
        (ws / "state" / "auth").mkdir(parents=True)
        (ws / "state" / "auth" / "cloud-auth.json").write_text(
            json.dumps({"apiBase": "https://sutando.ag2.space", "token": token}))
        return ws

    def test_file_wins_outside_the_desktop_host(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(cloud_auth.os.environ, {}, clear=False), \
                mock.patch.object(cloud_auth.Path, "home", return_value=Path(tmp) / "home"):
            cloud_auth.os.environ.pop("SUTANDO_PACKAGED", None)
            cloud_auth.os.environ.pop("SUTANDO_APP_SUPPORT", None)
            ws = self._ws(tmp)
            keychain = lambda: ("https://sutando.ag2.space", "sutk_keychain")  # noqa: E731
            self.assertEqual(cloud_auth.read_cloud_auth(ws, keychain_auth=keychain)[1], "sutk_file")

    def test_the_switch_is_the_marker_the_packaged_core_actually_carries(self):
        """Review of #4867 (Rui): SUTANDO_PACKAGED=1 reaches the sidecar only; the
        core's environment on a packaged install carries SUTANDO_APP_SUPPORT
        (verified with `ps eww` on the live core). The switch keys on that, the
        same variable channel_env_containment already treats as the host marker."""
        clean = {k: v for k, v in cloud_auth.os.environ.items() if k not in ("SUTANDO_PACKAGED", "SUTANDO_APP_SUPPORT")}
        with mock.patch.dict(cloud_auth.os.environ, clean, clear=True):
            self.assertFalse(cloud_auth.keychain_first())
        with mock.patch.dict(cloud_auth.os.environ, {**clean, "SUTANDO_APP_SUPPORT": "/Users/x/Library/Application Support/space.ag2.app"}, clear=True):
            self.assertTrue(cloud_auth.keychain_first())
        with mock.patch.dict(cloud_auth.os.environ, {**clean, "SUTANDO_APP_SUPPORT": "  "}, clear=True):
            self.assertFalse(cloud_auth.keychain_first(), "a blank-but-set value is not the host")
        with mock.patch.dict(cloud_auth.os.environ, {**clean, "SUTANDO_PACKAGED": "1"}, clear=True):
            self.assertTrue(cloud_auth.keychain_first(), "the sidecar's marker is still accepted")
        src = (Path(cloud_auth.__file__).parent / "channel_env_containment.py").read_text()
        self.assertIn('os.environ.get("SUTANDO_APP_SUPPORT")', src, "the two host markers must stay the same variable")

    def test_keychain_wins_under_the_desktop_host(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(cloud_auth.os.environ, {"SUTANDO_APP_SUPPORT": tmp}), \
                mock.patch.object(cloud_auth.Path, "home", return_value=Path(tmp) / "home"):
            ws = self._ws(tmp)
            keychain = lambda: ("https://sutando.ag2.space", "sutk_keychain")  # noqa: E731
            self.assertEqual(cloud_auth.read_cloud_auth(ws, keychain_auth=keychain)[1], "sutk_keychain")
            # Signed out in the Keychain (no session, or the sign-out sentinel): the
            # leftover file is not consulted; under the host it is only ever stale.
            calls = []
            def none():
                calls.append(1)
                return (None, None)
            cloud_auth.os.environ.pop("SUTANDO_METERING_HEADERS", None)
            self.assertEqual(cloud_auth.read_cloud_auth(ws, keychain_auth=none), (None, None))
            self.assertEqual(len(calls), 1)
            signed_out = lambda: cloud_auth.read_keychain_auth(get=lambda k: cloud_auth.SIGNED_OUT_SENTINEL)  # noqa: E731
            self.assertEqual(cloud_auth.read_cloud_auth(ws, keychain_auth=signed_out), (None, None))
            self.assertTrue((ws / "state" / "auth" / "cloud-auth.json").exists(), "the file is left alone, just not read")
            # The supervisor's metering env for a signed-in run is still honoured under the host.
            with mock.patch.dict(cloud_auth.os.environ, {
                "SUTANDO_METERING_HEADERS": json.dumps({"Authorization": "Bearer sutk_metering"}),
                "SUTANDO_METERING_ENDPOINT": "https://sutando.ag2.space/api/usage/v2"}):
                self.assertEqual(cloud_auth.read_cloud_auth(ws, keychain_auth=none),
                                 ("https://sutando.ag2.space", "sutk_metering"))


class TestSignedOutIsTerminalUnderTheHost(unittest.TestCase):
    """Review of #4871 (Rui): a sign-out marker on the current origin must end the
    lookup under the desktop host; the retired-origin carry-over and the bare
    pre-scoping key are older sessions."""

    def _store(self, **extra):
        cur = cloud_auth.origin_vault_key(cloud_auth.DEFAULT_CLOUD_ORIGIN)
        store = {cur: cloud_auth.SIGNED_OUT_SENTINEL, **extra}
        return store.get

    def test_the_retired_origin_key_does_not_serve_after_sign_out(self):
        retired = cloud_auth.origin_vault_key(cloud_auth.RETIRED_CLOUD_ORIGINS[0])
        get = self._store(**{retired: "sutk_OLD_retired"})
        with mock.patch.dict(cloud_auth.os.environ, {}, clear=False):
            cloud_auth.os.environ.pop("AG2_CLOUD_ORIGIN", None)
            self.assertEqual(cloud_auth.read_keychain_auth(get=get, signed_out_is_terminal=True), (None, None))
            # Outside the host the carry-over is still honoured (unchanged behaviour).
            self.assertEqual(cloud_auth.read_keychain_auth(get=get)[1], "sutk_OLD_retired")

    def test_the_bare_token_does_not_serve_after_sign_out(self):
        get = self._store(AG2_CLOUD_TOKEN="sutk_OLD_bare")
        with mock.patch.dict(cloud_auth.os.environ, {}, clear=False):
            cloud_auth.os.environ.pop("AG2_CLOUD_ORIGIN", None)
            self.assertEqual(cloud_auth.read_keychain_auth(get=get, signed_out_is_terminal=True), (None, None))
            self.assertEqual(cloud_auth.read_keychain_auth(get=get)[1], "sutk_OLD_bare")

    def test_read_cloud_auth_under_the_host_is_terminal_too(self):
        import tempfile
        retired = cloud_auth.origin_vault_key(cloud_auth.RETIRED_CLOUD_ORIGINS[0])
        store = {cloud_auth.origin_vault_key(cloud_auth.DEFAULT_CLOUD_ORIGIN): cloud_auth.SIGNED_OUT_SENTINEL,
                 retired: "sutk_OLD_retired", "AG2_CLOUD_TOKEN": "sutk_OLD_bare"}
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(cloud_auth.os.environ, {"SUTANDO_PACKAGED": "1"}), \
                mock.patch.object(cloud_auth.Path, "home", return_value=Path(tmp) / "home"), \
                mock.patch.object(cloud_auth, "keychain_get", store.get):
            cloud_auth.os.environ.pop("AG2_CLOUD_ORIGIN", None)
            cloud_auth.os.environ.pop("SUTANDO_METERING_HEADERS", None)
            ws = Path(tmp) / "ws"
            (ws / "state" / "auth").mkdir(parents=True)
            (ws / "state" / "auth" / "cloud-auth.json").write_text(json.dumps({"token": "sutk_file"}))
            self.assertEqual(cloud_auth.read_cloud_auth(ws), (None, None))
            # A live session on the current origin still serves.
            store[cloud_auth.origin_vault_key(cloud_auth.DEFAULT_CLOUD_ORIGIN)] = "sutk_live"
            self.assertEqual(cloud_auth.read_cloud_auth(ws)[1], "sutk_live")


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
