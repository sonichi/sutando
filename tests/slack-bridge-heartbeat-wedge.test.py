#!/usr/bin/env python3
"""slack-bridge wedge-detection: the heartbeat must be gated on the LIVE Socket
Mode connection so it goes stale during a wedge (alive-but-deaf), which is what
lets health-check's existing heartbeat-staleness check (Check 3) see it.

Does NOT import the bridge (slack_bolt is a CI-absent dep + the module has
import-time side effects) — mirrors the other slack-bridge tests: source-
structure assertions, plus a behavioral test that exec's the real
`_socket_connected` source with its production filename and line numbers
against fake handlers. Run: python3 tests/slack-bridge-heartbeat-wedge.test.py
"""
import re
import types
from pathlib import Path
import os
import tempfile

# Hermetic isolation before any bridge source is exec'd: CLAUDE_CONFIG_DIR points
# at a seeded temp dir so config resolution never reads the operator's ~/.claude.
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-token-wedge")
os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test-token-wedge")
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="wedge-test-ccd-")
_ccd_cfg = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "slack"
_ccd_cfg.mkdir(parents=True, exist_ok=True)
(_ccd_cfg / "access.json").write_text('{"allowFrom": []}')
# -----------------------------------------------------------------------------

SRC_PATH = Path(__file__).resolve().parent.parent / "src" / "slack-bridge.py"
SRC = SRC_PATH.read_text()
passed = []


def check(name, cond, detail=""):
    assert cond, f"FAIL {name}: {detail}"
    passed.append(name)


# --- structure: the three load-bearing properties of the fix ---

# 1. The heartbeat write is GATED on _socket_healthy(): an unconditional write
#    stays fresh through a wedge, a connection-only gate through reconnect churn.
check("heartbeat-gated-on-health",
      re.search(r"if\s+now\s*-\s*last_heartbeat\s*>=\s*60\s+and\s+_socket_healthy\(\)\s*:", SRC) is not None,
      "heartbeat write must be guarded by `and _socket_healthy()`")

# 1b. _socket_healthy() requires BOTH the live connection and no churn.
check("healthy-requires-connected-and-no-churn",
      re.search(r"def _socket_healthy\(\)[\s\S]+?_socket_connected\(\)\s+and\s+not\s+_reconnect_churning\(\)", SRC) is not None,
      "_socket_healthy() must be `_socket_connected() and not _reconnect_churning()`")

# 1c. session id is sampled every result_watcher tick, not at heartbeat instants,
#     so churn faster than the 60s cadence is still observed.
check("session-sampled-every-tick",
      re.search(r"_note_session_sample\(\)\s*\n\s*now = time\.time\(\)", SRC) is not None,
      "result_watcher must call _note_session_sample() each tick before the heartbeat check")

# 2. _socket_connected() consults the real socket client's is_connected().
check("socket-connected-checks-is_connected",
      re.search(r"def _socket_connected\(\)", SRC) is not None and "is_connected()" in SRC,
      "_socket_connected() must call the client's is_connected()")

# 3. The handler is wired to the module ref BEFORE handler.start() so the
#    heartbeat thread (started earlier) can read live state.
wire = SRC.find("_socket_handler = handler")
# rfind: the real `handler.start()` call is the LAST occurrence — an earlier
# mention lives in a code comment.
start = SRC.rfind("handler.start()")
check("handler-wired-before-start",
      wire != -1 and start != -1 and wire < start,
      "_socket_handler must be set before handler.start()")

# Exec the real _socket_connected source standalone: exercises production code
# without slack_bolt, and keeps coverage attributed to the real file.
m = re.search(r"\ndef _socket_connected\(\)[\s\S]+?\n(?=\S)", SRC)
assert m, "could not locate _socket_connected source"
fn_src = "\n" * SRC[:m.start()].count("\n") + m.group(0)

def run_socket_connected(handler):
    ns = {"_socket_handler": handler}
    exec(compile(fn_src, str(SRC_PATH), "exec"), ns)
    return ns["_socket_connected"]()

class _Client:
    def __init__(self, connected):
        self._c = connected
    def is_connected(self):
        return self._c

# connected socket -> heartbeat allowed
check("connected-true", run_socket_connected(types.SimpleNamespace(client=_Client(True))) is True)
# wedged/disconnected socket -> heartbeat suppressed (goes stale -> detectable)
check("disconnected-false", run_socket_connected(types.SimpleNamespace(client=_Client(False))) is False)
# handler not wired yet (early boot) -> False, no crash
check("no-handler-false", run_socket_connected(None) is False)
# handler present but client missing -> False, no crash
check("no-client-false", run_socket_connected(types.SimpleNamespace(client=None)) is False)
# is_connected raising -> caught, False (never crash the heartbeat thread)
class _Boom:
    def is_connected(self):
        raise RuntimeError("socket state unavailable")
