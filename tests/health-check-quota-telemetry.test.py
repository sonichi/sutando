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

    def _write_sessions(self, *starts: float) -> Path:
        """`state/session-starts.log` — one JSON line per core launch. THIS is the
        session boundary; the heartbeat is not (see below)."""
        p = self.ws / "state" / "session-starts.log"
        p.write_text("".join(json.dumps({"session_started_at": t, "source": "start-cli"}) + "\n"
                             for t in starts))
        return p

    def test_stale_codex_marker_from_a_previous_core_does_not_silence(self):
        """The false NEGATIVE: switched to Claude, stale codex marker still on disk.

        Originally written against the heartbeat, which john-the-dev showed is not a
        session boundary (#2446) — a retained heartbeat process can be OLDER than a
        fresh marker. Re-pointed at `session-starts.log`, which both launchers append
        to on every launch.
        """
        now = time.time()
        self._write_quota(mtime_age_sec=60 * 60 * 24 * 13)
        self._write_core_status(mtime_age_sec=60)
        (self.ws / "state" / "core-runtime.json").write_text(
            json.dumps({"runtime": "codex", "started_at": now - 86400})
        )
        self._write_sessions(now - 86400, now - 300)  # current core launched AFTER the marker
        r = self.hc.check_quota_telemetry("ok")
        self.assertEqual(r["status"], "warn", r["detail"])

    def test_long_lived_heartbeat_is_not_a_session_boundary(self):
        """john-the-dev, #2446: the heartbeat CANNOT stand in for session start.

        `core_heartbeat.py` stamps `_STARTED_AT` once at module load and both launch
        paths RETAIN an existing heartbeat process, so `.alive.started_at` measures the
        heartbeat process's age, not the session's. Here the heartbeat is an hour old
        while the codex core launched 60s ago — comparing against it made the staleness
        check silently useless. The boundary is `session-starts.log`, which both
        launchers append to on every launch.
        """
        now = time.time()
        self._write_quota(mtime_age_sec=60 * 60 * 24 * 13)
        self._write_core_status(mtime_age_sec=60)
        (self.ws / "state" / "core-runtime.json").write_text(
            json.dumps({"runtime": "codex", "started_at": now - 60})
        )
        self._write_alive(now - 3600)          # heartbeat far older than the marker
        self._write_sessions(now - 60)         # ...but the core launched WITH the marker
        r = self.hc.check_quota_telemetry("ok")
        # codex genuinely IS the current runtime here, so silence is correct — and now
        # it is correct for the right reason rather than by accident.
        self.assertEqual(r["status"], "ok", r["detail"])

    def test_marker_from_a_previous_launch_warns_against_session_log(self):
        """The case the guard exists for: Codex ran yesterday, Claude relaunched today."""
        now = time.time()
        self._write_quota(mtime_age_sec=60 * 60 * 24 * 13)
        self._write_core_status(mtime_age_sec=60)
        (self.ws / "state" / "core-runtime.json").write_text(
            json.dumps({"runtime": "codex", "started_at": now - 86400})
        )
        self._write_alive(now - 3600)
        self._write_sessions(now - 86400, now - 600)   # newest launch is AFTER the marker
        r = self.hc.check_quota_telemetry("ok")
        self.assertEqual(r["status"], "warn", r["detail"])

    def test_same_launch_timestamp_skew_is_not_staleness(self):
        """qingyun, #2446: the two launch records come from SEPARATE `date +%s` calls.

        `codex/cli/start-cli.sh:240` stamps core-runtime.json and `:243` appends
        session-starts.log, so ONE launch can produce started_at=N and
        session_started_at=N+1 when the second rolls between them. A strict `<` then
        reads a CURRENT marker as stale and emits the exact false proxy warning this
        check exists to suppress. Her reproducer used 1000/1001.
        """
        # ONE captured timestamp: calling time.time() twice made the boundary case
        # drift microseconds past the margin and fail intermittently — a flaky test of
        # my own making, caught before it shipped.
        now = time.time()
        for label, marker_at, session_at in (
            ("1s skew (her repro)", 1000, 1001),
            ("skew exactly at the margin", now - self.hc.LAUNCH_RECORD_SKEW_SEC, now),
        ):
            with self.subTest(label=label):
                self._write_quota(mtime_age_sec=60 * 60 * 24 * 13)
                self._write_core_status(mtime_age_sec=60)
                (self.ws / "state" / "core-runtime.json").write_text(
                    json.dumps({"runtime": "codex", "started_at": marker_at})
                )
                self._write_sessions(session_at)
                r = self.hc.check_quota_telemetry("ok")
                self.assertEqual(r["status"], "ok", f"{label}: {r['detail']}")

    def test_margin_does_not_mask_a_real_previous_core(self):
        """The slack must not swallow genuine staleness — a previous core is separated
        from the next launch by its whole lifetime, not by seconds."""
        now = time.time()
        for label, marker_at, session_at in (
            ("just past the margin", now - 86400 - 6, now - 86400),
            ("a day apart", now - 86400, now - 600),
        ):
            with self.subTest(label=label):
                self._write_quota(mtime_age_sec=60 * 60 * 24 * 13)
                self._write_core_status(mtime_age_sec=60)
                (self.ws / "state" / "core-runtime.json").write_text(
                    json.dumps({"runtime": "codex", "started_at": marker_at})
                )
                self._write_sessions(session_at)
                r = self.hc.check_quota_telemetry("ok")
                self.assertEqual(r["status"], "warn", f"{label}: {r['detail']}")

    def test_unverifiable_marker_says_so_instead_of_a_bare_ok(self):
        """A pinned pre-Jul-13 checkout has no `session-starts.log` at all.

        The launcher write-sites first landed in `17d094f4` (2026-07-13), and a fleet
        node pinned at `ea8745a4` (Jun 21) has no such file — a live counter-example,
        not a hypothesis. There the marker cannot be dated, so a stale one silences the
        check. We KEEP that conservative reading (refusing to trust the marker would
        reinstate the false warn on every healthy pre-Jul-13 Codex host, i.e. the defect
        this check exists to remove) but we must not report a bare `ok`, which would be
        indistinguishable from a check that actually verified something.
        """
        now = time.time()
        self._write_quota(mtime_age_sec=60 * 60 * 24 * 13)
        self._write_core_status(mtime_age_sec=60)
        (self.ws / "state" / "core-runtime.json").write_text(
            json.dumps({"runtime": "codex", "started_at": now - 86400})
        )
        (self.ws / "state" / "session-starts.log").unlink(missing_ok=True)
        r = self.hc.check_quota_telemetry("ok")
        self.assertEqual(r["status"], "ok", r["detail"])
        self.assertIn("UNVERIFIABLE", r["detail"])
        self.assertIn("session-starts.log", r["detail"])

    def test_verifiable_current_marker_gets_a_clean_ok_no_caveat(self):
        """The caveat must appear ONLY when it is true — otherwise it is noise that
        trains the reader to skip the detail string."""
        now = time.time()
        self._write_quota(mtime_age_sec=60 * 60 * 24 * 13)
        self._write_core_status(mtime_age_sec=60)
        (self.ws / "state" / "core-runtime.json").write_text(
            json.dumps({"runtime": "codex", "started_at": now - 60})
        )
        self._write_sessions(now - 60)
        r = self.hc.check_quota_telemetry("ok")
        self.assertEqual(r["status"], "ok", r["detail"])
        self.assertNotIn("UNVERIFIABLE", r["detail"])

    def test_unusable_session_log_is_no_evidence_not_stale(self):
        """Every hop guarded, per the lesson from rounds 1-3: unreadable, non-JSON,
        non-object, missing key. None of them may crash, and none may claim staleness."""
        now = time.time()
        for label, body in (("absent", None), ("empty", ""),
                            ("garbage lines", 'null\n[]\n"x"\nnot json\n'),
                            ("missing key", '{"source": "start-cli"}\n'),
                            ("non-numeric", '{"session_started_at": "soon"}\n')):
            with self.subTest(label=label):
                self._write_quota(mtime_age_sec=60 * 60 * 24 * 13)
                self._write_core_status(mtime_age_sec=60)
                (self.ws / "state" / "core-runtime.json").write_text(
                    json.dumps({"runtime": "codex", "started_at": now - 86400})
                )
                p = self.ws / "state" / "session-starts.log"
                if body is None:
                    p.unlink(missing_ok=True)
                else:
                    p.write_text(body)
                r = self.hc.check_quota_telemetry("ok")   # must not raise
                self.assertEqual(r["status"], "ok", f"{label}: {r['detail']}")

    def test_current_codex_marker_still_silences(self):
        """A marker from the RUNNING core is authoritative — don't over-correct."""
        self._write_quota(mtime_age_sec=60 * 60 * 24 * 13)
        self._write_core_status(mtime_age_sec=60)
        now = time.time()
        (self.ws / "state" / "core-runtime.json").write_text(
            json.dumps({"runtime": "codex", "started_at": now - 60})
        )
        self._write_sessions(now - 300)               # launch BEFORE the marker -> not stale
        r = self.hc.check_quota_telemetry("ok")
        self.assertEqual(r["status"], "ok", r["detail"])

    def test_marker_without_started_at_is_taken_at_face_value(self):
        """No evidence of staleness is not evidence of staleness."""
        self._write_quota(mtime_age_sec=60 * 60 * 24 * 13)
        self._write_core_status(mtime_age_sec=60)
        (self.ws / "state" / "core-runtime.json").write_text(json.dumps({"runtime": "codex"}))
        self._write_sessions(time.time() - 300)
        r = self.hc.check_quota_telemetry("ok")
        self.assertEqual(r["status"], "ok", r["detail"])

    def test_missing_session_log_leaves_the_marker_trusted(self):
        """Without a launch record there is nothing to compare against.

        (Was `test_missing_heartbeat_...` — renamed with the mechanism it now asserts,
        so the name cannot outlive the thing it tests.)"""
        self._write_quota(mtime_age_sec=60 * 60 * 24 * 13)
        self._write_core_status(mtime_age_sec=60)
        (self.ws / "state" / "core-runtime.json").write_text(
            json.dumps({"runtime": "codex", "started_at": time.time() - 86400})
        )
        r = self.hc.check_quota_telemetry("ok")
        self.assertEqual(r["status"], "ok", r["detail"])

    def test_non_object_core_runtime_is_silent_not_a_crash(self):
        """Valid JSON is not necessarily an OBJECT.

        `null`, `[]`, `"codex"` and `3` all decode fine and then raise AttributeError
        on `.get` — which the (OSError, ValueError) handler does NOT catch, so a junk
        state file crashed the whole health run inside the branch this check hardens
        (qingyun, #2446). A non-object marker is exactly as uninformative as malformed
        JSON, so it takes the same silent path rather than taking the process down.
        """
        for raw in ("null", "[]", '"codex"', "3", "{bad"):
            with self.subTest(raw=raw):
                self._write_quota(mtime_age_sec=60 * 60 * 24 * 13)
                self._write_core_status(mtime_age_sec=60)
                (self.ws / "state" / "core-runtime.json").write_text(raw)
                r = self.hc.check_quota_telemetry("ok")   # must not raise
                self.assertEqual(r["status"], "ok", f"{raw}: {r['detail']}")

    def test_non_string_runtime_field_is_silent_not_a_crash_or_a_warn(self):
        """The FIELD has a schema too, not just the container.

        Two distinct failures from one loose read, which is why both are pinned here:
          * `{"runtime": []}` / `{"runtime": {}}` reach `in NON_PROXY_RUNTIMES` and raise
            TypeError (unhashable) — crashing the whole health run;
          * `{"runtime": 3}` / `{"runtime": true}` / `{}` don't crash but fall through to
            "proxy-routed" and MANUFACTURE the stale-quota warning this check exists to
            suppress.
        A field that isn't a string tells us nothing about the runtime, so it takes the
        same fail-silent path as malformed JSON (qingyun, #2446).
        """
        for raw in ('{"runtime": []}', '{"runtime": {}}', '{"runtime": 3}',
                    '{"runtime": true}', '{"runtime": null}', '{}'):
            with self.subTest(raw=raw):
                self._write_quota(mtime_age_sec=60 * 60 * 24 * 13)
                self._write_core_status(mtime_age_sec=60)
                (self.ws / "state" / "core-runtime.json").write_text(raw)
                r = self.hc.check_quota_telemetry("ok")   # must not raise
                self.assertEqual(r["status"], "ok", f"{raw}: {r['detail']}")

    def test_string_runtime_values_still_decide_normally(self):
        """The guard must not over-correct into ignoring well-formed markers."""
        for raw, expected in (('{"runtime": "codex"}', "ok"), ('{"runtime": "claude"}', "warn")):
            with self.subTest(raw=raw):
                self._write_quota(mtime_age_sec=60 * 60 * 24 * 13)
                self._write_core_status(mtime_age_sec=60)
                (self.ws / "state" / "core-runtime.json").write_text(raw)
                r = self.hc.check_quota_telemetry("ok")
                self.assertEqual(r["status"], expected, f"{raw}: {r['detail']}")

    def test_non_object_heartbeat_does_not_crash_the_staleness_compare(self):
        """The sibling read has the same shape hazard: a junk `.alive` must not take
        the run down while comparing a legitimate marker against it."""
        for raw in ("null", "[]", "not json"):
            with self.subTest(raw=raw):
                self._write_quota(mtime_age_sec=60 * 60 * 24 * 13)
                self._write_core_status(mtime_age_sec=60)
                (self.ws / "state" / "core-runtime.json").write_text(
                    json.dumps({"runtime": "codex", "started_at": time.time() - 86400})
                )
                d = self.ws / "state" / "cores"
                d.mkdir(parents=True, exist_ok=True)
                (d / f"{self.hc._host_label()}.alive").write_text(raw)
                r = self.hc.check_quota_telemetry("ok")   # must not raise
                self.assertEqual(r["status"], "ok", f"{raw}: {r['detail']}")

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
