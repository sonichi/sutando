#!/usr/bin/env python3
"""`notify_gateway_for_failures` DMs the owner on ag2.space (the gateway) on a
failing health check — the surface the owner actually watches, core-independent.

Owner-requested 2026-08-01 (#2487 follow-up): route the over-quota / wedge alert
to ag2.space, not only Slack. This suite pins:

  - room resolution: an EXPLICIT owner-only REMOTE_ALERT_ROOM only (never
    inferred from a possibly-shared last-owner-activity room); else None.
  - creds resolution: url+token from the channel .env, incl. one-token form.
  - delivery: a failure DMs the owner exactly once per unchanged episode (dedup);
    a failed send does NOT record dedup (so the next tick retries).
  - a clean run sends nothing.

Run: python3 tests/health-check-notify-gateway.test.py
"""
from __future__ import annotations

import io
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("hc_gw_test", REPO / "src" / "health-check.py")
    hc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hc)
    return hc


class TestGatewayNotify(unittest.TestCase):
    def setUp(self):
        self._env_keys = ("REMOTE_TASK_URL", "REMOTE_TASK_TOKEN", "AG2_REMOTE_URL", "AG2_REMOTE_TOKEN")
        self._saved_env = {k: os.environ.pop(k, None) for k in self._env_keys}
        self.hc = _load()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.hc.WORKSPACE_DIR = self.root
        # Redirect claude_home_path to a temp channels tree.
        self.cfg = self.root / "cfg"
        (self.cfg / "channels" / "ag2space").mkdir(parents=True)
        self.hc.claude_home_path = lambda *p: self.cfg.joinpath(*p)

    def tearDown(self):
        self._tmp.cleanup()
        for key in self._env_keys:
            os.environ.pop(key, None)
            if self._saved_env[key] is not None:
                os.environ[key] = self._saved_env[key]

    def _write_env(self, text):
        (self.cfg / "channels" / "ag2space" / ".env").write_text(text)

    def _write_activity(self, cid):
        (self.root / "state").mkdir(exist_ok=True)
        (self.root / "state" / "last-owner-activity.json").write_text(
            json.dumps({"channel": "ag2space", "channel_id": cid}))

    # --- room resolution --------------------------------------------------

    def test_room_requires_explicit_config(self):
        self._write_env("REMOTE_ALERT_ROOM=!explicit:ag2.space\n")
        self.assertEqual(self.hc._gateway_owner_room(), "!explicit:ag2.space")

    def test_room_none_when_unset(self):
        self._write_env("REMOTE_TASK_URL=https://gw\n")
        self.assertIsNone(self.hc._gateway_owner_room())

    def test_room_ignores_last_activity_room_privacy(self):
        # PRIVACY (qingyun #2487 P1): the room must NOT be inferred from
        # last-owner-activity — that can be a SHARED room, and a health alert
        # could leak host/config details there. Without an explicit
        # REMOTE_ALERT_ROOM, resolution MUST be None even if a (shared-looking)
        # activity room exists.
        self._write_env("REMOTE_TASK_URL=https://gw\n")
        self._write_activity("!shared-team-room:ag2.space")
        self.assertIsNone(self.hc._gateway_owner_room())

    def test_env_parsing_skips_comments_blanks_and_missing_file(self):
        # Comments + blank lines are ignored; a missing .env yields no creds.
        self._write_env("# gateway creds\n\nREMOTE_TASK_URL=https://gw\nREMOTE_TASK_TOKEN=s\n")
        self.assertEqual(self.hc._gateway_creds(), ("https://gw", "s"))
        (self.cfg / "channels" / "ag2space" / ".env").unlink()
        self.assertIsNone(self.hc._gateway_creds())

    # --- creds ------------------------------------------------------------

    def test_creds_split_form(self):
        self._write_env("REMOTE_TASK_URL=https://gw/\nREMOTE_TASK_TOKEN=secret\n")
        self.assertEqual(self.hc._gateway_creds(), ("https://gw", "secret"))

    def test_creds_one_token_form(self):
        self._write_env("REMOTE_TASK_TOKEN=https://gw|secret\n")
        self.assertEqual(self.hc._gateway_creds(), ("https://gw", "secret"))

    def test_creds_legacy_split_form(self):
        self._write_env("AG2_REMOTE_URL=https://legacy-gw/\nAG2_REMOTE_TOKEN=legacy-secret\n")
        self.assertEqual(self.hc._gateway_creds(), ("https://legacy-gw", "legacy-secret"))

    def test_creds_legacy_one_token_form(self):
        self._write_env("AG2_REMOTE_TOKEN=https://legacy-gw|legacy-secret\n")
        self.assertEqual(self.hc._gateway_creds(), ("https://legacy-gw", "legacy-secret"))

    def test_creds_none_when_missing(self):
        self._write_env("AGENT_ID=x\n")
        self.assertIsNone(self.hc._gateway_creds())

    # --- delivery ---------------------------------------------------------

    def _fail(self, name="core-quota", detail="CORE IS OVER QUOTA"):
        return {"name": name, "status": "fail", "detail": detail}

    def test_dms_owner_once_per_episode(self):
        sent = []
        st = self.root / "state" / "gw-slacked.json"
        c = self._fail()
        self.hc.notify_gateway_for_failures([c], state_file=st, sender=lambda t: (sent.append(t) or True))
        self.hc.notify_gateway_for_failures([c], state_file=st, sender=lambda t: (sent.append(t) or True))
        self.assertEqual(len(sent), 1, "over-quota must DM ag2.space exactly once per unchanged episode")
        self.assertIn("core-quota", sent[0])

    def test_clean_run_sends_nothing(self):
        sent = []
        st = self.root / "state" / "gw2.json"
        self.hc.notify_gateway_for_failures(
            [{"name": "x", "status": "ok", "detail": "fine"}],
            state_file=st, sender=lambda t: (sent.append(t) or True))
        self.assertEqual(sent, [])

    def test_default_sender_posts_on_2xx(self):
        # Cover the real sender's happy path without a network call.
        self._write_env("REMOTE_ALERT_ROOM=!room:ag2.space\nREMOTE_TASK_URL=https://gw\nREMOTE_TASK_TOKEN=secret\n")
        captured = {}

        class FakeResp:
            status = 200

            def __enter__(self_):
                return self_

            def __exit__(self_, *a):
                return False

        import urllib.request as u
        orig = u.urlopen
        u.urlopen = lambda req, timeout=10: (captured.update(
            url=req.full_url, body=req.data), FakeResp())[1]
        try:
            ok = self.hc._default_gateway_sender("hello owner")
        finally:
            u.urlopen = orig
        self.assertTrue(ok)
        self.assertEqual(captured["url"], "https://gw/v1/room")
        self.assertIn(b'"op": "message"', captured["body"])
        self.assertIn(b'!room:ag2.space', captured["body"])

    def test_default_sender_false_when_unconfigured(self):
        # No room / no creds -> False, no network attempt.
        self._write_env("AGENT_ID=x\n")
        self.assertFalse(self.hc._default_gateway_sender("x"))

    def test_default_sender_false_on_network_error(self):
        self._write_env("REMOTE_ALERT_ROOM=!room:ag2.space\nREMOTE_TASK_URL=https://gw\nREMOTE_TASK_TOKEN=secret\n")
        import urllib.request as u
        orig = u.urlopen

        def boom(*a, **k):
            raise OSError("network down")

        u.urlopen = boom
        try:
            self.assertFalse(self.hc._default_gateway_sender("x"))
        finally:
            u.urlopen = orig

    def test_uses_default_state_file(self):
        # state_file omitted -> defaults under WORKSPACE_DIR/state; still sends once.
        sent = []
        self.hc.notify_gateway_for_failures([self._fail()], sender=lambda t: (sent.append(t) or True))
        self.assertEqual(len(sent), 1)
        self.assertTrue((self.root / "state" / "health-last-gateway.json").exists())

    def test_survives_unreadable_and_unwritable_state(self):
        # A state path that is a directory: read + write both raise, but the
        # alert must still be delivered (defensive excepts, no crash).
        st = self.root / "state" / "adir"
        st.mkdir(parents=True)
        sent = []
        self.hc.notify_gateway_for_failures([self._fail()], state_file=st, sender=lambda t: (sent.append(t) or True))
        self.assertEqual(len(sent), 1)

    def test_failed_send_is_not_deduped(self):
        # A failed send must NOT record dedup, so the next tick retries.
        st = self.root / "state" / "gw3.json"
        c = self._fail()
        calls = []
        self.hc.notify_gateway_for_failures([c], state_file=st, sender=lambda t: (calls.append(t) or False))
        self.hc.notify_gateway_for_failures([c], state_file=st, sender=lambda t: (calls.append(t) or False))
        self.assertEqual(len(calls), 2, "a failed gateway send must be retried, not silently deduped")

    def test_non_object_history_is_reset_not_crashed(self):
        st = self.root / "state" / "gw-list.json"
        st.parent.mkdir(exist_ok=True)
        st.write_text("[]")
        sent = []
        self.hc.notify_gateway_for_failures(
            [self._fail()], state_file=st, sender=lambda t: (sent.append(t) or True))
        self.assertEqual(len(sent), 1)
        self.assertIsInstance(json.loads(st.read_text()), dict)

    def test_main_notify_gateway_flag_dispatches_failures(self):
        checks = [self._fail()]
        with (
            mock.patch.object(self.hc, "run_all_checks", return_value=checks),
            mock.patch.object(self.hc, "notify_gateway_for_failures") as notify,
            mock.patch.object(
                sys,
                "argv",
                ["health-check.py", "--notify-gateway", "--json"],
            ),
            redirect_stdout(io.StringIO()),
        ):
            self.hc.main()

        notify.assert_called_once_with(checks)