check("is_connected-raises-false", run_socket_connected(types.SimpleNamespace(client=_Boom())) is False)

# The P1 case: is_connected() stays True while Socket Mode thrashes sessions.
# Drives the real churn functions with a fake clock and an always-True client.

def _extract(name):
    m2 = re.search(rf"\ndef {name}\([\s\S]+?\n(?=\S)", SRC)
    assert m2, f"could not locate {name} source"
    return "\n" * SRC[:m2.start()].count("\n") + m2.group(0)

def make_churn_ns(handler):
    """Fresh namespace holding the real churn code + controllable state."""
    import collections
    ns = {
        "time": types.SimpleNamespace(time=lambda: 0.0),  # tests always pass now=
        "deque": collections.deque,
        "_socket_handler": handler,
        "_CHURN_WINDOW_S": 300,
        "_CHURN_MAX_SESSIONS": 3,
        "_session_changes": collections.deque(),
        "_last_session_id": None,
        "_churn_logged": False,
        "print": lambda *a, **k: None,  # churn-transition log lines, silenced
    }
    for fn in ("_socket_connected", "_note_session_sample",
               "_reconnect_churning", "_socket_healthy"):
        exec(compile(_extract(fn), str(SRC_PATH), "exec"), ns)
    return ns

class _ChurningClient:
    """is_connected() ALWAYS True; session_id changes per reconnect — the
    exact live-repro shape (~7 sessions/min, is_connected truthy)."""
    def __init__(self):
        self._sid = "s-0"
    def reconnect(self, n):
        self._sid = f"s-{n}"
    def is_connected(self):
        return True
    def session_id(self):
        return self._sid

client = _ChurningClient()
ns = make_churn_ns(types.SimpleNamespace(client=client))

# Baseline: first observed session id is not churn; healthy gate passes.
ns["_note_session_sample"](now=0)
check("stable-session-healthy",
      ns["_socket_healthy"]() is True,
      "a stable session with is_connected()=True must remain healthy")

# Two id changes inside the window: still below threshold -> healthy.
for i, t in [(1, 10), (2, 20)]:
    client.reconnect(i)
    ns["_note_session_sample"](now=t)
check("below-threshold-still-healthy",
      ns["_reconnect_churning"](now=25) is False and ns["_socket_healthy"]() is True,
      "threshold-1 session changes must not trip the churn gate")

# Third change inside the window: churn threshold reached while
# is_connected() is STILL True -> unhealthy, heartbeat suppressed.
client.reconnect(3)
ns["_note_session_sample"](now=30)
check("churn-with-truthy-is_connected-unhealthy",
      client.is_connected() is True and ns["_socket_healthy"]() is False,
      "3 session changes in the window with is_connected()=True must suppress the heartbeat")

# Sustained churn at the repro rate (~7/min) stays unhealthy.
for i in range(4, 20):
    client.reconnect(i)
    ns["_note_session_sample"](now=30 + i * 9)
check("sustained-churn-stays-unhealthy",
      ns["_socket_healthy"]() is False,
      "sustained reconnect churn must keep the heartbeat suppressed")

# Recovery: churn stops, the window drains, the gate returns to healthy.
last_change = 30 + 19 * 9  # ts of the final reconnect above
check("churn-subsides-recovers",
      ns["_reconnect_churning"](now=last_change + 301) is False and ns["_socket_healthy"]() is True,
      "after a quiet window the gate must recover to healthy")

# Baseline-not-churn: a fresh boot's first id must never count toward churn.
ns2 = make_churn_ns(types.SimpleNamespace(client=_ChurningClient()))
ns2["_note_session_sample"](now=0)
check("first-session-is-baseline",
      len(ns2["_session_changes"]) == 0,
      "the first observed session id is baseline, not a churn event")

# None session ids (between sessions / handler unwired) are skipped, and a
# session_id() that raises must never crash the watcher thread.
class _NoneThenBoom:
    def is_connected(self):
        return True
    def session_id(self):
        return None
ns3 = make_churn_ns(types.SimpleNamespace(client=_NoneThenBoom()))
ns3["_note_session_sample"](now=0)
class _Boom2:
    def is_connected(self):
        return True
    def session_id(self):
        raise RuntimeError("no session state")
ns3["_socket_handler"] = types.SimpleNamespace(client=_Boom2())
ns3["_note_session_sample"](now=1)
check("none-and-raising-session-ids-safe",
      len(ns3["_session_changes"]) == 0 and ns3["_last_session_id"] is None,
      "None/raising session_id() must be skipped, not counted or crashing")

