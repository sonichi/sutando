#!/usr/bin/env python3
"""A guarded result the gateway closes silently is always journaled for the owner.

The drain and the suppression journal read one verdict: parse_markers with
skip_after_channel=True on the guarded body. `[dm-only]` drops the redirect, so a
team `[dm-only]` + `[channel: X]` + skip is not withheld; the drain closes it, and
the journal must record it.

  a) team  [dm-only]+[channel:]+[no-send]  -> closed no_send, a wr_*.json record
  b) team  [channel:]+[dm-only]+[deduped:] -> dedup plan, a wr_*.json record
  c) team  [no-send]                        -> record (control, as on main)
  d) owner [dm-only]+[channel:]+[no-send]   -> closed no_send, no record (owner unguarded)
  e) owner [channel:]+[no-send]             -> closed no_send, no record (#5340 unchanged)
  f) _quarantine_unsent reads the same verdict: an owner [channel:]+[no-send] is
     archived as suppressed, never quarantined as a redirect; a team
     [dm-only]+[channel:]+[no-send] is archived and journaled

Loads src/remote-gateway-bridge.py in-process through the #5326 harness (its real
PROACTIVE_CLAIM_GATE), with an isolated workspace and a fake `_req`.
Run: python3 tests/gateway-dm-only-channel-skip-journal.test.py
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "gw_thread_ask_harness", REPO / "tests" / "gateway-task-result-thread-ask.test.py")
_h = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_h)
Harness, OTHER = _h.Harness, _h.OTHER
FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def records(h) -> list[dict]:
    d = h.mod._STATE / "suppressed-team-results"
    return [json.loads(p.read_text()) for p in sorted(d.glob("wr_*.json"))] if d.is_dir() else []


def closed_silently(h) -> bool:
    wire = json.dumps(h.results)
    return ("visible" not in wire and "[channel:" not in wire
            and all(r.get("no_send") is True for r in h.results))


def main() -> int:
    # a) team: [dm-only] drops the redirect; the drain's skip is journaled
    for n, body in enumerate((f"[dm-only]\n[channel: {OTHER}]\n[no-send]\nvisible\n",
                              f"[channel: {OTHER}]\n[dm-only]\n[REPLIED]\nvisible\n")):
        h = Harness()
        h.run(f"dj-a{n}", body, tier="team")
        check(len(h.results) == 1 and closed_silently(h),
              f"a{n}) team: one lease-close POST, nothing visible; got {h.results}")
        recs = records(h)
        check(len(recs) == 1 and recs[0].get("task_id") == f"dj-a{n}"
              and recs[0].get("suppressed_body") == body.strip()
              and recs[0].get("status") == "suppressed_silent_close",
              f"a{n}) team: the silent close has an owner record; got {recs}")

    # b) team: [deduped:] after [channel:]+[dm-only] goes through the dedup plan, recorded
    h = Harness()
    body = f"[channel: {OTHER}]\n[dm-only]\n[deduped: dj-holder]\nvisible\n"
    h.run("dj-b", body, tier="team")
    check(closed_silently(h), f"b) team [deduped:]: never posted as text; got {h.results}")
    check(any("dedup" in m and "dj-b" in m for m in h.logs),
          f"b) team [deduped:]: routed through the dedup plan; got {[m for m in h.logs if 'dj-b' in m]}")
    recs = records(h)
    check(len(recs) == 1 and recs[0].get("suppressed_body") == body.strip(),
          f"b) team [deduped:]: the silent close has an owner record; got {recs}")

    # c) control: a plain team skip was already journaled
    h = Harness()
    h.run("dj-c", "[no-send]\nvisible\n", tier="team")
    check(closed_silently(h) and len(records(h)) == 1,
          f"c) team [no-send]: closed and recorded; got {h.results} {records(h)}")

    # d, e) owner results are not guarded: closed as #5340 closes them, no record
    for n, body in (("d", f"[dm-only]\n[channel: {OTHER}]\n[no-send]\nvisible\n"),
                    ("e", f"[channel: {OTHER}]\n[no-send]\nvisible\n")):
        h = Harness()
        h.run(f"dj-{n}", body)
        check(len(h.results) == 1 and closed_silently(h)
              and h.results[0].get("body") == "[no-send]",
              f"{n}) owner: one lease-close POST, canonical body; got {h.results}")
        check(records(h) == [], f"{n}) owner: no suppression record; got {records(h)}")
        check(h.archived(f"dj-{n}"), f"{n}) owner: archived")

    # f) _quarantine_unsent reaches the delivery verdict, not the guard's
    h = Harness()
    h.task("dj-f")
    rfile = h.mod.RESULTS_DIR / "dj-f.txt"
    rfile.write_text(f"[channel: {OTHER}]\n[no-send]\nvisible\n", encoding="utf-8")
    h.mod._quarantine_unsent(rfile, "dj-f", "dj-f")
    lines = [m for m in h.logs if "dj-f" in m]
    check(any("suppressed ([no-send])" in m and "is archived" in m for m in lines),
          f"f) owner [channel:]+[no-send]: archived as suppressed; got {lines}")
    check(not any("marked [channel:" in m for m in lines) and h.archived("dj-f"),
          f"f) owner [channel:]+[no-send]: never quarantined as a redirect; got {lines}")
    h = Harness()
    h.task("dj-g", tier="team")
    rfile = h.mod.RESULTS_DIR / "dj-g.txt"
    body = f"[dm-only]\n[channel: {OTHER}]\n[no-send]\nvisible\n"
    rfile.write_text(body, encoding="utf-8")
    h.mod._quarantine_unsent(rfile, "dj-g", "dj-g")
    lines = [m for m in h.logs if "dj-g" in m]
    check(any("suppressed ([no-send])" in m for m in lines) and h.archived("dj-g"),
          f"f) team [dm-only]+[channel:]+[no-send]: archived as suppressed; got {lines}")
    recs = records(h)
    check(len(recs) == 1 and recs[0].get("suppressed_body") == body.strip(),
          f"f) team [dm-only]+[channel:]+[no-send]: recorded; got {recs}")

    print(f"\n{'FAIL' if FAILS else 'PASS'}: {len(FAILS)} failure(s)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