class TestLaunchdMinimalEnvWiring(unittest.TestCase):
    """P1 (qingyun #2487): the launchd fallback runs with a minimal env, so the
    gateway config dir must be baked into the plist — otherwise claude_home_path
    falls back to the legacy default and the gateway-notify path can't find the
    channel creds on a workspace-scoped install. The monkeypatched unit tests
    can't catch that, so pin the wiring at the template/installer layer."""

    def test_plist_exports_claude_config_dir(self):
        plist = (REPO / "src" / "launchd" / "com.sutando.health-check-fallback.plist").read_text()
        self.assertIn("<key>CLAUDE_CONFIG_DIR</key>", plist,
                      "fallback plist must export CLAUDE_CONFIG_DIR so the minimal "
                      "launchd env resolves the canonical channels/ config dir")
        self.assertIn("__CLAUDE_CONFIG_DIR__", plist, "placeholder must be present for substitution")

    def test_installer_substitutes_claude_config_dir(self):
        inst = (REPO / "src" / "install-health-check-launchd.sh").read_text()
        self.assertIn("CLAUDE_CONFIG_DIR=$CLAUDE_CFG", inst,
                      "installer must supply the config-dir value for substitution")
        self.assertIn("claude-home-path", inst, "installer must resolve the canonical config dir")
        self.assertNotIn('CLAUDE_CFG="$CLAUDE_CONFIG_DIR"', inst,
                         "a failed canonical lookup must not continue with an empty ambient value")
        self.assertIn("could not resolve canonical Claude config directory", inst,
                      "a failed canonical lookup must stop the install clearly")

    def test_protocol_documents_explicit_owner_only_room(self):
        docs = (REPO / "docs" / "remote-gateway-protocol.md").read_text()
        row = next(line for line in docs.splitlines() if "`REMOTE_ALERT_ROOM`" in line)
        self.assertIn("none (gateway alert disabled)", row,
                      "docs must not promise the removed last-activity fallback")
        self.assertIn("owner-only", row,
                      "docs must identify the privacy requirement for the configured room")
        self.assertNotIn("latest ag2.space owner room", row)


if __name__ == "__main__":
    unittest.main(verbosity=2)
