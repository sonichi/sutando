#!/usr/bin/env python3
"""Tests for `_credential_proxy_wedge_from_quota_state` (`src/health-check.py`): pins
that a listening-but-uncredentialed proxy escalates to 'warn', self-heals, ignores a
record that predates a restart, and stays silent on a missing/unreadable file or a
down port. See the PR body for the incident this closes.

Run: python3 tests/health-check-credential-proxy-drivability.test.py
Exit 0 on pass, 1 on fail.
"""

from __future__ import annotations
import importlib.util
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location("hc", REPO / "src" / "health-check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hc = _load_module()


class CredentialProxyDrivabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name)
        (self.workspace / "state").mkdir(parents=True, exist_ok=True)
        patcher = patch.object(hc, "WORKSPACE_DIR", self.workspace)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_state(self, **fields):
        path = self.workspace / "state" / "quota-state.json"
        path.write_text(json.dumps(fields))
        return path

    def _check(self, status="ok", proc_starts=()):
        # Isolate from this host's real credential-proxy process (unpatched, the guard
        # below would depend on whatever _proc_lstarts finds actually running here).
        check = {"status": status, "detail": "listening" if status != "down" else "not listening"}
        with patch.object(hc, "_proc_lstarts", return_value=(list(proc_starts), {})):
            hc._credential_proxy_wedge_from_quota_state(check)
        return check

    def test_exhausted_escalates_ok_to_warn(self):
        self._write_state(credential_state="exhausted",
                           credential_state_detail="stored token expired, refresh unavailable",
                           credential_state_at="2026-09-27T19:30:00Z")
        check = self._check("ok")
        self.assertEqual(check["status"], "warn")
        self.assertIn("exhausted", check["detail"])
        self.assertIn("stored token expired, refresh unavailable", check["detail"])

    def test_exhausted_escalates_stale_to_warn_too(self):
        self._write_state(credential_state="exhausted", credential_state_detail="x",
                           credential_state_at="2026-09-27T19:30:00Z")
        check = self._check("stale")
        self.assertEqual(check["status"], "warn")

    def test_ok_credential_state_leaves_check_untouched(self):
        self._write_state(credential_state="ok")
        check = self._check("ok")
        self.assertEqual(check["status"], "ok")

    def test_self_heals_once_a_later_request_succeeds(self):
        # recordCredentialState('ok') overwrites the file on the next success, so a
        # later read sees 'ok', not a stale 'exhausted' from a wedge that has cleared.
        self._write_state(credential_state="ok", credential_state_detail="",
                           credential_state_at="2026-09-27T20:00:00Z")
        check = self._check("ok")
        self.assertEqual(check["status"], "ok")

    def test_missing_file_is_silent(self):
        check = self._check("ok")
        self.assertEqual(check["status"], "ok")

    def test_unreadable_json_is_silent_not_fatal(self):
        (self.workspace / "state" / "quota-state.json").write_text("{not json")
        check = self._check("ok")
        self.assertEqual(check["status"], "ok")

    def test_down_proxy_never_consults_the_file(self):
        # No request path exists to have recorded anything; must not fire even if a
        # stale 'exhausted' happens to be sitting on disk from before the outage.
        self._write_state(credential_state="exhausted", credential_state_detail="x",
                           credential_state_at="2026-09-27T19:30:00Z")
        check = self._check("down")
        self.assertEqual(check["status"], "down")

    def test_age_is_reported_when_computable(self):
        recent = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 300))
        self._write_state(credential_state="exhausted", credential_state_detail="x",
                           credential_state_at=recent)
        check = self._check("ok")
        self.assertIn("m ago)", check["detail"])

    def test_unparseable_timestamp_still_warns_without_age(self):
        self._write_state(credential_state="exhausted", credential_state_detail="x",
                           credential_state_at="not-a-timestamp")
        check = self._check("ok")
        self.assertEqual(check["status"], "warn")
        self.assertNotIn("m ago)", check["detail"])

    # ---- restart leaves a stale 'exhausted' the file never resets on its own ----
    def test_exhausted_record_older_than_the_current_process_is_ignored(self):
        recorded_at = time.time() - 3600
        self._write_state(
            credential_state="exhausted", credential_state_detail="x",
            credential_state_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(recorded_at)))
        check = self._check("ok", proc_starts=[recorded_at + 1800])
        self.assertEqual(check["status"], "ok")

    def test_exhausted_record_newer_than_the_process_still_warns(self):
        proc_started = time.time() - 3600
        self._write_state(
            credential_state="exhausted", credential_state_detail="x",
            credential_state_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(proc_started + 1800)))
        check = self._check("ok", proc_starts=[proc_started])
        self.assertEqual(check["status"], "warn")

    def test_no_process_info_falls_back_to_warning(self):
        # _proc_lstarts's probe-failure/no-match shapes both come back empty; without a
        # process start to compare against, the pre-existing behavior (warn) applies.
        self._write_state(credential_state="exhausted", credential_state_detail="x",
                           credential_state_at="2026-09-27T19:30:00Z")
        check = self._check("ok", proc_starts=[])
        self.assertEqual(check["status"], "warn")


if __name__ == "__main__":
    unittest.main(verbosity=2)
