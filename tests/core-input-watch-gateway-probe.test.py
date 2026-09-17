#!/usr/bin/env python3
"""
Tests for `gateway_alive` / `_gateway_status` in src/core-input-watch.py.

Motivated by 2026-07-21: the supervisor pinned the core at `gateway-down` —
surfacing as a standing health-check warning — while the bridge was connected
and rewriting state/gateway-status.json every few seconds.

Mechanism: on a bundled install the probe was
`pgrep -f <app_data>/engine/runtime/python`, i.e. "is a process running under
the app-bundle interpreter". The bridge on this host runs under system python
(startup.sh invokes a bare `python3`), so the pattern matched nothing. The
probe answered a question about argv when the question was whether the gateway
is serving.

Covers:
  a) fresh + connected  → True, without consulting pgrep
  b) fresh + disconnected → False, without consulting pgrep (info the pgrep
     path never had — a live-but-unauthenticated bridge)
  c) stale ts → fall back to pgrep (a wedged bridge must not read as alive)
  d) missing file → fall back (bridge too old to emit the sidecar)
  e) malformed JSON → fall back
  f) non-numeric ts → fall back
  g) state_dir None → fall back (preserves the old call signature)
  h) the 90s threshold clears the bridge's worst-case write gap
  i) connected:false WITH backoff_s and a recent last_ok_ts → alive (a
     retryable transport blip, not a dead link). The 35s gap is measured from
     a real capture: a 1s backoff still means a whole poll cycle since success,
     so a grace window under ~35s would fail on live traffic.
  j) auth rejection (backoff_s 0) stays down despite a recent last_ok_ts
  k) a 4.9h outage stays down — backoff grows, last_ok_ts ages out
     (REMOTE_TASK_POLL_WAIT 25s + 10s request timeout)

Run: python3 tests/core-input-watch-gateway-probe.test.py
Exit code: 0 on pass, 1 on fail.
"""

from __future__ import annotations
import importlib.util
import json
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location(
    "core_input_watch", REPO / "src" / "core-input-watch.py")
ciw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ciw)


def with_status(payload, *, pgrep_returns=False, app_data="", write=True):
    """Run gateway_alive against a temp state dir holding `payload`.

    Returns (verdict, pgrep_was_called). `payload=None` with write=True writes
    invalid JSON; write=False skips the file entirely.
    """
    calls = []

    def fake_pgrep(pattern):
        calls.append(pattern)
        return pgrep_returns

    with tempfile.TemporaryDirectory() as td:
        state_dir = Path(td)
        if write:
            body = json.dumps(payload) if payload is not None else "{ not json"
            (state_dir / "gateway-status.json").write_text(body)
        orig = ciw._pgrep
        try:
            ciw._pgrep = fake_pgrep
            verdict = ciw.gateway_alive(app_data, str(state_dir))
        finally:
            ciw._pgrep = orig
    return verdict, bool(calls)


def case_a_fresh_connected() -> list[str]:
    # last_ok_ts present: this case pins the pgrep-avoidance path, not the
    # never-polled rule (case m). Without it the fixture IS a never-polled lane.
    v, pgrepped = with_status(
        {"connected": True, "ts": time.time(), "last_ok_ts": time.time() - 5},
        pgrep_returns=False,
    )
    fails = []
    if v is not True:
        fails.append(f"a) fresh+connected should be True, got {v!r}")
    if pgrepped:
        fails.append("a) should not consult pgrep when the sidecar answers")
    return fails


def case_b_fresh_disconnected() -> list[str]:
    # pgrep_returns=True: if the fallback ran, the verdict would wrongly be True.
    v, pgrepped = with_status({"connected": False, "ts": time.time()}, pgrep_returns=True)
    fails = []
    if v is not False:
        fails.append(f"b) fresh+disconnected should be False, got {v!r}")
    if pgrepped:
        fails.append("b) a fresh sidecar saying 'not connected' must not be second-guessed by pgrep")
    return fails


def case_c_stale_falls_back() -> list[str]:
    old = time.time() - (ciw.GATEWAY_STATUS_MAX_AGE_S + 30)
    v, pgrepped = with_status({"connected": True, "ts": old}, pgrep_returns=False)
    fails = []
    if not pgrepped:
        fails.append("c) a stale sidecar must fall back to pgrep, not be trusted")
    if v is not False:
        fails.append(f"c) stale + no process should be False, got {v!r}")
    return fails


def case_d_missing_falls_back() -> list[str]:
    v, pgrepped = with_status(None, write=False, pgrep_returns=True)
    fails = []
    if not pgrepped:
        fails.append("d) missing sidecar must fall back to pgrep")
    if v is not True:
        fails.append(f"d) fallback should return the pgrep verdict, got {v!r}")
    return fails


def case_e_malformed_falls_back() -> list[str]:
    v, pgrepped = with_status(None, write=True, pgrep_returns=True)
    if not pgrepped:
        return ["e) malformed JSON must fall back to pgrep"]
    return []


def case_f_non_numeric_ts_falls_back() -> list[str]:
    v, pgrepped = with_status({"connected": True, "ts": "just now"}, pgrep_returns=True)
    if not pgrepped:
        return ["f) non-numeric ts must fall back to pgrep"]
    return []


