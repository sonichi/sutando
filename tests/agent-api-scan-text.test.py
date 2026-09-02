"""agent-api /scan-text — the Signal Room daemon's decoded-value guard re-run.

Exercises the real Handler.do_POST (same harness as agent-api-guest-routes):

  * the tier is pinned server-side (SIGNAL_ROOM_TIER) — the request carries no
    tier and cannot pick one;
  * verdicts: clean text -> pass; a withheld verdict from the guard -> withhold;
  * suppression markers exempt nothing (the daemon publishes the text itself);
  * a failing underlying scanner -> 500 (the daemon reads non-200 as fail-closed);
  * input validation: auth, Content-Length caps, texts shape/limits.

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


def make_handler(path: str, headers: dict, body: bytes = b"", auth: bool = True):
    h = agent_api.Handler.__new__(agent_api.Handler)
    h.path = path
    h.headers = headers
    h.rfile = FakeRFile(body)
    h._responses = []
    h.check_auth = lambda: (auth if auth else h._responses.append((401, {"error": "unauthorized"})) or False)
    h.send_json = lambda status, data: h._responses.append((status, data))
    h.send_private_json = lambda status, data: h._responses.append((status, data))
    return h


def post(payload, auth: bool = True):
    body = json.dumps(payload).encode()
    h = make_handler("/scan-text", {"Content-Length": str(len(body))}, body, auth=auth)
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
    h = post({"texts": ["x"]}, auth=False)
    check("honours check_auth", not any(r[0] == 200 for r in h._responses))

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

    # [1] and "hi" are VALID JSON that is not an object: json.loads succeeds, so
    # they reach .get() outside the parse except and raised AttributeError —
    # an uncaught 500 for an authenticated caller rather than a 400.
    for bad in ({}, {"texts": []}, {"texts": "one"}, {"texts": [1]},
                {"texts": ["x"] * 65}, {"texts": ["y" * 16385]},
                [1], "hi"):
        h = post(bad)
        if h._responses[0][0] != 400:
            check(f"bad texts shape rejected: {str(bad)[:40]}", False)
            break
    else:
        check("every bad texts shape -> 400", True)


run()
print()
if failures:
    print(f"FAILED ({failures})")
    sys.exit(1)
print("all /scan-text checks passed")
