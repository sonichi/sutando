#!/usr/bin/env python3
"""Regression test: check_quota_telemetry must surface a proxy that is up but
producing no quota state.

The gap it covers: quota-state.json is written by the credential proxy from
upstream response headers, so it only appears if a core actually ROUTES
through the proxy. src/startup.sh is the only thing exporting
ANTHROPIC_BASE_URL=http://localhost:7846, and a supervisor-launched core
never runs startup.sh. On such a host the proxy is healthy and listening,
every check is green, and quota telemetry is silently absent forever — the
proactive loop's budget check reads "unknown" every pass with no explanation.

The pre-existing credential-proxy check cannot catch this: it is a plain
TCP-listening probe (correct for a forwarding proxy with no liveness
endpoint), so "listening" is the most it can ever assert.

Run: python3 tests/health-check-quota-telemetry.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations
import importlib.util
import json
import os
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load_health_check():
    spec = importlib.util.spec_from_file_location(
        "health_check_quota_test", REPO / "src" / "health-check.py"
    )
    hc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hc)
    return hc


class TestQuotaTelemetryCheck(unittest.TestCase):
    def setUp(self):
        self.hc = _load_health_check()
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        (self.ws / "state").mkdir(parents=True, exist_ok=True)
        self.hc.WORKSPACE_DIR = self.ws

    def tearDown(self):
        self._tmp.cleanup()

    def _write_quota(self, mtime_age_sec: float = 0.0) -> Path:
        p = self.ws / "state" / "quota-state.json"
        p.write_text('{"remaining_pct": 42}')
        if mtime_age_sec:
            past = time.time() - mtime_age_sec
            os.utime(p, (past, past))
        return p

    def test_proxy_up_but_no_quota_state_warns(self):
        """The actual bug: green everywhere, telemetry silently dead."""
        r = self.hc.check_quota_telemetry("ok")
        self.assertEqual(r["status"], "warn")
        self.assertIn("never written quota-state.json", r["detail"])
        # The detail must name the cause, not just the symptom — otherwise the
        # reader has no idea why an up proxy produces nothing.
        self.assertIn("ANTHROPIC_BASE_URL", r["detail"])

    def test_proxy_down_stays_silent(self):
        """Not every host routes through the proxy, and its own check already
        reports it as down. Warning twice would be noise."""
        for status in ("warn", "down"):
            r = self.hc.check_quota_telemetry(status)
            self.assertEqual(r["status"], "ok", f"status={status}")
            self.assertIn("not expected", r["detail"])

    def test_quota_state_present_is_ok_with_age(self):
        self._write_quota()
        r = self.hc.check_quota_telemetry("ok")
        self.assertEqual(r["status"], "ok")
        self.assertIn("present", r["detail"])

    def test_old_quota_state_does_not_warn(self):
        """Deliberate: a quiet core legitimately writes nothing for a long
        time. An age threshold would fire on healthy idle hosts, so absence —
        not staleness — is the signal. Pin it so nobody 'improves' this into
        a flaky check later.

        Still true, and deliberately left byte-identical: age ALONE never
        warns. The stale branch added below fires only when a second signal
        rules the quiet-core explanation out."""
        self._write_quota(mtime_age_sec=60 * 60 * 24 * 3)
        r = self.hc.check_quota_telemetry("ok")
        self.assertEqual(r["status"], "ok")
        self.assertIn("4320m ago", r["detail"])

    # --- presence is satisfied by a HISTORICAL write ------------------------
    # The check exists to catch "proxy up, nothing routing through it". It sees
    # only the never-wired shape: a host that WAS wired, wrote the file once and
    # then lost the wiring keeps that file forever and reads green forever.

    def _write_core_status(self, mtime_age_sec: float = 0.0) -> Path:
        p = self.ws / "state" / "core-status.json"
        p.write_text('{"status": "running"}')
        if mtime_age_sec:
            past = time.time() - mtime_age_sec
            os.utime(p, (past, past))
        return p

    def test_stale_quota_while_agent_is_working_warns(self):
        """The uncaught shape: wiring lost after the first write.

        Observed on this fleet — quota-state 323h old, credential-proxy ok,
        quota-telemetry ok, and the loop's budget gate quoting 13-day-old
        percentages as current."""
        self._write_quota(mtime_age_sec=60 * 60 * 24 * 13)
        self._write_core_status(mtime_age_sec=60)
        r = self.hc.check_quota_telemetry("ok")
        self.assertEqual(r["status"], "warn")
        self.assertIn("stale", r["detail"])
        # Name the cause and both ages, or the reader cannot act on it.
        self.assertIn("ANTHROPIC_BASE_URL", r["detail"])
        self.assertIn("312h", r["detail"])
        self.assertIn("1m", r["detail"])

    def test_idle_host_with_stale_quota_stays_silent(self):
        """The false positive the original decision was protecting against,
        now pinned explicitly: nothing has run, so nothing SHOULD have written
        quota headers. A stale core-status is what makes 'quiet' evidence."""
        self._write_quota(mtime_age_sec=60 * 60 * 24 * 13)
        self._write_core_status(mtime_age_sec=60 * 60 * 9)
        r = self.hc.check_quota_telemetry("ok")
        self.assertEqual(r["status"], "ok", r["detail"])

    def test_unknown_agent_activity_stays_silent(self):
        """No core-status.json = no evidence either way. 'Unknown' must not
        collapse into 'idle' OR 'working' — a check that cannot rule out the
        quiet-core explanation has not earned a warning."""
        self._write_quota(mtime_age_sec=60 * 60 * 24 * 13)
        r = self.hc.check_quota_telemetry("ok")
        self.assertEqual(r["status"], "ok", r["detail"])

    def test_fresh_quota_with_active_agent_is_ok(self):
        """The healthy wired host — the case that must never be warned at."""
        self._write_quota(mtime_age_sec=5 * 60)
        self._write_core_status(mtime_age_sec=10)
        r = self.hc.check_quota_telemetry("ok")
        self.assertEqual(r["status"], "ok", r["detail"])
        self.assertIn("present", r["detail"])

    def test_just_under_the_stale_threshold_is_ok(self):
        """Boundary, so the threshold is a decision rather than an accident."""
        self._write_quota(mtime_age_sec=self.hc.QUOTA_STATE_STALE_SEC - 120)
        self._write_core_status(mtime_age_sec=10)
        r = self.hc.check_quota_telemetry("ok")
        self.assertEqual(r["status"], "ok", r["detail"])

    def test_unreadable_core_status_is_unknown_not_idle(self):
        """A core-status.json that exists but cannot be stat'd is UNKNOWN, so
        the stale branch must stay closed. Treating a read error as 'the agent
        is working' would turn one unlucky race into a spurious warning."""
        self._write_quota(mtime_age_sec=60 * 60 * 24 * 13)
        self._write_core_status(mtime_age_sec=60)
        real_stat = Path.stat

        def _boom(self, *a, **kw):
            if self.name == "core-status.json":
                raise OSError("boom")
            return real_stat(self, *a, **kw)

        with mock.patch.object(Path, "stat", _boom):
            r = self.hc.check_quota_telemetry("ok")
        self.assertEqual(r["status"], "ok", r["detail"])

    # --- runtime scoping (#2445 review 2, qingyun) --------------------------
    # `core-status.json` is a CROSS-RUNTIME contract: a Codex pass refreshes it too,
    # while producing no Anthropic quota headers. Treating any fresh write as proof a
    # request should have crossed the proxy warns on a perfectly healthy Codex host —
    # which trains operators to ignore the very check this probe exists to make
    # actionable. Same defect shape the probe itself targets: a proxy for the property
    # ("the agent did something") standing in for the property ("an Anthropic request
    # should have traversed the proxy").

    def _write_runtime(self, runtime: str | None, raw: str | None = None) -> Path:
        p = self.ws / "state" / "core-runtime.json"
        p.write_text(raw if raw is not None else json.dumps({"runtime": runtime}))
        return p

    def test_codex_runtime_stale_quota_stays_silent(self):
        """The false positive qingyun demonstrated: healthy Codex pass, stale quota."""
        self._write_quota(mtime_age_sec=60 * 60 * 24 * 13)
        self._write_core_status(mtime_age_sec=60)
        self._write_runtime("codex")
        r = self.hc.check_quota_telemetry("ok")
        self.assertEqual(r["status"], "ok", r["detail"])

    def test_absent_core_runtime_still_warns(self):
        """Absence positively excludes Codex — its launcher is the only writer, and it
        writes unconditionally. Treating absence as 'unknown' would silence the check on
        every Claude host, i.e. ship a no-op."""
        self._write_quota(mtime_age_sec=60 * 60 * 24 * 13)
        self._write_core_status(mtime_age_sec=60)
        r = self.hc.check_quota_telemetry("ok")
        self.assertEqual(r["status"], "warn", r["detail"])

    def test_claude_runtime_still_warns(self):
        """A positively-identified proxy-routed runtime keeps the detection (#2406)."""
        self._write_quota(mtime_age_sec=60 * 60 * 24 * 13)
        self._write_core_status(mtime_age_sec=60)
        self._write_runtime("claude")
        r = self.hc.check_quota_telemetry("ok")
        self.assertEqual(r["status"], "warn", r["detail"])

    def test_unreadable_core_runtime_stays_silent(self):
        """Malformed JSON cannot rule Codex out, and a corrupt status file must never
        manufacture a health warning."""
        self._write_quota(mtime_age_sec=60 * 60 * 24 * 13)
        self._write_core_status(mtime_age_sec=60)
        self._write_runtime(None, raw="{not json")
        r = self.hc.check_quota_telemetry("ok")
        self.assertEqual(r["status"], "ok", r["detail"])

    # --- a marker left by a PREVIOUS core carries no information -------------
    # Nothing resets core-runtime.json, and only the Codex launcher writes it, so a
    # Codex -> Claude switch leaves a stale {"runtime":"codex"} behind (#2406 saw this
    # live on 2026-07-30). Trusting it silences this check on a host that IS
    # proxy-routed — the mirror of the false positive the runtime gate was added for.

    def _write_alive(self, started_at: float) -> Path:
        d = self.ws / "state" / "cores"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{self.hc._host_label()}.alive"
        p.write_text(json.dumps({"host": "h", "started_at": started_at}))
        return p

    def test_stale_codex_marker_from_a_previous_core_does_not_silence(self):
        """The false NEGATIVE: switched to Claude, stale codex marker still on disk."""
        self._write_quota(mtime_age_sec=60 * 60 * 24 * 13)
        self._write_core_status(mtime_age_sec=60)
        (self.ws / "state" / "core-runtime.json").write_text(
            json.dumps({"runtime": "codex", "started_at": time.time() - 86400})
        )
        self._write_alive(time.time() - 300)          # current core started AFTER the marker
        r = self.hc.check_quota_telemetry("ok")
        self.assertEqual(r["status"], "warn", r["detail"])

    def test_current_codex_marker_still_silences(self):
        """A marker from the RUNNING core is authoritative — don't over-correct."""
        self._write_quota(mtime_age_sec=60 * 60 * 24 * 13)
        self._write_core_status(mtime_age_sec=60)
        now = time.time()
        (self.ws / "state" / "core-runtime.json").write_text(
            json.dumps({"runtime": "codex", "started_at": now - 60})
        )
        self._write_alive(now - 300)                  # core started BEFORE the marker
        r = self.hc.check_quota_telemetry("ok")
        self.assertEqual(r["status"], "ok", r["detail"])

    def test_marker_without_started_at_is_taken_at_face_value(self):
        """No evidence of staleness is not evidence of staleness."""
        self._write_quota(mtime_age_sec=60 * 60 * 24 * 13)
        self._write_core_status(mtime_age_sec=60)
        (self.ws / "state" / "core-runtime.json").write_text(json.dumps({"runtime": "codex"}))
        self._write_alive(time.time() - 300)
        r = self.hc.check_quota_telemetry("ok")
        self.assertEqual(r["status"], "ok", r["detail"])

    def test_missing_heartbeat_leaves_the_marker_trusted(self):
        """Without a heartbeat there is nothing to compare against."""
        self._write_quota(mtime_age_sec=60 * 60 * 24 * 13)
        self._write_core_status(mtime_age_sec=60)
        (self.ws / "state" / "core-runtime.json").write_text(
            json.dumps({"runtime": "codex", "started_at": time.time() - 86400})
        )
        r = self.hc.check_quota_telemetry("ok")
        self.assertEqual(r["status"], "ok", r["detail"])

    def test_codex_runtime_does_not_suppress_the_ABSENT_file_warning(self):
        """Runtime scoping applies only to the staleness branch. A proxy that never
        wrote quota-state at all is still broken wiring worth reporting."""
        self._write_core_status(mtime_age_sec=60)
        self._write_runtime("codex")
        r = self.hc.check_quota_telemetry("ok")
        self.assertEqual(r["status"], "warn", r["detail"])
        self.assertIn("never written quota-state.json", r["detail"])

    def test_stale_quota_with_proxy_down_stays_silent(self):
        """Proxy-down short-circuits before any staleness reasoning: its own
        check already reports it, and warning twice for one cause is noise."""
        self._write_quota(mtime_age_sec=60 * 60 * 24 * 13)
        self._write_core_status(mtime_age_sec=60)
        for status in ("warn", "down"):
            r = self.hc.check_quota_telemetry(status)
            self.assertEqual(r["status"], "ok", f"status={status}")

    def test_stat_failure_still_reports_present(self):
        """`exists()` true but `stat()` raising is rare (file removed mid-check,
        permissions changed) — but a health tick must degrade to a less precise
        detail, never raise. Without this guard one unlucky race takes down the
        whole check run, which is strictly worse than losing the age string."""
        self._write_quota()
        # `exists()` calls stat() internally and swallows OSError by returning
        # False — patching stat alone would silently exercise the ABSENT branch
        # instead, so exists() is pinned True to isolate the one being tested.
        with mock.patch.object(Path, "exists", return_value=True), mock.patch.object(
            Path, "stat", side_effect=OSError("boom")
        ):
            r = self.hc.check_quota_telemetry("ok")
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["detail"], "quota state present")


if __name__ == "__main__":
    unittest.main(verbosity=2)
