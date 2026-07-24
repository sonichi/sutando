"""Wiring tests for the AWP event channel inside remote_gateway_bridge —
`_maybe_start_event_channel` (opt-in guard + start) and the per-channel health
block in `_emit_gateway_status`. The point is the ISOLATION contract: off by
default, and starting it never touches the task loop. No real threads or network
(threading.Thread is stubbed to a no-op), no real gateway. Exit 0/1."""
import json
import os
import tempfile
import importlib
import sys
import pathlib

FAILS: list = []


def check(cond, msg):
    print(("  ok  " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def _load(tmp):
    os.environ["AGENT_CONNECT_STATE_DIR"] = str(tmp)
    os.environ.setdefault("REMOTE_TASK_URL", "https://gw.example/relay")
    os.environ.setdefault("REMOTE_TASK_TOKEN", "dummy-secret")
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    mod = importlib.import_module("ag2_sparrow.remote_gateway_bridge")
    return importlib.reload(mod)


class _NoThread:
    """Stub: records that a thread WOULD start, but never runs the target — so
    no SSE connect + no drain loop fire during the test."""
    started = []

    def __init__(self, target=None, name=None, daemon=None, **kw):
        self._name = name

    def start(self):
        _NoThread.started.append(self._name)


def test_off_by_default():
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        os.environ.pop("SPARROW_EVENTS", None)
        m._EVENT_CHANNEL = None
        m._maybe_start_event_channel()
        check(m._EVENT_CHANNEL is None,
              "wiring: SPARROW_EVENTS unset → event channel NOT started (off by default)")


def test_opt_in_starts_isolated_threads():
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        os.environ["SPARROW_EVENTS"] = "1"
        _NoThread.started = []
        orig_thread = m.threading.Thread
        m.threading.Thread = _NoThread
        m._EVENT_CHANNEL = None
        try:
            m._maybe_start_event_channel()
        finally:
            m.threading.Thread = orig_thread
            os.environ.pop("SPARROW_EVENTS", None)
        check(m._EVENT_CHANNEL is not None,
              "wiring: SPARROW_EVENTS on → event channel created")
        check("sparrow-event-channel" in _NoThread.started
              and "sparrow-event-drain" in _NoThread.started,
              "wiring: both the SSE channel AND the drain loop start in their own daemon threads")


def test_start_failure_is_swallowed():
    # A broken EventChannel must NOT propagate — task delivery is unaffected.
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        os.environ["SPARROW_EVENTS"] = "1"
        import ag2_sparrow.event_channel as ec
        orig = ec.EventChannel
        ec.EventChannel = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        m._EVENT_CHANNEL = None
        raised = False
        try:
            m._maybe_start_event_channel()
        except Exception:  # noqa: BLE001
            raised = True
        finally:
            ec.EventChannel = orig
            os.environ.pop("SPARROW_EVENTS", None)
        check(not raised, "wiring: a start failure is swallowed (task loop never sees it)")
        check(m._EVENT_CHANNEL is None, "wiring: failed start leaves channel unset")


def test_drain_loop_isolates_errors():
    # The P1 drain loop must run consumer.drain() and SWALLOW any error (task
    # delivery unaffected). Capture the loop's target without starting a thread,
    # force drain() to raise, and break the loop via a sentinel on time.sleep.
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        os.environ["SPARROW_EVENTS"] = "1"
        captured = {}

        class _Capture:
            def __init__(self, target=None, name=None, daemon=None, **kw):
                captured[name] = target
            def start(self):
                pass
        import ag2_sparrow.event_consumer as ecmod
        orig_thread = m.threading.Thread
        orig_drain = ecmod.EventConsumer.drain
        orig_sleep = m.time.sleep
        m.threading.Thread = _Capture
        m._EVENT_CHANNEL = None
        try:
            m._maybe_start_event_channel()
            drain_loop = captured.get("sparrow-event-drain")
            check(callable(drain_loop), "wiring: the drain loop target is registered")
            ecmod.EventConsumer.drain = lambda self: (_ for _ in ()).throw(RuntimeError("drain boom"))
            m.time.sleep = lambda s: (_ for _ in ()).throw(KeyboardInterrupt)  # break while-True
            escaped = None
            try:
                drain_loop()          # one iteration: drain raises → swallowed → sleep breaks
            except KeyboardInterrupt:
                escaped = False       # broke out cleanly via the sentinel
            except Exception as e:    # noqa: BLE001
                escaped = e           # a drain error leaked out — isolation broken
            check(escaped is False,
                  "wiring: drain loop swallows a drain() error and keeps looping (isolated)")
        finally:
            m.threading.Thread = orig_thread
            ecmod.EventConsumer.drain = orig_drain
            m.time.sleep = orig_sleep
            os.environ.pop("SPARROW_EVENTS", None)


def test_gateway_status_reports_per_channel_health():
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))

        class _FakeCh:
            health = {"status": "connected", "last_cursor": 12,
                      "last_event_at": 99, "retry_count": 0, "error": None}
        m._EVENT_CHANNEL = _FakeCh()
        try:
            m._emit_gateway_status(True)
            rec = json.loads(m.GATEWAY_STATUS_FILE.read_text())
        finally:
            m._EVENT_CHANNEL = None
        check(rec.get("channels", {}).get("events") == "connected"
              and rec["channels"]["tasks"] == "connected",
              "wiring: gateway-status carries per-channel {tasks, events} health")
        check(rec.get("events", {}).get("last_cursor") == 12,
              "wiring: gateway-status carries the event channel's cursor/retry detail")


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"# {name}")
            fn()
    if FAILS:
        print(f"\nFAILED ({len(FAILS)})")
        return 1
    print("\nPASS — event channel wiring (isolation contract)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