def case_g_no_state_dir_falls_back() -> list[str]:
    """The old signature was gateway_alive(app_data); callers passing one arg
    must still get the pgrep behavior."""
    calls = []
    orig = ciw._pgrep
    try:
        ciw._pgrep = lambda p: (calls.append(p), True)[1]
        v = ciw.gateway_alive("")
    finally:
        ciw._pgrep = orig
    fails = []
    if not calls:
        fails.append("g) no state_dir must fall back to pgrep")
    if v is not True:
        fails.append(f"g) should return pgrep's verdict, got {v!r}")
    return fails


def case_h_threshold_clears_poll_gap() -> list[str]:
    """The bridge long-polls REMOTE_TASK_POLL_WAIT (default 25s) with a +10s
    request timeout, so ~35s is the worst-case gap between writes on a HEALTHY
    connection. The threshold must sit above that or healthy bridges flap."""
    if ciw.GATEWAY_STATUS_MAX_AGE_S <= 35:
        return [f"h) threshold {ciw.GATEWAY_STATUS_MAX_AGE_S}s does not clear the "
                "35s worst-case healthy write gap"]
    # And a write inside that gap must still read fresh.
    v, pgrepped = with_status(
        {"connected": True, "ts": time.time() - 35, "last_ok_ts": time.time() - 35}
    )
    if v is not True or pgrepped:
        return ["h) a 35s-old sidecar (healthy long-poll gap) must still be trusted"]
    return []


def case_i_transient_backoff_is_alive() -> list[str]:
    # Values from a REAL captured blip, not invented: backoff_s 1 with the last
    # success 35s back — a poll cycle (POLL_WAIT 25s + 10s timeout), not seconds.
    v, pgrepped = with_status(
        {"connected": False, "ts": time.time(), "backoff_s": 1,
         "last_ok_ts": time.time() - 35,
         "error": "network: The read operation timed out"},
        pgrep_returns=False)
    fails = []
    if v is not True:
        fails.append(f"i) a 1s retry with a 35s-old success should be alive, got {v!r}")
    if pgrepped:
        fails.append("i) the sidecar answered; pgrep must not be consulted")
    return fails


def case_j_auth_rejection_stays_down() -> list[str]:
    # Auth rejection writes backoff_s 0 — it is not retryable and must stay down
    # even though last_ok_ts is recent. Guards the case-b intent.
    v, _ = with_status(
        {"connected": False, "ts": time.time(), "backoff_s": 0,
         "last_ok_ts": time.time() - 5, "error": "auth rejected HTTP 401"},
        pgrep_returns=True)
    return [] if v is False else [f"j) auth rejection must read down, got {v!r}"]


def case_k_sustained_outage_stays_down() -> list[str]:
    # The 2026-07-28 incident: 4.9h of connected:false reported as running. The
    # backoff grows but last_ok_ts ages out, so the verdict must be False.
    v, _ = with_status(
        {"connected": False, "ts": time.time(), "backoff_s": 64,
         "last_ok_ts": time.time() - 4.9 * 3600, "error": "network: no route"},
        pgrep_returns=True)
    return [] if v is False else [f"k) a 4.9h outage must read down, got {v!r}"]


def case_l_never_connected_stays_down() -> list[str]:
    # Never-connected: backoff_s grows, last_ok_ts ABSENT. Reads down only via
    # isinstance(None, ...), so a `.get(..., time.time())` default would flip it.
    v, _ = with_status(
        {"connected": False, "ts": time.time(), "backoff_s": 4,
         "error": "network: no route"},
        pgrep_returns=True)
    return [] if v is False else [f"l) never-connected must read down, got {v!r}"]


def case_m_connected_without_last_ok_stays_down():
    """`connected` alone is not serving: a lane that never completed a poll
    carries connected=true with last_ok_ts null. This is the shape a dead
    bridge's own final write leaves behind, and the reason cases a) and h)
    above had to name a last_ok_ts — before this rule they were this case."""
    fails = []
    for label, doc in (
        ("null", {"connected": True, "ts": time.time(), "last_ok_ts": None}),
        ("absent", {"connected": True, "ts": time.time()}),
    ):
        v, _ = with_status(doc)
        if v is not False:
            fails.append(f"m) connected with {label} last_ok_ts should be False, got {v!r}")
    # The reconnect grace must not rescue it either — there is no success to age.
    v, _ = with_status({"connected": False, "ts": time.time(), "last_ok_ts": None, "backoff_s": 5})
    if v is not False:
        fails.append(f"m) backoff grace must not apply to a never-polled lane, got {v!r}")
    return fails


def main() -> int:
    cases = [
        ("a", case_a_fresh_connected),
        ("b", case_b_fresh_disconnected),
        ("c", case_c_stale_falls_back),
        ("d", case_d_missing_falls_back),
        ("e", case_e_malformed_falls_back),
        ("f", case_f_non_numeric_ts_falls_back),
        ("g", case_g_no_state_dir_falls_back),
        ("h", case_h_threshold_clears_poll_gap),
        ("i", case_i_transient_backoff_is_alive),
        ("j", case_j_auth_rejection_stays_down),
        ("k", case_k_sustained_outage_stays_down),
        ("l", case_l_never_connected_stays_down),
        ("m", case_m_connected_without_last_ok_stays_down),
    ]
    all_failures = []
    for label, fn in cases:
        try:
            fails = fn()
        except Exception as e:
            fails = [f"{label}) raised {type(e).__name__}: {e}"]
        if fails:
            all_failures.extend(fails)
            print(f"  ✗ case {label}")
            for f in fails:
                print(f"      {f}")
        else:
            print(f"  ✓ case {label}")
    if all_failures:
        print(f"\n{len(all_failures)} failure(s)")
        return 1
    print("\nGateway-probe invariants hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
