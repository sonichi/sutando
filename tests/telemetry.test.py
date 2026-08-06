#!/usr/bin/env python3
"""Tests for src/telemetry.py — the anonymous, opt-out product telemetry module.

The load-bearing guarantee is that opting out **actually stops all network
sends** — a bug class that has shipped in other OSS telemetry (opt-out flag
read once at import, or checked but not honored). We prove it by monkeypatching
the network sink and asserting it is never touched when opted out, via every
opt-out mechanism (DO_NOT_TRACK, SUTANDO_TELEMETRY=0, disable file).

Also covers: no-op without a key, anonymous distinct-id persistence/format,
and that a normal capture DOES reach the sink when enabled.

Run: python3 tests/telemetry.test.py
"""
# PEP 604 (`X | None`) in annotations is evaluated at def-time on Python < 3.10;
# defer all annotation evaluation so this file runs on the 3.9 baseline (CR #2088,
# @qingyun-wu). No runtime annotation introspection here, so this is semantics-safe.
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"


def _load(state_dir: Path, key: str = "", env: dict | None = None):
    """Import a fresh telemetry module with a temp state dir + clean env."""
    for k in ("DO_NOT_TRACK", "SUTANDO_TELEMETRY", "POSTHOG_API_KEY",
              "SUTANDO_DEBUG_TELEMETRY", "SUTANDO_SURFACE", "SUTANDO_TELEMETRY_ID_FILE"):
        os.environ.pop(k, None)
    os.environ["SUTANDO_STATE_DIR"] = str(state_dir)
    # Isolate the durable per-install-id path into the temp tree so tests never
    # touch (or depend on) the real ~/Library/Application Support/Sutando path
    # (added with id-persistence). Defaulting it to <state_dir>/telemetry-id also
    # keeps the legacy assertions valid (durable == the state-dir file). `env` may
    # override this to exercise durable-vs-legacy migration explicitly.
    os.environ["SUTANDO_TELEMETRY_ID_FILE"] = str(state_dir / "telemetry-id")
    # Force the module's state dir to the temp path even if workspace_default
    # resolves elsewhere: point SUTANDO_STATE_DIR and stub resolve_workspace.
    os.environ.update(env or {})
    if key:
        os.environ["POSTHOG_API_KEY"] = key
    sys.path.insert(0, str(SRC))
    spec = importlib.util.spec_from_file_location("telemetry_under_test", SRC / "telemetry.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # Pin the effective key deterministically so tests don't depend on whether a
    # public key is embedded in _EMBEDDED_KEY for distribution.
    mod._KEY = key
    # No monkeypatch needed: _state_dir honors SUTANDO_STATE_DIR (set above),
    # so the real resolver code runs against the temp dir.
    assert mod._state_dir() == state_dir, "SUTANDO_STATE_DIR override should win"
    return mod


def _capture_sync(mod, event, props=None):
    """Call capture and join any spawned sender threads so assertions are
    deterministic (capture posts on a daemon thread)."""
    before = set(threading.enumerate())
    mod.capture(event, props)
    for t in set(threading.enumerate()) - before:
        t.join(timeout=2)


def run():
    passed = 0

    # 1) Opt-out via each mechanism → sink NEVER called, even with a key set.
    for mech in ("DO_NOT_TRACK", "SUTANDO_TELEMETRY", "file"):
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td)
            env = {}
            if mech == "DO_NOT_TRACK":
                env["DO_NOT_TRACK"] = "1"
            elif mech == "SUTANDO_TELEMETRY":
                env["SUTANDO_TELEMETRY"] = "0"
            mod = _load(sd, key="phc_test_key", env=env)
            if mech == "file":
                (sd).mkdir(parents=True, exist_ok=True)
                (sd / "telemetry-disabled").write_text("")
            calls = []
            mod._post = lambda payload: calls.append(payload)  # type: ignore
            assert mod.opted_out() is True, f"{mech}: opted_out should be True"
            assert mod.enabled() is False, f"{mech}: enabled should be False"
            _capture_sync(mod, "core_started", {"interval_s": 30})
            assert calls == [], f"{mech}: opt-out MUST send nothing, got {calls}"
            passed += 1
            print(f"ok   opt-out honored via {mech} — zero sends")

    # 2) No key configured → no-op even when not opted out.
    with tempfile.TemporaryDirectory() as td:
        mod = _load(Path(td), key="")
        calls = []
        mod._post = lambda payload: calls.append(payload)  # type: ignore
        assert mod.enabled() is False, "no key → disabled"
        _capture_sync(mod, "core_started")
        assert calls == [], f"no key MUST send nothing, got {calls}"
        passed += 1
        print("ok   no key configured — zero sends")

    # 3) Enabled path → sink IS called, payload is anonymous + well-formed.
    with tempfile.TemporaryDirectory() as td:
        sd = Path(td)
        mod = _load(sd, key="phc_live")
        calls = []
        mod._post = lambda payload: calls.append(payload)  # type: ignore
        assert mod.enabled() is True, "key + not opted out → enabled"
        _capture_sync(mod, "feature_used", {"feature": "morning-briefing"})
        assert len(calls) == 1, f"enabled MUST send once, got {len(calls)}"
        p = calls[0]
        assert p["event"] == "feature_used"
        assert p["api_key"] == "phc_live"
        assert p["properties"]["feature"] == "morning-briefing"
        assert "$process_person_profile" not in p["properties"], \
            "person processing on so installs appear as active users"
        assert p["properties"]["$ip"] == "", "must suppress IP storage"
        assert p["properties"]["$geoip_disable"] is True, "must disable GeoIP"
        did = p["distinct_id"]
        assert did and did != "anonymous" and len(did) == 32, f"distinct_id looks wrong: {did!r}"
        passed += 1
        print("ok   enabled — sends once, anonymous, well-formed payload")

    # 3b) Debug mode prints to stderr before send; still no send when opted out.
    with tempfile.TemporaryDirectory() as td:
        sd = Path(td)
        mod = _load(sd, key="phc_live", env={"SUTANDO_DEBUG_TELEMETRY": "1", "DO_NOT_TRACK": "1"})
        calls = []
        mod._post = lambda payload: calls.append(payload)  # type: ignore
        import io
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            _capture_sync(mod, "core_started", {"interval_s": 30})
        assert "[telemetry] core_started" in buf.getvalue(), "debug should print the event"
        assert calls == [], "debug + opted-out MUST still send nothing"
        passed += 1
        print("ok   debug mode prints but opt-out still sends nothing")

    # 4) distinct_id persists across calls (stable per install).
    with tempfile.TemporaryDirectory() as td:
        sd = Path(td)
        mod = _load(sd, key="phc_live")
        d1 = mod._distinct_id()
        d2 = mod._distinct_id()
        assert d1 == d2, "distinct_id must be stable"
        assert (sd / "telemetry-id").read_text().strip() == d1
        passed += 1
        print("ok   distinct_id persists across calls")

    # 5) Surface (desktop vs OSS) — explicit env, pgrep probe, and payload wiring.
    import unittest.mock as _um
    with tempfile.TemporaryDirectory() as td:
        mod = _load(Path(td), key="phc_live", env={"SUTANDO_SURFACE": "desktop"})
        assert mod._install_surface() == "desktop", "SUTANDO_SURFACE=desktop → desktop"
        passed += 1
        print("ok   surface: SUTANDO_SURFACE=desktop honored")
    with tempfile.TemporaryDirectory() as td:
        mod = _load(Path(td), key="phc_live", env={"SUTANDO_SURFACE": "oss"})
        assert mod._install_surface() == "oss", "SUTANDO_SURFACE=oss → oss"
        passed += 1
        print("ok   surface: SUTANDO_SURFACE=oss honored")
    with tempfile.TemporaryDirectory() as td:
        mod = _load(Path(td), key="phc_live")  # no env override → pgrep decides
        with _um.patch.object(mod.subprocess, "run",
                              return_value=_um.Mock(returncode=0, stdout="4321\n")):
            assert mod._install_surface() == "desktop", "pgrep hit → desktop"
        with _um.patch.object(mod.subprocess, "run",
                              return_value=_um.Mock(returncode=1, stdout="")):
            assert mod._install_surface() == "oss", "pgrep miss → oss"
        with _um.patch.object(mod.subprocess, "run", side_effect=OSError("boom")):
            assert mod._install_surface() == "oss", "pgrep error → oss (fail-safe)"
        passed += 1
        print("ok   surface: pgrep probe (hit/miss/error) resolves correctly")
    with tempfile.TemporaryDirectory() as td:
        mod = _load(Path(td), key="phc_live", env={"SUTANDO_SURFACE": "desktop"})
        calls = []
        mod._post = lambda payload: calls.append(payload)  # type: ignore
        _capture_sync(mod, "feature_used", {"feature": "x"})
        assert len(calls) == 1
        pr = calls[0]["properties"]
        assert pr["surface"] == "desktop", f"event-prop surface wrong: {pr.get('surface')}"
        assert pr["$set"]["surface"] == "desktop", f"$set person-prop surface wrong: {pr.get('$set')}"
        passed += 1
        print("ok   surface: attached to payload (event property + $set person property)")

    # 6) Phase-2 typed helpers send the right bucketed event + property.
    with tempfile.TemporaryDirectory() as td:
        mod = _load(Path(td), key="phc_live")
        calls = []
        mod._post = lambda payload: calls.append(payload)  # type: ignore
        before = set(threading.enumerate())
        mod.task_processed("discord")
        mod.feature_used("morning_briefing")
        for t in set(threading.enumerate()) - before:
            t.join(timeout=2)
        assert len(calls) == 2, f"two helper events expected, got {len(calls)}"
        tp = next(c for c in calls if c["event"] == "task_processed")
        fu = next(c for c in calls if c["event"] == "feature_used")
        assert tp["properties"]["source"] == "discord", tp
        assert fu["properties"]["feature"] == "morning_briefing", fu
        # Anonymity posture carries through the helpers; only the bucket ships.
        assert tp["properties"]["$ip"] == "" and tp["properties"]["$geoip_disable"] is True
        # Beyond the source bucket, the only extra keys are the standard anonymity
        # + surface envelope every event carries (#2071): surface + $set. Still no
        # task content, ids, user, or channel — that's the invariant under test.
        assert set(tp["properties"]) == {"$ip", "$geoip_disable", "source", "surface", "$set"}, \
            f"task_processed must ship ONLY the source bucket + surface envelope, got {tp['properties']}"
        passed += 1
        print("ok   task_processed/feature_used send correct bucketed events")

    # 6b) task_processed sanitizes its source against a fixed allowlist. The
    # value can come from a caller-supplied task `source:` header (local web/API
    # path), so an unknown / hostile value must collapse to "unknown" — no
    # unbounded cardinality, no accidental identifier/secret reaching PostHog
    # (CR #2274, qingyun-wu). Known surfaces pass through unchanged.
    with tempfile.TemporaryDirectory() as td:
        mod = _load(Path(td), key="phc_live")
        # Direct unit on the collapse helper.
        assert mod._coarse_source("discord") == "discord"
        assert mod._coarse_source("  Phone ") == "phone"  # normalized (case/space)
        assert mod._coarse_source("sk-secret-abc123") == "unknown"
        assert mod._coarse_source("api; DROP TABLE") == "unknown"
        assert mod._coarse_source("") == "unknown"
        # End-to-end: a hostile source never leaves the allowlist on the wire.
        calls = []
        mod._post = lambda payload: calls.append(payload)  # type: ignore
        before = set(threading.enumerate())
        mod.task_processed("s3cr3t-token-leak")
        for t in set(threading.enumerate()) - before:
            t.join(timeout=2)
        tp = next(c for c in calls if c["event"] == "task_processed")
        assert tp["properties"]["source"] == "unknown", \
            f"hostile source must collapse to 'unknown', got {tp['properties']['source']!r}"
        passed += 1
        print("ok   task_processed collapses unknown/hostile source to 'unknown'")

    # 7) Short-lived callers can select the bounded synchronous path.
    with tempfile.TemporaryDirectory() as td:
        mod = _load(Path(td), key="phc_live")
        calls = []
        mod._post = lambda payload, timeout=5: calls.append((payload, timeout))  # type: ignore
        mod.feature_used("morning_briefing", flush=True)
        assert len(calls) == 1, f"flush path must send exactly once, got {len(calls)}"
        payload, timeout = calls[0]
        assert payload["event"] == "feature_used"
        assert payload["properties"]["feature"] == "morning_briefing"
        assert timeout == 1, f"flush path must stay bounded to 1s, got {timeout}"
        passed += 1
        print("ok   feature_used flush path sends synchronously with 1s bound")

    # 8) The typed helpers ALSO honor opt-out (no path around capture()).
    with tempfile.TemporaryDirectory() as td:
        mod = _load(Path(td), key="phc_live", env={"DO_NOT_TRACK": "1"})
        calls = []
        mod._post = lambda payload: calls.append(payload)  # type: ignore
        before = set(threading.enumerate())
        mod.task_processed("slack")
        mod.feature_used("daily_insight")
        for t in set(threading.enumerate()) - before:
            t.join(timeout=2)
        assert calls == [], f"opt-out MUST silence phase-2 helpers too, got {calls}"
        passed += 1
        print("ok   phase-2 helpers honor opt-out (zero sends)")

    # 9) A short-lived subprocess can flush feature telemetry before exit.
    # This is the production lifecycle of morning-briefing.py/daily-insight.py;
    # their old daemon sender was terminated with the interpreter.
    with tempfile.TemporaryDirectory() as td:
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                received.append(json.loads(self.rfile.read(length)))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *_args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever)
        server_thread.start()
        try:
            env = {
                **os.environ,
                "POSTHOG_API_KEY": "phc_subprocess_test",
                "POSTHOG_HOST": f"http://127.0.0.1:{server.server_port}",
                "SUTANDO_STATE_DIR": td,
                "SUTANDO_SURFACE": "oss",
                "PYTHONPATH": str(SRC),
            }
            env.pop("DO_NOT_TRACK", None)
            env.pop("SUTANDO_TELEMETRY", None)
            proc = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from telemetry import feature_used; "
                    "feature_used('subprocess_probe', flush=True)",
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=5,
            )
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)

        assert proc.returncode == 0, proc.stderr
        assert len(received) == 1, \
            f"short-lived process must deliver before exit, got {len(received)}"
        assert received[0]["event"] == "feature_used"
        assert received[0]["properties"]["feature"] == "subprocess_probe"
        passed += 1
        print("ok   short-lived subprocess flushes feature_used before exit")

    # 9b) The `python3 src/telemetry.py task_processed <source>` CLI entrypoint
    # delivers a source-tagged task_processed event before exit. This is the
    # exact path the non-Python task creators use (task-delegation.ts for
    # voice/chat/context-drop, conversation-server.ts for phone) so their
    # surfaces finally count. Runs the real module file as a script.
    with tempfile.TemporaryDirectory() as td:
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                received.append(json.loads(self.rfile.read(length)))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *_args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever)
        server_thread.start()
        try:
            env = {
                **os.environ,
                "POSTHOG_API_KEY": "phc_cli_test",
                "POSTHOG_HOST": f"http://127.0.0.1:{server.server_port}",
                "SUTANDO_STATE_DIR": td,
                "SUTANDO_SURFACE": "oss",
            }
            env.pop("DO_NOT_TRACK", None)
            env.pop("SUTANDO_TELEMETRY", None)
            proc = subprocess.run(
                [sys.executable, str(SRC / "telemetry.py"), "task_processed", "voice"],
                env=env,
                capture_output=True,
                text=True,
                timeout=5,
            )
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)

        assert proc.returncode == 0, proc.stderr
        assert len(received) == 1, \
            f"CLI must deliver task_processed before exit, got {len(received)}"
        assert received[0]["event"] == "task_processed"
        assert received[0]["properties"]["source"] == "voice"
        # A bad verb must fail loudly (exit 2).
        bad = subprocess.run(
            [sys.executable, str(SRC / "telemetry.py"), "bogus_verb"],
            env=env, capture_output=True, text=True, timeout=5,
        )
        assert bad.returncode == 2, f"usage error must exit 2, got {bad.returncode}"
        passed += 1
        print("ok   CLI entrypoint delivers source-tagged task_processed (voice)")

    # 9c) _cli_main dispatch — every branch in-process (the subprocess shim above
    # escapes the coverage gate; this covers the real logic).
    import unittest.mock as _um
    with tempfile.TemporaryDirectory() as td:
        mod = _load(Path(td), key="phc_cli_unit")
        with _um.patch.object(mod, "task_processed") as tp, \
                _um.patch.object(mod, "feature_used") as fu:
            assert mod._cli_main(["task_processed", "chat"]) == 0
            tp.assert_called_once_with("chat", flush=True)
            assert mod._cli_main(["feature_used", "morning_briefing"]) == 0
            fu.assert_called_once_with("morning_briefing", flush=True)
            # Unknown verb / wrong arity → exit 2, no emit.
            assert mod._cli_main(["bogus"]) == 2
            assert mod._cli_main(["task_processed"]) == 2
            assert mod._cli_main(["task_processed", "a", "b"]) == 2
            assert tp.call_count == 1 and fu.call_count == 1
        passed += 1
        print("ok   _cli_main dispatches task_processed/feature_used + rejects bad args")

    # 10) Per-install id PERSISTS across workspace churn — the id-persistence fix.
    #    Change the state dir between boots (as a desktop update/relaunch does)
    #    but keep the durable path: the id must NOT change (else every boot looks
    #    like a new user → the ~20-40x DAU inflation this fix removes).
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        durable = td / "appsupport" / "telemetry-id"
        m1 = _load(td / "ws1", key="phc_live", env={"SUTANDO_TELEMETRY_ID_FILE": str(durable)})
        id1 = m1._distinct_id()
        m2 = _load(td / "ws2-CHURNED", key="phc_live", env={"SUTANDO_TELEMETRY_ID_FILE": str(durable)})
        id2 = m2._distinct_id()
        assert id1 and id1 != "anonymous" and id2 == id1, f"id churned: {id1!r} != {id2!r}"
        passed += 1
        print("ok   distinct_id persists across workspace churn")

    # 11) Migrate an existing legacy <state>/telemetry-id into the durable path —
    #    installs whose id already persisted are adopted, NOT reset.
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        legacy_sd = td / "legacy"
        legacy_sd.mkdir(parents=True)
        (legacy_sd / "telemetry-id").write_text("legacy-stable-id-abc123")
        durable = td / "appsupport" / "telemetry-id"  # absent
        m = _load(legacy_sd, key="phc_live", env={"SUTANDO_TELEMETRY_ID_FILE": str(durable)})
        got = m._distinct_id()
        assert got == "legacy-stable-id-abc123", got
        assert durable.read_text().strip() == "legacy-stable-id-abc123"
        passed += 1
        print("ok   migrates a legacy id without reset")

    # 12) Durable location unwritable (its parent is a FILE) → still persists via
    #     the legacy path instead of churning; never "anonymous".
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        blocker = td / "blocker"
        blocker.write_text("i am a file, not a dir")
        durable = blocker / "telemetry-id"  # mkdir(parents) on `blocker` fails
        sd = td / "st"
        m = _load(sd, key="phc_live", env={"SUTANDO_TELEMETRY_ID_FILE": str(durable)})
        got = m._distinct_id()
        assert got and got != "anonymous", got
        assert (sd / "telemetry-id").read_text().strip() == got
        passed += 1
        print("ok   falls back to legacy path when durable unwritable")

    # 13) _durable_id_path branches: macOS, XDG, default, and explicit override.
    with tempfile.TemporaryDirectory() as td:
        m = _load(Path(td), key="phc_live")
        os.environ.pop("SUTANDO_TELEMETRY_ID_FILE", None)
        real, real_xdg = m.sys.platform, os.environ.get("XDG_DATA_HOME")
        try:
            m.sys.platform = "darwin"
            assert "Application Support/Sutando" in str(m._durable_id_path())
            m.sys.platform = "linux"
            os.environ["XDG_DATA_HOME"] = "/tmp/xdg-test"
            assert str(m._durable_id_path()) == "/tmp/xdg-test/sutando/telemetry-id"
            os.environ.pop("XDG_DATA_HOME")
            assert str(m._durable_id_path()).endswith(".local/share/sutando/telemetry-id")
        finally:
            m.sys.platform = real
            if real_xdg is not None:
                os.environ["XDG_DATA_HOME"] = real_xdg
        os.environ["SUTANDO_TELEMETRY_ID_FILE"] = "/tmp/override-id"
        assert str(m._durable_id_path()) == "/tmp/override-id"
        os.environ.pop("SUTANDO_TELEMETRY_ID_FILE")
        passed += 1
        print("ok   durable path — macOS / XDG / default / override branches")

    # 14) bandaid_generalize (#2147): the opt-out marker is honored at the
    #     DURABLE data dir too, not just <workspace>/state.
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        durable = td / "appsupport" / "telemetry-id"
        durable.parent.mkdir(parents=True)
        (durable.parent / "telemetry-disabled").write_text("")  # opt-out in the durable dir
        m = _load(td / "ws-any", key="phc_live", env={"SUTANDO_TELEMETRY_ID_FILE": str(durable)})
        assert m.opted_out() is True, "durable-dir telemetry-disabled must opt out"
        assert m.enabled() is False
        passed += 1
        print("ok   opt-out honored at the durable data dir (#2147 generalize)")

    # 15) BEFORE/AFTER: an opt-out survives workspace churn ONLY when it lives in
    #     the durable dir. BEFORE (marker in <workspace>/state) → a churn to a
    #     new state dir drops it and telemetry re-enables. AFTER (marker in the
    #     durable dir) → the same churn keeps opted_out True.
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        durable = td / "appsupport" / "telemetry-id"
        durable.parent.mkdir(parents=True)
        # BEFORE: opt-out written only to boot #1's legacy state dir.
        ws1 = td / "ws1"; ws1.mkdir()
        (ws1 / "telemetry-disabled").write_text("")
        before_boot1 = _load(ws1, key="phc_live", env={"SUTANDO_TELEMETRY_ID_FILE": str(durable)}).opted_out()
        before_boot2 = _load(td / "ws2-CHURNED", key="phc_live",
                             env={"SUTANDO_TELEMETRY_ID_FILE": str(durable)}).opted_out()
        # AFTER: opt-out written to the durable dir (survives the same churn).
        (durable.parent / "telemetry-disabled").write_text("")
        after = _load(td / "ws3-CHURNED-AGAIN", key="phc_live",
                      env={"SUTANDO_TELEMETRY_ID_FILE": str(durable)}).opted_out()
        assert before_boot1 is True and before_boot2 is False, \
            f"legacy-only opt-out should be lost on churn: boot1={before_boot1} boot2={before_boot2}"
        assert after is True, "durable opt-out must survive churn"
        passed += 1
        print("ok   BEFORE legacy opt-out lost on churn / AFTER durable opt-out survives (#2147)")

    print(f"\nALL PASS ({passed} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(run())
