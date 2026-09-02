"""agent-api /scan-text — the Signal Room daemon's decoded-value guard re-run.

Exercises the real Handler.do_POST (same harness as agent-api-guest-routes):

  * the tier is pinned server-side (SIGNAL_ROOM_TIER) — the request carries no
    tier and cannot pick one;
  * verdicts: clean text -> pass; a withheld verdict from the guard -> withhold;
  * suppression markers exempt nothing (the daemon publishes the text itself);
  * a failing underlying scanner -> 500 (the daemon reads non-200 as fail-closed);
  * auth is registry-scoped exactly like /guest-task: the legacy global token
    works until a live per-room row exists, then only a per-room ENQUEUE token
    whose room equals the body room_id (cross-room 403, read scope 403), and
    it stays refused once every row is revoked, an invalid file is a 503, and
    provisioning is irreversible — an emptied or deleted registry is a 503,
    never a return to the legacy gate;
  * input validation (run while still unprovisioned): bearer, Content-Length
    caps, texts shape/limits.

Run: `python3 tests/agent-api-scan-text.test.py`
"""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

_TEST_WS = tempfile.mkdtemp()
os.environ["SUTANDO_TEST_MODE"] = "1"
os.environ["SUTANDO_WORKSPACE"] = _TEST_WS
SRC = Path(__file__).resolve().parent.parent / "src"
_spec = importlib.util.spec_from_file_location("agent_api", str(SRC / "agent-api.py"))
agent_api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(agent_api)
from policy import signal_tokens as st  # noqa: E402

GLOBAL = "global-token"
agent_api.API_TOKEN = GLOBAL
agent_api.SIGNAL_TOKEN_REGISTRY = Path(_TEST_WS) / "state" / "signal-room-tokens.json"

failures = 0


def check(name: str, cond: bool) -> None:
    global failures
    print(("ok: " if cond else "FAIL: ") + name)
    if not cond:
        failures += 1


class FakeRFile:
    def __init__(self, body: bytes = b""):
        self.body = body
        self.read_calls = 0

    def read(self, n: int = -1) -> bytes:
        self.read_calls += 1
        return self.body


def make_handler(path: str, headers: dict, body: bytes = b"", token=GLOBAL):
    h = agent_api.Handler.__new__(agent_api.Handler)
    h.path = path
    h.headers = dict(headers)
    if token is not None:
        h.headers["Authorization"] = f"Bearer {token}"
    h.rfile = FakeRFile(body)
    h._responses = []
    h.send_json = lambda status, data: h._responses.append((status, data))
    h.send_private_json = lambda status, data: h._responses.append((status, data))
    return h


def post(payload, token=GLOBAL):
    body = json.dumps(payload).encode()
    h = make_handler("/scan-text", {"Content-Length": str(len(body))}, body, token=token)
    h.do_POST()
    return h


