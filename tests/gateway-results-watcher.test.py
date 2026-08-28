#!/usr/bin/env python3
"""kqueue results watcher: advisory doorbells, never delivery state.

The owner's proof list, each as an executed control: plain create; temp file
then os.replace; burst coalescing without a missed drain; files existing
before registration reach the first sweep; writes during watcher outage are
recovered by the bounded scan; directory recreation re-registers; shutdown
leaves no thread behind. Scan isolation: the scan period is set LONG in the
doorbell arms so a fast drain can only be the watcher's doing, and SHORT in
the outage arm so the floor is what recovers.
"""
from __future__ import annotations

import importlib.util
import os
import select
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

if not hasattr(select, "kqueue"):
    print("SKIP: kqueue unavailable on this platform (watcher inert by design)")
    sys.exit(0)

REPO = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="results-watcher-test-")
os.environ["SUTANDO_TEST_MODE"] = "1"
os.environ["SUTANDO_WORKSPACE"] = TMP
os.environ["REMOTE_TASK_URL"] = "http://127.0.0.1:1"
os.environ["REMOTE_TASK_TOKEN"] = "testtoken"
os.environ["REMOTE_OUTBOUND_SCAN_S"] = "30"      # doorbell arms: scan can't help

spec = importlib.util.spec_from_file_location(
    "rtc_watch", REPO / "src" / "remote-gateway-bridge.py")
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
        self.lock = threading.Lock()

    def __call__(self, *a, **kw):
        with self.lock:
            self.calls.append(time.monotonic())

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


drain = DrainRecorder()
rtc._post_ready_results = drain
rtc._post_proactive = lambda *a: None
RD = Path(rtc.RESULTS_DIR)
RD.mkdir(parents=True, exist_ok=True)

# ── 4. a file existing BEFORE registration reaches the first sweep ─────────
(RD / "pre-existing.txt").write_text("before the watch began")
watcher = rtc._start_results_watcher()
check(watcher is not None, "watcher starts on a kqueue platform")
worker = rtc._start_outbound_worker(set())
check(wait_for(lambda: drain.count() >= 1, 3.0),
      "registration wake: pre-existing file reaches the first full sweep")

# ── 1. plain create wakes the drain (scan is 30s — can't be the scan) ──────
base = drain.count()
t0 = time.monotonic()
(RD / "plain-create.txt").write_text("hello")
check(wait_for(lambda: drain.count() > base, 3.0),
      "plain create wakes a drain")
lat = drain.calls[base] - t0 if drain.count() > base else 99
check(lat < 2.0, f"create-to-drain {lat*1000:.0f}ms — doorbell, not the 30s scan")

# ── 2. temp file then os.replace (the real writers' pattern) ───────────────
base = drain.count()
tmpf = RD / ".staging.tmp"
tmpf.write_text("payload")
os.replace(tmpf, RD / "atomic-landed.txt")
check(wait_for(lambda: drain.count() > base, 3.0),
      "temp-then-os.replace wakes a drain")

# ── 3. burst: 20 writes coalesce, at least one drain, none missed ──────────
base = drain.count()
for i in range(20):
    (RD / f"burst-{i}.txt").write_text(str(i))
check(wait_for(lambda: drain.count() > base, 3.0),
      "a 20-write burst produces at least one drain")
check(watcher.is_alive(), "watcher survives the burst")

# ── 6. directory recreated → watcher re-registers and still wakes ──────────
shutil.rmtree(RD)
time.sleep(1.5)                       # let the vnode-gone event cycle
RD.mkdir(parents=True, exist_ok=True)
check(wait_for(lambda: watcher.is_alive(), 2.0),
      "watcher thread survives directory deletion")
base = drain.count()
deadline = time.monotonic() + 8.0     # re-register includes a fresh sweep wake
(RD / "after-recreate.txt").write_text("back")
while time.monotonic() < deadline and drain.count() <= base:
    (RD / "after-recreate.txt").write_text(str(time.monotonic()))
    time.sleep(0.3)
check(drain.count() > base,
      "after dir recreation the watcher re-registers and wakes again")

# ── 5. watcher outage: bounded scan recovers the write (floor) ─────────────
rtc.OUTBOUND_SCAN_S = 0.5
rtc._OUTBOUND_STOP.set()              # floor arm: kill both, restart worker only
rtc.wake_outbound()
watcher.join(timeout=5)
worker.join(timeout=5)
check(not watcher.is_alive() and not worker.is_alive(),
      "7. shutdown: stop+wake joins watcher and worker, no threads left")
rtc._OUTBOUND_STOP.clear()
worker2 = rtc._start_outbound_worker(set())   # deliberately NO watcher
base = drain.count()
(RD / "during-outage.txt").write_text("scan must find me")
check(wait_for(lambda: drain.count() > base, 3.0),
      "watcher absent: the bounded scan still drains (correctness floor)")
rtc._OUTBOUND_STOP.set()
rtc.wake_outbound()
worker2.join(timeout=5)

# ── 8. partial-capability runtime (Xcode 3.9): kqueue WITHOUT os.O_EVTONLY —
#      the doorbell must ride the O_RDONLY fallback, not degrade to scan ─────
rtc.OUTBOUND_SCAN_S = 30.0
rtc._OUTBOUND_STOP.clear()
_had_evtonly = hasattr(os, "O_EVTONLY")
_saved_evtonly = getattr(os, "O_EVTONLY", None)
if _had_evtonly:
    del os.O_EVTONLY
try:
    check(not hasattr(os, "O_EVTONLY"), "control precondition: O_EVTONLY absent")
    watcher3 = rtc._start_results_watcher()
    check(watcher3 is not None, "watcher starts without O_EVTONLY")
    worker3 = rtc._start_outbound_worker(set())
    base = drain.count()
    t0 = time.monotonic()
    (RD / "no-evtonly.txt").write_text("fallback fd")
    check(wait_for(lambda: drain.count() > base, 3.0),
          "kqueue-without-O_EVTONLY: create still wakes a drain")
    lat = drain.calls[base] - t0 if drain.count() > base else 99
    check(lat < 2.0, f"fallback doorbell latency {lat*1000:.0f}ms — not the 30s scan")
finally:
    if _had_evtonly:
        os.O_EVTONLY = _saved_evtonly
rtc._OUTBOUND_STOP.set()
rtc.wake_outbound()
watcher3.join(timeout=5)
worker3.join(timeout=5)
check(not watcher3.is_alive(), "partial-capability watcher shuts down cleanly")

print(f"\n{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