# Slack's routine session refresh (~1 per 10-30 min) must NOT read as churn:
# id changes spaced wider than the window never accumulate to threshold.
client4 = _ChurningClient()
ns4 = make_churn_ns(types.SimpleNamespace(client=client4))
ns4["_note_session_sample"](now=0)
for i, t in [(1, 600), (2, 1200), (3, 1800), (4, 2400)]:
    client4.reconnect(i)
    ns4["_note_session_sample"](now=t)
    check(f"routine-refresh-{i}-healthy",
          ns4["_reconnect_churning"](now=t) is False,
          "routine ~10-min session refreshes must never trip the churn gate")

# Real-module half: imports the bridge with slack_bolt stubbed and drives the
# actual result_watcher through both phases, which the exec tests cannot reach.
import os
import sys
import tempfile
import threading
import time as _time


class _StubApp:
    def __init__(self, *a, **kw):
        self.client = types.SimpleNamespace()

    def event(self, _name):
        def decorator(fn):
            return fn
        return decorator


def _load_bridge():
    os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-token-wedge")
    os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test-token-wedge")
    try:
        import slack_bolt as _real_bolt
        _real_bolt.App = _StubApp
    except ImportError:
        stub_bolt = types.ModuleType("slack_bolt")
        stub_bolt.App = _StubApp
        sys.modules["slack_bolt"] = stub_bolt
        adapter_pkg = types.ModuleType("slack_bolt.adapter")
        sys.modules["slack_bolt.adapter"] = adapter_pkg
        sm_mod = types.ModuleType("slack_bolt.adapter.socket_mode")
        sm_mod.SocketModeHandler = object
        sys.modules["slack_bolt.adapter.socket_mode"] = sm_mod
    import importlib.util
    repo = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("slack_bridge_wedge_test", repo / "src" / "slack-bridge.py")
    sys.path.insert(0, str(repo / "src"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


try:
    _mod = _load_bridge()
except Exception as _e:  # slack_sdk absent etc. — exec-based coverage above stands
    print(f"note: real-module section skipped ({_e})")
    _mod = None

if _mod is not None:
    class _LiveChurnClient:
        def __init__(self):
            self._sid = "live-0"
        def is_connected(self):
            return True
        def session_id(self):
            return self._sid

    lc = _LiveChurnClient()
    _mod._socket_handler = types.SimpleNamespace(client=lc)

    # Real module functions, quick sanity: baseline, churn, recovery.
    _mod._note_session_sample(now=0)
    check("realmod-baseline-healthy", _mod._socket_healthy() is True)
    for i, t in [(1, 1), (2, 2), (3, 3)]:
        lc._sid = f"live-{i}"
        _mod._note_session_sample(now=_time.time())
    check("realmod-churn-unhealthy",
          lc.is_connected() is True and _mod._socket_healthy() is False,
          "real module must suppress the heartbeat under churn with a truthy is_connected()")
    _mod._session_changes.clear()
    check("realmod-recovery-healthy", _mod._socket_healthy() is True)

    # Loop-level: run the REAL result_watcher thread.
    for d in (_mod.RESULTS_DIR, _mod.ARCHIVE_RESULTS_DIR, _mod.STATE_DIR,
              _mod.REPO / "tasks"):
        Path(d).mkdir(parents=True, exist_ok=True)
    hb = Path(_mod.REPO) / "state" / "slack-bridge.heartbeat"
    hb.unlink(missing_ok=True)

    # Seed a pending reply whose result carries a skip marker so the loop's delivery
    # branch runs alongside the heartbeat phases.
    with _mod.pending_replies_lock:
        _mod.pending_replies["task-wedge-test"] = {"channel": "C000", "access_tier": "owner"}
    (Path(_mod.RESULTS_DIR) / "task-wedge-test.txt").write_text("[no-send]\ninternal\n")

    # Phase A: churn active (3 fresh changes) — the loop must NOT write.
    now = _time.time()
    _mod._session_changes.clear()
    _mod._session_changes.extend([now - 10, now - 5, now - 1])
    threading.Thread(target=_mod.result_watcher, daemon=True).start()
    _time.sleep(2.5)
    check("watcher-suppresses-heartbeat-under-churn",
          not hb.exists(),
          "result_watcher must not write the heartbeat while churn is active")

    # Phase B: churn drains — the loop must resume writing within ~2 ticks.
    _mod._session_changes.clear()
    deadline = _time.time() + 6
    while _time.time() < deadline and not hb.exists():
        _time.sleep(0.25)
    check("watcher-resumes-heartbeat-after-churn",
          hb.exists(),
          "result_watcher must resume the heartbeat once churn subsides")

# --- health-check already has the consuming half (Check 3) — assert it exists,
#     so this fix and that detector stay coupled. ---
HC = (Path(__file__).resolve().parent.parent / "src" / "health-check.py").read_text()
check("health-check-has-heartbeat-staleness-check",
      "heartbeat stale" in HC and re.search(r"\.heartbeat\"?\s*\n?", HC) is not None,
      "health-check must retain its heartbeat-staleness detection (Check 3)")

print(f"OK — {len(passed)} checks passed: {', '.join(passed)}")
