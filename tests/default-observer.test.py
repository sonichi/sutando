"""Tests for the built-in 👀 observed-receipt (default_observer) — the tee
wrapper's chain transparency, react targeting, self-echo/dedup suppression, and
failure isolation; plus the bridge's default-on / opt-out / no-mxid wiring.
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
            def close(self):
                pass
        return _R()


def _msg(eid="e1", actor="@alice:hs", room="!r:hs", mid="m1", etype="message.created"):
    return {"event_id": eid, "type": etype, "actor_id": actor,
            "room_id": room, "content": {"message_id": mid}}


def _handler(net, inner=None, mxid="@me:hs"):
    h = ReactObserverHandler(inner or _Inner(), "https://gw.example/relay",
                             {"Authorization": "Bearer t"}, mxid,
                             log=lambda *_: None)
    urllib.request.urlopen, orig = net, urllib.request.urlopen
    return h, orig


def test_reacts_and_stays_transparent():
    net = _Net()
    inner = _Inner()
    h, orig = _handler(net, inner)
    try:
        out = h.offer(_msg())
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
    finally:
        urllib.request.urlopen = orig
    check(len(h._seen) <= _SEEN_CAP and len(h._order) <= _SEEN_CAP,
          "seen-set FIFO-bounded at cap")


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


def test_bridge_wiring_default_on_optout_and_no_mxid():
    with tempfile.TemporaryDirectory() as d:
        m = _load_bridge(pathlib.Path(d))
        os.environ["SPARROW_EVENTS"] = "1"
        for k in ("SPARROW_HA_OWNER", "SPARROW_HA_ROOM"):
            os.environ.pop(k, None)

        os.environ["AGENT_MXID"] = "@me:hs"
        os.environ.pop("SPARROW_OBSERVE_REACT", None)
        h = _start_and_grab_handler(m)
        check(type(h).__name__ == "ReactObserverHandler",
              "wiring: env unset → observer wraps handler (DEFAULT ON)")

        os.environ["SPARROW_OBSERVE_REACT"] = "0"
        h = _start_and_grab_handler(m)
        check(type(h).__name__ == "TaskifyHandler",
              "wiring: SPARROW_OBSERVE_REACT=0 → plain taskify handler (opt-out)")

        os.environ.pop("SPARROW_OBSERVE_REACT", None)
        os.environ.pop("AGENT_MXID", None)
        h = _start_and_grab_handler(m)
        check(type(h).__name__ == "TaskifyHandler",
              "wiring: no AGENT_MXID → observer stays off")

        os.environ.pop("SPARROW_EVENTS", None)


if __name__ == "__main__":
    test_reacts_and_stays_transparent()
    test_skips_self_noise_and_dups()
    test_no_mxid_never_reacts()
    test_failures_never_break_the_chain()
    test_seen_set_is_bounded()
    test_bridge_wiring_default_on_optout_and_no_mxid()
    print(("PASS" if not FAILS else f"FAIL ({len(FAILS)})"))
    sys.exit(1 if FAILS else 0)