def run() -> None:
    import policy.egress.result as egress

    # --- verdicts, with the guard spied so verdicts are deterministic ---------
    calls = []
    real_guard = egress.guard_result_for_tier

    def spy(body, tier, repo, *a, **kw):
        calls.append((body, tier, kw.get("honor_suppressions")))
        if "TRIGGER" in body:
            return "[withheld]", "secret-scan"
        return body, None

    egress.guard_result_for_tier = spy
    try:
        h = post({"texts": ["clean one", "clean two"]})
        check("clean texts -> 200 pass", h._responses[0] == (200, {"verdict": "pass"}))
        check("tier is pinned server-side to the Signal Room lane",
              all(t == agent_api.SIGNAL_ROOM_TIER for _b, t, _hs in calls))
        check("published-text mode: honor_suppressions=False on every call",
              all(hs is False for _b, _t, hs in calls))

        calls.clear()
        h = post({"texts": ["fine", "has TRIGGER inside", "never scanned"]})
        check("any withheld verdict -> withhold", h._responses[0] == (200, {"verdict": "withhold"}))
        check("scan short-circuits after the hit", len(calls) == 2)

        # A caller-supplied tier field is ignored — there is nothing to elect.
        calls.clear()
        h = post({"texts": ["x"], "tier": "owner", "access_tier": "owner"})
        check("caller cannot elect a tier", h._responses[0][0] == 200
              and all(t == agent_api.SIGNAL_ROOM_TIER for _b, t, _hs in calls))
    finally:
        egress.guard_result_for_tier = real_guard

    # --- real guard: the daemon publishes the text itself, so a [no-send] skip
    # marker exempts nothing and a failing underlying scanner is a 500 ---------
    from types import SimpleNamespace

    real_loader = egress.load_team_result_scanner
    egress.load_team_result_scanner = lambda repo: (lambda body: SimpleNamespace(
        detected="AKIA" in body, secret_types=["aws-key"]))
    try:
        h = post({"texts": ["[no-send]\nAKIAFAKEFAKEFAKEFAKE"]})
        check("a skip marker does not exempt a secret from the scan",
              h._responses[0] == (200, {"verdict": "withhold"}))
        h = post({"texts": ["[no-send]\nnothing sensitive here"]})
        check("a clean skip-marker body still passes",
              h._responses[0] == (200, {"verdict": "pass"}))

        def down(_repo):
            raise RuntimeError("scanner down")

        egress.load_team_result_scanner = down
        h = post({"texts": ["anything"]})
        check("failing underlying scanner -> 500 with the real guard in place",
              h._responses[0] == (500, {"error": "scanner unavailable"}))
    finally:
        egress.load_team_result_scanner = real_loader

    # --- a guard that itself throws (out-of-contract) still fails closed ------
    def boom(*a, **kw):
        raise RuntimeError("guard blew up")

    egress.guard_result_for_tier = boom
    try:
        h = post({"texts": ["anything"]})
        check("out-of-contract guard exception -> 500",
              h._responses[0][0] == 500)
    finally:
        egress.guard_result_for_tier = real_guard

    # --- input validation -----------------------------------------------------
    h = post({"texts": ["x"]}, token="wrong")
    check("wrong bearer -> 401", h._responses[0][0] == 401)
    h = post({"texts": ["x"]}, token=None)
    check("missing bearer -> 401", h._responses[0][0] == 401)

    h = make_handler("/scan-text", {"Content-Length": "999999"})
    h.do_POST()
    check("oversized request -> 413 before the body is read",
          h._responses[0][0] == 413 and h.rfile.read_calls == 0)

    h = make_handler("/scan-text", {"Content-Length": "nope"})
    h.do_POST()
    check("invalid Content-Length -> 400", h._responses[0][0] == 400)

    h = make_handler("/scan-text", {"Content-Length": "5"}, b"{oops")
    h.do_POST()
    check("malformed JSON -> 400", h._responses[0][0] == 400)

    # [1] and "hi" are valid JSON that is not an object: they parse, then reach
    # .get() outside the except and raised instead of returning 400.
    for bad in ({}, {"texts": []}, {"texts": "one"}, {"texts": [1]},
                {"texts": ["x"] * 65}, {"texts": ["y" * 16385]},
                [1], "hi"):
        h = post(bad)
        if h._responses[0][0] != 400:
            check(f"bad texts shape rejected: {str(bad)[:40]}", False)
            break
    else:
        check("every bad texts shape -> 400", True)


    # --- registry-scoped auth, mirroring /guest-task ---------------------------
    egress.guard_result_for_tier = lambda body, tier, repo, *a, **kw: (body, None)
    registry = agent_api.SIGNAL_TOKEN_REGISTRY
    rows = [st.make_row("!a:hs", "enqueue", "tok-a-enq", created_at_ms=1),
            st.make_row("!a:hs", "read", "tok-a-read", created_at_ms=2),
            st.make_row("!b:hs", "enqueue", "tok-b-enq", created_at_ms=3)]
    try:
        h = post({"texts": ["x"], "room_id": "!a:hs"})
        check("global token accepted before any per-room row exists",
              h._responses[0] == (200, {"verdict": "pass"}))
        h = post({"texts": ["x"]}, token="tok-a-enq")
        check("an unprovisioned room token is 401 (ordinary gate, no rows yet)",
              h._responses[0][0] == 401)

        st.write_registry(registry, rows)
        h = post({"texts": ["x"], "room_id": "!a:hs"}, token="tok-a-enq")
        check("per-room enqueue token accepted for its own room",
              h._responses[0] == (200, {"verdict": "pass"}))
        h = post({"texts": ["x"]}, token="tok-a-enq")
        check("no body room_id: the token's room stands (200)", h._responses[0][0] == 200)
        h = post({"texts": ["x"], "room_id": "!b:hs"}, token="tok-a-enq")
        check("body room_id != token room -> 403", h._responses[0][0] == 403)
        h = post({"texts": ["x"], "room_id": "!a:hs"}, token="tok-a-read")
        check("read-scope token cannot scan (403)", h._responses[0][0] == 403)
        h = post({"texts": ["x"], "room_id": "!a:hs"}, token=GLOBAL)
        check("global token refused once a live row exists (403)", h._responses[0][0] == 403)
        h = post({"texts": ["x"]}, token="tok-unknown")
        check("unknown token is 403, never the legacy path", h._responses[0][0] == 403)
        h = post({"texts": ["x"] * 65, "room_id": "!b:hs"}, token="tok-a-enq")
        check("payload shape is validated before the room binding (400, not 403)",
              h._responses[0][0] == 400)

        st.write_registry(registry, [dict(r, revoked_at=9) for r in rows])
        h = post({"texts": ["x"], "room_id": "!a:hs"}, token=GLOBAL)
        check("every row revoked: the global token stays refused (403)", h._responses[0][0] == 403)
        h = post({"texts": ["x"]}, token="tok-a-enq")
        check("a revoked room token is refused as unscoped (403)", h._responses[0][0] == 403)
        registry.write_text("{not json")
        h = post({"texts": ["x"], "room_id": "!a:hs"}, token=GLOBAL)
        check("invalid registry: the global token is refused (503)", h._responses[0][0] == 503)
        h = post({"texts": ["x"]}, token="tok-a-enq")
        check("invalid registry: a room token is refused (503)", h._responses[0][0] == 503)
        st.write_registry(registry, [])
        h = post({"texts": ["x"], "room_id": "!a:hs"}, token=GLOBAL)
        check("emptied after provisioning: the global token is refused (503), never re-admitted",
              h._responses[0][0] == 503)
        registry.unlink()
        h = post({"texts": ["x"], "room_id": "!a:hs"}, token=GLOBAL)
        check("deleted after provisioning: still 503 — the marker outlives the file",
              h._responses[0][0] == 503 and st.marker_path(_TEST_WS).is_file())
        h = post({"texts": ["x"]}, token="tok-a-enq")
        check("deleted after provisioning: a would-be room token is refused too (503)", h._responses[0][0] == 503)
    finally:
        egress.guard_result_for_tier = real_guard


run()
print()
if failures:
    print(f"FAILED ({failures})")
    sys.exit(1)
print("all /scan-text checks passed")
