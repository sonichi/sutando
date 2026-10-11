#!/usr/bin/env python3
"""A skip marker directly after `[channel:]` on an owner task result is a skip.

The gateway owns the skip decision on the full leading block of the guarded body:

  a) owner `[channel: X]` + [no-send] / [REPLIED] -> lease closed no_send, nothing
     posted to X, no marker text on the wire, archived with the reason logged
  b) owner `[channel: X]` + [deduped:]             -> routed through the dedup plan,
     never posted as text
  c) team  `[channel: X]` + [no-send]              -> unchanged: the guard withholds it
     for private owner review and the lease closes no_send
  d) orphan sweep, owner `[channel: X]` + [no-send] -> lease closed no_send, no marker text
  e) proactive file `[channel: room]` + [no-send]  -> dropped, nothing posted
  f) `[channel: X]` then prose                     -> still re-stitched as a redirect

Loads src/remote-gateway-bridge.py in-process through the #5326 harness (its real
PROACTIVE_CLAIM_GATE), with an isolated workspace and a fake `_req`.
Run: python3 tests/gateway-channel-skip-owner.test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "gw_thread_ask_harness", REPO / "tests" / "gateway-task-result-thread-ask.test.py")
_h = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_h)
Harness, ROOM, OTHER = _h.Harness, _h.ROOM, _h.OTHER
FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def main() -> int:
    # a) owner: a skip after [channel:] closes the lease, posts nothing
    for n, marker in enumerate(("[no-send]", "[REPLIED]")):
        h = Harness()
        left = h.run(f"cs-a{n}", f"[channel: {OTHER}]\n{marker}\nvisible\n")
        wire = json.dumps(h.results)
        check(len(h.results) == 1 and h.results[0].get("no_send") is True
              and h.results[0].get("body") == marker,
              f"a) {marker}: one lease-close POST, no_send, canonical body; got {h.results}")
        check("visible" not in wire and "[channel:" not in wire,
              f"a) {marker}: neither the text nor the redirect reaches the wire; got {wire}")
        check(left == set() and h.archived(f"cs-a{n}"), f"a) {marker}: archived, out of flight")
        check(any(f"(marker {marker.strip('[]')}, lease closed, not sent)" in m for m in h.logs),
              f"a) {marker}: reason logged; got {[m for m in h.logs if f'cs-a{n}' in m]}")

    # b) owner: [deduped:] after [channel:] goes through the dedup plan
    h = Harness()
    h.run("cs-b", f"[channel: {OTHER}]\n[deduped: cs-holder]\nvisible\n")
    check(not any("visible" in json.dumps(r) or "[channel:" in json.dumps(r) for r in h.results),
          f"b) [deduped:]: never posted as text; got {h.results}")
    check(any("dedup" in m and "cs-b" in m for m in h.logs),
          f"b) [deduped:]: routed through the dedup plan; got {[m for m in h.logs if 'cs-b' in m]}")

    # c) non-owner: the guard still withholds redirect-plus-skip for owner review
    h = Harness()
    h.run("cs-c", f"[channel: {OTHER}]\n[no-send]\nvisible\n", tier="team")
    check(len(h.results) == 1 and h.results[0].get("no_send") is True
          and h.results[0].get("body") == "[no-send]",
          f"c) team: the lease is closed no_send, as on main; got {h.results}")
    check("visible" not in json.dumps(h.results) and "[channel:" not in json.dumps(h.results),
          f"c) team: neither the text nor the redirect reaches the wire; got {h.results}")
    check(any("withheld non-owner result for cs-c" in m and "pending private owner review" in m
              for m in h.logs),
          f"c) team: the withhold is logged; got {[m for m in h.logs if 'cs-c' in m]}")

    # d) orphan sweep reaches the same verdict
    h = Harness()
    h.task("task-cs-d")
    rfile = h.mod.RESULTS_DIR / "task-cs-d.txt"
    rfile.write_text(f"[channel: {OTHER}]\n[no-send]\nvisible\n")
    old = time.time() - h.mod.ORPHAN_GRACE_S - 60
    os.utime(rfile, (old, old))
    h.mod._last_orphan_sweep = 0.0
    h.mod._reconcile_orphan_results(set())
    wire = json.dumps(h.results)
    check(len(h.results) == 1 and h.results[0].get("no_send") is True
          and h.results[0].get("body") == "[no-send]",
          f"d) orphan sweep: one lease-close POST, no_send, canonical body; got {h.results}")
    check("visible" not in wire and "[channel:" not in wire,
          f"d) orphan sweep: neither the text nor the redirect reaches the wire; got {wire}")

    # e) proactive: a skip after [channel:] drops the file
    h = Harness()
    (h.ws / "state" / "last-owner-activity.json").write_text(json.dumps(
        {"ts": int(time.time()), "channel": "ag2space", "summary": "t"}))
    (h.mod.RESULTS_DIR / "proactive-1.txt").write_text(f"[channel: {ROOM}]\n[no-send]\nvisible\n")
    h.mod._post_proactive()
    posts = [c[2] for c in h.calls if c[1] == "/v1/room" and (c[2] or {}).get("op") == "message"]
    check(posts == [], f"e) proactive: nothing posted; got {posts}")
    check(h.mod._proactive_route(f"[channel: {ROOM}]\n[no-send]\nvisible") == ("drop", None, ""),
          "e) proactive: _proactive_route drops it")

    # f) an ordinary redirect is untouched
    h = Harness()
    h.run("cs-f", f"[channel: {OTHER}]\nmoved\n")
    p = h.results[0] if h.results else {}
    check(p.get("body") == f"[channel: {OTHER}]\nmoved" and not p.get("no_send"),
          f"f) redirect re-stitched as today; got {p}")

    print(f"\n{'FAIL' if FAILS else 'PASS'}: {len(FAILS)} failure(s)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
