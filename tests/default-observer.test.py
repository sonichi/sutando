"""Tests for the 👀 observed-receipt (default_observer) and its bridge wiring.
No real network (urlopen monkeypatched). Exit 0/1."""
import importlib
import os
import pathlib
import sys
import tempfile
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]
                       / "packages" / "ag2-sparrow"))

from ag2_sparrow.default_observer import (  # noqa: E402
    OBSERVE_REACTION, ReactObserverHandler, _SEEN_CAP,
)

FAILS: list = []


def check(cond, msg):
    print(("  ok  " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


class _Inner:
    """Recording inner handler with the full consumer contract."""

    def __init__(self):
        self.offered = []
        self.last_path = "/tmp/inner-last"

    def offer(self, event):
        self.offered.append(event)
        return ["settled-" + str(event.get("event_id"))]

    def has_pending(self):
        return True


class _Net:
    """Captures react POSTs; optionally raises."""

    def __init__(self, raise_exc=None):
        self.calls = []
        self.raise_exc = raise_exc

    def __call__(self, req, timeout=None):
        self.calls.append(req)
        if self.raise_exc:
            raise self.raise_exc

        class _R:
            def read(self):
                return b""

            def close(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return _R()


def _msg(eid="e1", actor="@alice:hs", room="!r:hs", mid="m1", etype="message.created"):
    return {"event_id": eid, "type": etype, "actor_id": actor,
            "room_id": room, "content": {"message_id": mid}}


_BRIDGE = None


def _react_sender():
    """The REAL sender from the allowlisted adapter edge, so the URL-shape
    assertions below stay on the code that builds the URL."""
    global _BRIDGE
    if _BRIDGE is None:
        os.environ["AGENT_CONNECT_STATE_DIR"] = tempfile.mkdtemp()
        os.environ["REMOTE_TASK_URL"] = "https://gw.example/relay"
        os.environ["REMOTE_TASK_TOKEN"] = "dummy-secret"
        _BRIDGE = importlib.import_module("ag2_sparrow.remote_gateway_bridge")
    return _BRIDGE._react_sender()


def _handler(net, inner=None, mxid="@me:hs"):
    h = ReactObserverHandler(inner or _Inner(), _react_sender(), mxid,
                             log=lambda *_: None)
    urllib.request.urlopen, orig = net, urllib.request.urlopen
    return h, orig


def test_reacts_and_stays_transparent():
    net = _Net()
    inner = _Inner()
    h, orig = _handler(net, inner)
    try:
        out = h.offer(_msg())
        check(h.flush(5), "queued receipt delivered")
    finally:
        urllib.request.urlopen = orig
    check(out == ["settled-e1"], "offer() returns inner result unchanged")
    check(inner.offered == [_msg()], "inner handler saw the event")
    check(len(net.calls) == 1, "one react POST fired")
    req = net.calls[0]
    check("/v1/rooms/" in req.full_url and req.full_url.endswith("/react"),
          "react POST targets /v1/rooms/<room>/react")
    import json as _json
    body = _json.loads(req.data.decode())
    check(body == {"event_id": "m1", "key": OBSERVE_REACTION},
          "react body carries content.message_id + \U0001F440 key")
    check(req.get_header("User-agent") is not None, "explicit UA set (edge rejects default)")
    check(h.last_path == "/tmp/inner-last" and h.has_pending() is True,
          "last_path/has_pending pass through to inner")


def test_skips_self_noise_and_dups():
    net = _Net()
    inner = _Inner()
    h, orig = _handler(net, inner)
    try:
        h.offer(_msg(eid="s1", actor="@me:hs"))                 # self-echo
        h.offer(_msg(eid="s2", etype="reaction.added"))         # not a message
        h.offer({"event_id": "s3", "type": "message.created",
                 "actor_id": "@alice:hs", "room_id": "!r:hs"})  # no message_id
        h.offer(_msg(eid="s4", mid="dup"))
        h.offer(_msg(eid="s5", mid="dup"))                      # redelivery
        check(h.flush(5), "queued receipts delivered")
    finally:
        urllib.request.urlopen = orig
    check(len(net.calls) == 1, "self/non-message/missing-id/dup all skipped (1 react)")
    check(len(inner.offered) == 5, "every event still delegated to inner")


def test_no_mxid_never_reacts():
    net = _Net()
    h, orig = _handler(net, mxid=None)
    try:
        h.offer(_msg())
    finally:
        urllib.request.urlopen = orig
    check(net.calls == [], "without agent mxid the receipt stays off")


def test_failures_never_break_the_chain():
    for exc, label in [
        (urllib.error.HTTPError("u", 502, "dup", {}, None), "HTTP 502 (dup react)"),
        (urllib.error.URLError("down"), "network error"),
        (RuntimeError("boom"), "unexpected error"),
    ]:
        net = _Net(raise_exc=exc)
        inner = _Inner()
        h, orig = _handler(net, inner)
        try:
            out = h.offer(_msg())
            h.flush(5)
        finally:
            urllib.request.urlopen = orig
        check(out == ["settled-e1"] and len(inner.offered) == 1,
              f"{label} swallowed — inner still ran, result intact")


def test_seen_set_is_bounded():
    net = _Net()
    h, orig = _handler(net)
    try:
        for i in range(_SEEN_CAP + 10):
            h.offer(_msg(eid=f"e{i}", mid=f"m{i}"))
        h.flush(10)
    finally:
        urllib.request.urlopen = orig
    check(len(h._seen) <= _SEEN_CAP and len(h._order) <= _SEEN_CAP,
          "seen-set FIFO-bounded at cap")


def test_room_id_fully_escaped():
    net = _Net()
    h, orig = _handler(net)
    try:
        h.offer(_msg(room="!a/b:hs"))
        check(h.flush(5), "receipt for slash-room delivered")
    finally:
        urllib.request.urlopen = orig
    check(len(net.calls) == 1, "one react POST for slash-room id")
    url = net.calls[0].full_url
    check("%21a%2Fb%3Ahs" in url and "/%21a/b" not in url,
          "room id with / fully escaped (safe='') — path not split")


def test_slow_reaction_never_delays_inner():
    import time as _time

    class _SlowNet(_Net):
        def __call__(self, req, timeout=None):
            _time.sleep(0.25)
            return super().__call__(req, timeout)

    net = _SlowNet()
    inner = _Inner()
    h, orig = _handler(net, inner)
    try:
        t0 = _time.monotonic()
        h.offer(_msg())
        elapsed = _time.monotonic() - t0
        check(h.flush(5), "slow receipt eventually delivered")
    finally:
        urllib.request.urlopen = orig
    check(elapsed < 0.1,
          f"offer() returned in {elapsed:.3f}s with 250ms react I/O — drain not delayed")
    check(len(inner.offered) == 1 and len(net.calls) == 1,
          "inner ran immediately; react delivered async")


def test_queue_overflow_drops_never_blocks():
    import threading as _threading
    import time as _time

    release = _threading.Event()

    class _BlockedNet(_Net):
        def __call__(self, req, timeout=None):
            release.wait(5)
            return super().__call__(req, timeout)

    net = _BlockedNet()
    logs = []
    h = ReactObserverHandler(_Inner(), _react_sender(), "@me:hs",
                             log=logs.append, queue_cap=2)
    urllib.request.urlopen, orig = net, urllib.request.urlopen
    try:
        t0 = _time.monotonic()
        # worker takes m0 and blocks; m1+m2 fill the 2-slot queue; m3+ drop
        for i in range(6):
            h.offer(_msg(eid=f"o{i}", mid=f"om{i}"))
            _time.sleep(0.02)
        elapsed = _time.monotonic() - t0
        release.set()
        check(h.flush(5), "queue drains after endpoint unblocks")
    finally:
        urllib.request.urlopen = orig
    check(elapsed < 1.0, f"6 offers against a wedged endpoint took {elapsed:.3f}s — never blocked")
    check(any("queue full" in m for m in logs), "overflow drop is logged")
    check(len(net.calls) < 6, "overflow receipts dropped, not queued unboundedly")


# ---- bridge wiring (mirrors test_event_wiring's stubbed-thread approach) ----

class _NoThread:
    def __init__(self, target=None, name=None, daemon=None, **kw):
        pass

    def start(self):
        pass


def _load_bridge(tmp):
    os.environ["AGENT_CONNECT_STATE_DIR"] = str(tmp)
    os.environ.setdefault("REMOTE_TASK_URL", "https://gw.example/relay")
    os.environ.setdefault("REMOTE_TASK_TOKEN", "dummy-secret")
    mod = importlib.import_module("ag2_sparrow.remote_gateway_bridge")
    return importlib.reload(mod)


def _start_and_grab_handler(m):
    """Run _maybe_start_event_channel with threads stubbed; capture the handler
    EventConsumer was built with."""
    import threading
    grabbed = {}
    orig_thread = threading.Thread
    from ag2_sparrow import event_consumer as ec
    orig_consumer = ec.EventConsumer

    class _GrabConsumer(orig_consumer):
        def __init__(self, inbox, handler, **kw):
            grabbed["handler"] = handler
            super().__init__(inbox, handler, **kw)

    threading.Thread = _NoThread
    ec.EventConsumer = _GrabConsumer
    try:
        m._EVENT_CHANNEL = None
        m._maybe_start_event_channel()
    finally:
        threading.Thread = orig_thread
        ec.EventConsumer = orig_consumer
    return grabbed.get("handler")



def test_unrelated_shared_room_message_gets_no_reaction_by_default():
    """Asserts on /react traffic, not handler type. AGENT_MXID is set on
    purpose so a missing identity can't make this pass for the wrong reason."""
    with tempfile.TemporaryDirectory() as d:
        m = _load_bridge(pathlib.Path(d))
        net = _Net()
        orig = urllib.request.urlopen
        urllib.request.urlopen = net
        try:
            os.environ["SPARROW_EVENTS"] = "1"
            os.environ["AGENT_MXID"] = "@me:hs"
            os.environ.pop("SPARROW_OBSERVE_REACT", None)
            for k in ("SPARROW_HA_OWNER", "SPARROW_HA_ROOM"):
                os.environ.pop(k, None)
            h = _start_and_grab_handler(m)
            # A human in a shared room this agent merely subscribes to.
            h.offer(_msg(eid="shared-1", actor="@someone-else:hs",
                         room="!busy-shared-room:hs", mid="msg-1"))
            reacts = [c for c in net.calls
                      if str(getattr(c, "full_url", "")).endswith("/react")]
            check(reacts == [],
                  "default: unrelated shared-room message gets NO react POST")
        finally:
            urllib.request.urlopen = orig
            os.environ.pop("SPARROW_EVENTS", None)
            os.environ.pop("AGENT_MXID", None)


def test_opted_in_still_reacts_so_the_default_test_is_not_vacuous():
    """Positive control: without it, the default test's `reacts == []` would
    also hold if the harness never delivered events at all."""
    with tempfile.TemporaryDirectory() as d:
        m = _load_bridge(pathlib.Path(d))
        net = _Net()
        orig = urllib.request.urlopen
        urllib.request.urlopen = net
        try:
            os.environ["SPARROW_EVENTS"] = "1"
            os.environ["AGENT_MXID"] = "@me:hs"
            os.environ["SPARROW_OBSERVE_REACT"] = "1"
            for k in ("SPARROW_HA_OWNER", "SPARROW_HA_ROOM"):
                os.environ.pop(k, None)
            h = _start_and_grab_handler(m)
            h.offer(_msg(eid="shared-2", actor="@someone-else:hs",
                         room="!busy-shared-room:hs", mid="msg-2"))
            if hasattr(h, "flush"):
                h.flush(5)
            reacts = [c for c in net.calls
                      if str(getattr(c, "full_url", "")).endswith("/react")]
            check(len(reacts) == 1,
                  "opted in: the same message DOES get exactly one react POST")
        finally:
            urllib.request.urlopen = orig
            for k in ("SPARROW_EVENTS", "AGENT_MXID", "SPARROW_OBSERVE_REACT"):
                os.environ.pop(k, None)

def test_catchup_guard_skips_backlog():
    import time as _time
    # Old → seen, not reacted (and stays silent on redelivery); fresh →
    # reacted; ts-less → live; max_age_s=0 disables the guard.
    net = _Net()
    h, orig = _handler(net)
    try:
        old_ms = (_time.time() - 3600) * 1000
        fresh_ms = _time.time() * 1000
        h._maybe_react({**_msg(mid="m-old"), "ts": old_ms})
        check(not net.calls and "m-old" in h._seen,
              "backlog event marked seen, no react (catch-up guard)")
        h._maybe_react({**_msg(mid="m-old"), "ts": fresh_ms})
        check(not net.calls, "redelivered backlog message stays silent (seen)")
        h._maybe_react({**_msg(mid="m-fresh"), "ts": fresh_ms})
        h._maybe_react(_msg(mid="m-tsless"))
        check(h.flush(5) and len(net.calls) == 2,
              "fresh + ts-less events still react")
    finally:
        urllib.request.urlopen = orig

    net2 = _Net()
    h2, orig2 = _handler(net2)
    h2._max_age_s = 0
    try:
        h2._maybe_react({**_msg(mid="m-old2"), "ts": (_time.time() - 3600) * 1000})
        check(h2.flush(5) and len(net2.calls) == 1,
              "max_age_s=0 disables the guard (old event reacts)")
    finally:
        urllib.request.urlopen = orig2


def test_bridge_wiring_is_OPT_IN_not_default_on():
    with tempfile.TemporaryDirectory() as d:
        m = _load_bridge(pathlib.Path(d))
        os.environ["SPARROW_EVENTS"] = "1"
        for k in ("SPARROW_HA_OWNER", "SPARROW_HA_ROOM"):
            os.environ.pop(k, None)

        os.environ["AGENT_MXID"] = "@me:hs"
        os.environ.pop("SPARROW_OBSERVE_REACT", None)
        h = _start_and_grab_handler(m)
        check(type(h).__name__ == "TaskifyHandler",
              "wiring: env unset → observer OFF (opt-in)")

        os.environ["SPARROW_OBSERVE_REACT"] = "0"
        h = _start_and_grab_handler(m)
        check(type(h).__name__ == "TaskifyHandler",
              "wiring: SPARROW_OBSERVE_REACT=0 → still off")

        os.environ["SPARROW_OBSERVE_REACT"] = "1"
        h = _start_and_grab_handler(m)
        check(type(h).__name__ == "ReactObserverHandler",
              "wiring: SPARROW_OBSERVE_REACT=1 → observer wraps handler (explicit opt-in)")
        os.environ.pop("SPARROW_OBSERVE_REACT", None)

        os.environ.pop("SPARROW_OBSERVE_REACT", None)
        os.environ.pop("AGENT_MXID", None)
        os.environ.pop("AGENT_ID", None)
        h = _start_and_grab_handler(m)
        check(type(h).__name__ == "TaskifyHandler",
              "wiring: no AGENT_MXID/AGENT_ID → observer stays off")

        # AGENT_ID fallback (live-deployment finding: a real install's durable
        # env names the id AGENT_ID — "default-on" must hold there too).
        os.environ["AGENT_ID"] = "@me:hs"
        os.environ["SPARROW_OBSERVE_REACT"] = "1"
        h = _start_and_grab_handler(m)
        check(type(h).__name__ == "ReactObserverHandler",
              "wiring: AGENT_ID is still honored as the fallback name when opted in")
        os.environ.pop("SPARROW_OBSERVE_REACT", None)
        os.environ.pop("AGENT_ID", None)

        os.environ.pop("SPARROW_EVENTS", None)


if __name__ == "__main__":
    test_reacts_and_stays_transparent()
    test_skips_self_noise_and_dups()
    test_no_mxid_never_reacts()
    test_failures_never_break_the_chain()
    test_seen_set_is_bounded()
    test_room_id_fully_escaped()
    test_slow_reaction_never_delays_inner()
    test_queue_overflow_drops_never_blocks()
    test_catchup_guard_skips_backlog()
    test_unrelated_shared_room_message_gets_no_reaction_by_default()
    test_opted_in_still_reacts_so_the_default_test_is_not_vacuous()
    test_bridge_wiring_is_OPT_IN_not_default_on()
    print(("PASS" if not FAILS else f"FAIL ({len(FAILS)})"))
    sys.exit(1 if FAILS else 0)
