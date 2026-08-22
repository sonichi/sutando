#!/usr/bin/env python3
"""#3279 layer 3: the running bridge self-reports its identity and the
health probe compares it to the checkout. Pins: loader pre-exec injection
survives the exec; the status payload carries the runtime block; the two
engine counters increment at their real send sites; probe verdicts for
fresh / drifted / pre-report sidecars."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
failures: list[str] = []


def check(cond, label):
    print(("ok: " if cond else "FAIL: ") + label)
    if not cond:
        failures.append(label)


os.environ.setdefault("SUTANDO_TEST_MODE", "1")
os.environ["REMOTE_TASK_URL"] = "http://127.0.0.1:1"
os.environ["REMOTE_TASK_TOKEN"] = "t"

# ── loader injection survives the exec; sha matches the real checkout ──────
spec = importlib.util.spec_from_file_location(
    "rgb_loader", REPO / "src" / "remote-gateway-bridge.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
head = subprocess.check_output(["git", "-C", str(REPO), "rev-parse", "HEAD"],
                               text=True).strip()
check(m.RUNTIME_IDENTITY.get("build_sha") == head,
      "loader injects the checkout HEAD as build_sha (pre-exec, exec-proof)")
check(str(m.RUNTIME_IDENTITY.get("entrypoint", "")).endswith(
    "src/remote-gateway-bridge.py"), "entrypoint names the canonical loader")

# ── the status payload carries the runtime block ───────────────────────────
with tempfile.TemporaryDirectory() as td:
    m.GATEWAY_STATUS_FILE = Path(td) / "gateway-status.json"
    m._emit_gateway_status(True)
    rt = json.loads(m.GATEWAY_STATUS_FILE.read_text())["runtime"]
    check(rt["build_sha"] == head and "engine" in rt
          and rt["core_confirmed"] == 0 and rt["legacy_sends"] == 0,
          "status sidecar carries {build_sha, engine, both counters}")

    # ── counters increment at the real sites and re-emit ───────────────────
    m._ENGINE_COUNTS["core_confirmed"] += 0  # anchor: the dict is the API
    class _Res:
        pass
    # Drive _deliver_result_payload's confirmed branch through a stub core.
    class _StubBackend:
        # Must equal the singleton's keyed root or _delivery_core() rebuilds
        # a REAL core over the stub (the exact keying the prod code uses).
        root = m.RESULTS_DIR / f".outbox{m._INST_SUFFIX}"
        def publish(self, *a): return True
        def attempts(self, *a): return 0
    class _StubCore:
        backend = _StubBackend()
        provider = object()
        worker = "w"
        def deliver_one(self, *a, **k):
            r = _Res()
            r.status = m.DrainStatus.ATTEMPTED
            r.outcome = m.CoreDeliveryOutcome.CONFIRMED
            return r
    m._DELIVERY_CORE = _StubCore()
    ok = m._deliver_result_payload("tid-1", "tid-1", "body")
    check(ok and m._ENGINE_COUNTS["core_confirmed"] == 1,
          "a CONFIRMED DeliveryCore result increments core_confirmed")
    m._emit_gateway_status(True)
    rt = json.loads(m.GATEWAY_STATUS_FILE.read_text())["runtime"]
    check(rt["core_confirmed"] == 1, "the incremented counter reaches the sidecar")
    check("StubBackend" in rt["engine"],
          "engine string names the LIVE backend/provider pair")

# ── probe verdicts ─────────────────────────────────────────────────────────
hspec = importlib.util.spec_from_file_location("hc", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(hspec)
try:
    hspec.loader.exec_module(hc)
except SystemExit:
    pass
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "gateway-status.json"
    r = hc.check_runtime_identity(path=p, head_sha="a" * 40)
    check(r["status"] == "ok" and "nothing to verify" in r["detail"],
          "no sidecar: probe idles rather than inventing a verdict")
    p.write_text(json.dumps({"connected": True}))
    r = hc.check_runtime_identity(path=p, head_sha="a" * 40)
    check(r["status"] == "warn" and "predates" in r["detail"],
          "sidecar without runtime block: warn names the restart remedy")
    p.write_text(json.dumps({"runtime": {"build_sha": "b" * 40,
                                         "entrypoint": "x/src/remote-gateway-bridge.py",
                                         "engine": "E", "core_confirmed": 3,
                                         "legacy_sends": 1}}))
    r = hc.check_runtime_identity(path=p, head_sha="a" * 40)
    check(r["status"] == "warn" and "build drift" in r["detail"],
          "sha mismatch: warn says drift and both shas")
    p.write_text(json.dumps({"runtime": {"build_sha": "a" * 40,
                                         "entrypoint": "x/src/remote-gateway-bridge.py",
                                         "engine": "E", "core_confirmed": 3,
                                         "legacy_sends": 1}}))
    r = hc.check_runtime_identity(path=p, head_sha="a" * 40)
    check(r["status"] == "ok" and "legacy_sends=1" in r["detail"],
          "matching sha: ok, and the legacy counter is surfaced")

print(f"\n{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
