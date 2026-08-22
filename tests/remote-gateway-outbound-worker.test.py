#!/usr/bin/env python3
"""Outbound worker: decouple outbound progress from inbound long-poll progress.

Scheduling-layer contract ONLY — delivery machinery (DeliveryCore claims,
three-state outcomes) is covered by src/remote-gateway-bridge.test.py. Drains
are patched at the module seam so each control isolates one scheduling
behavior: periodic drain with no inbound loop, wake-on-kick immediacy,
per-cycle failure isolation, graceful stop.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="outbound-worker-test-")
os.environ["SUTANDO_TEST_MODE"] = "1"
os.environ["SUTANDO_WORKSPACE"] = TMP
os.environ["REMOTE_TASK_URL"] = "http://127.0.0.1:1"     # never contacted
os.environ["REMOTE_TASK_TOKEN"] = "testtoken"
os.environ["REMOTE_OUTBOUND_SCAN_S"] = "0.2"

spec = importlib.util.spec_from_file_location(
    "rtc_worker", REPO / "src" / "remote-gateway-bridge.py")
rtc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rtc)

failures: list[str] = []


def check(cond, label):
    print(("ok: " if cond else "FAIL: ") + label)
    if not cond:
        failures.append(label)


class DrainRecorder:
    def __init__(self):
        self.calls: list[float] = []
        self.raise_next = 0
        self.lock = threading.Lock()

    def __call__(self, *a, **kw):
        with self.lock:
            self.calls.append(time.monotonic())
            if self.raise_next > 0:
                self.raise_next -= 1
                raise RuntimeError("injected drain failure")

    def count(self):
        with self.lock:
            return len(self.calls)


def wait_for(pred, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


results_drain = DrainRecorder()
proactive_drain = DrainRecorder()
rtc._post_ready_results = results_drain
rtc._post_proactive = proactive_drain

# ── 1. periodic drain with NO inbound loop running ─────────────────────────
inflight: set = set()
t = rtc._start_outbound_worker(inflight)
check(wait_for(lambda: results_drain.count() >= 2, 3.0),
      "worker drains repeatedly with no inbound poll loop at all")
check(proactive_drain.count() >= 1, "proactive drain rides the same worker")

# ── 2. wake_outbound() beats the scan period ───────────────────────────────
rtc.OUTBOUND_SCAN_S = 30.0
wait_for(lambda: False, 0.5)          # let the worker park on the long wait
base = results_drain.count()
kick_at = time.monotonic()
rtc.wake_outbound()
check(wait_for(lambda: results_drain.count() > base, 2.0),
      "wake_outbound() triggers a drain while parked on a 30s scan")
if results_drain.count() > base:
    latency = results_drain.calls[base] - kick_at
    check(latency < 1.0, f"wake-to-drain latency {latency*1000:.0f}ms < 1s")

# ── 3. failure isolation: a raising drain never kills the worker ───────────
results_drain.raise_next = 2
base = results_drain.count()
rtc.wake_outbound()
wait_for(lambda: results_drain.count() > base, 2.0)
base2 = results_drain.count()
rtc.wake_outbound()
check(wait_for(lambda: results_drain.count() > base2, 2.0),
      "worker survives raising drains (per-cycle isolation)")
check(t.is_alive(), "worker thread alive after injected failures")

# ── 4. graceful stop: stop + wake joins promptly, no further drains ────────
rtc._OUTBOUND_STOP.set()
rtc.wake_outbound()
t.join(timeout=3.0)
check(not t.is_alive(), "worker joins promptly on stop+wake")
final = results_drain.count()
time.sleep(0.5)
check(results_drain.count() == final, "no drains after stop (clean shutdown)")

print(f"\n{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
