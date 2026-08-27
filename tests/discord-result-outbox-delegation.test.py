#!/usr/bin/env python3
"""Discord result delivery binds the shared outbox (#3279 action 2).

Direct contract tests on src/discord_result_delivery.py — the state machine
the sentinel files used to improvise — plus wiring assertions that the bridge
calls the module at its choke points and writes NO private delivery state.
"""
from __future__ import annotations

import ast
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))

import discord_result_delivery as drd  # noqa: E402
import outbox  # noqa: E402

failures: list[str] = []


def check(cond, label):
    print(("ok: " if cond else "FAIL: ") + label)
    if not cond:
        failures.append(label)


with tempfile.TemporaryDirectory() as td:
    rd = Path(td) / "results"
    rd.mkdir()

    # happy path: claim -> confirm -> delivered; second cycle refuses
    tok = drd.claim_for_send(rd, "task-A")
    check(tok is not None, "fresh item: claim_for_send returns a token")
    check(drd.confirm(rd, tok, "chan-1") is True, "confirm records CONFIRMED")
    check(drd.is_delivered(rd, "task-A"), "delivered: is_delivered True")
    check(drd.claim_for_send(rd, "task-A") is None,
          "delivered item is never re-claimed (crash-window idempotency)")

    check(drd.is_delivered(rd, "task-Z") is False,
          "unknown item, no legacy dir: not delivered")

    # double-claim race: a held claim yields None for the second caller
    t1 = drd.claim_for_send(rd, "task-B")
    check(t1 is not None and drd.claim_for_send(rd, "task-B") is None,
          "held claim: second claim_for_send is refused")

    # one-shot failure: terminal park, visible, never silently re-sendable
    check(drd.failed_terminal(rd, t1) is True, "failed_terminal completes")
    check(outbox.item_status(drd.result_backend(rd).root, "task-B") == "PARKED",
          "failed send parks (operator-visible, matches archive-on-failure)")
    check(drd.claim_for_send(rd, "task-B") is None,
          "parked item refuses a fresh cycle")

    # ambiguous outcome: parked as outcome-unknown, never auto-retried
    t2 = drd.claim_for_send(rd, "task-C")
    check(drd.unknown(rd, t2) is True and
          outbox.item_status(drd.result_backend(rd).root, "task-C") == "PARKED",
          "OUTCOME_UNKNOWN parks — a maybe-received reply is never re-sent")

    # legacy sentinel honored READ-ONLY for the migration window
    legacy = Path(td) / "sentinels"
    legacy.mkdir()
    (legacy / "task-L.sentinel").touch()
    check(drd.is_delivered(rd, "task-L", legacy),
          "legacy sentinel alone reads as delivered (no double-send on migrate)")
    check(outbox.item_status(drd.result_backend(rd).root, "task-L") is None,
          "…and reading it writes NOTHING to the outbox (read-only compat)")

    # retry-variant contract (the two-line follow-up API): re-readied, then
    # parked at the attempt cap — never an infinite retry loop
    t3 = drd.claim_for_send(rd, "task-R")
    for i in range(drd.PARK_AT_ATTEMPTS):
        check(drd.failed(rd, t3) is True, f"failed() attempt {i+1} completes")
        t3 = drd.claim_for_send(rd, "task-R")
        if t3 is None:
            break
    check(t3 is None and outbox.item_status(
              drd.result_backend(rd).root, "task-R") == "PARKED",
          "failed() re-readies until the cap, then parks (no infinite retry)")

    # unreadable legacy sentinel dir (permission denied): OSError arm -> False
    import os as _os
    locked = Path(td) / "locked-sentinels"
    locked.mkdir()
    _os.chmod(locked, 0)
    try:
        check(drd.is_delivered(rd, "task-U", locked) is False,
              "permission-denied sentinel dir: False, never raises")
    finally:
        _os.chmod(locked, 0o755)

    # the vendored twin ships the same item_status — exercise it directly
    import ag2_sparrow.outbox as twin_outbox
    rt = Path(td) / "twin-root"
    check(twin_outbox.item_status(rt, "task-T") is None,
          "twin item_status: no record -> None")
    tb = drd.result_backend(rd)
    check(twin_outbox.item_status(tb.root, "task-A") == "DELIVERED",
          "twin item_status reads the same record shape")
    tp = twin_outbox._item_path(rt, "task-T2")
    tp.parent.mkdir(parents=True)
    tp.write_text('{"status": "READY"}')
    _os.chmod(tp, 0)
    try:
        check(twin_outbox.item_status(rt, "task-T2") is None,
              "twin item_status: unreadable record -> None, never raises")
    finally:
        _os.chmod(tp, 0o644)

    # item_status total-exception arm: unreadable record file -> None
    r2 = Path(td) / "corrupt-root"
    cp = outbox._item_path(r2, "task-C2")
    cp.parent.mkdir(parents=True)
    cp.write_text('{"status": "READY"}')
    _os.chmod(cp, 0)
    try:
        check(outbox.item_status(r2, "task-C2") is None,
              "unreadable item record: item_status None, never raises")
    finally:
        _os.chmod(cp, 0o644)

# ── wiring: the bridge delegates at its choke points ───────────────────────
src = (REPO / "src" / "discord-bridge.py").read_text()
with tempfile.TemporaryDirectory() as td:
    rd = Path(td) / "results"
    rd.mkdir()
    # crash window: parked (terminal) but the archive never ran. On restart
    # the caller must be able to detect PARKED and archive — not loop.
    tok = drd.claim_for_send(rd, "task-P")
    drd.failed_terminal(rd, tok)
    check(drd.is_parked(rd, "task-P"), "parked item: is_parked True")
    check(drd.claim_for_send(rd, "task-P") is None,
          "parked item: claim_for_send still refuses")
    check(not drd.is_delivered(rd, "task-P"),
          "parked is not conflated with delivered")
    check(not drd.is_parked(rd, "task-F"), "fresh item: is_parked False")

tree = ast.parse(src)
# Attribute ACCESSES, not just direct-call funcs: the failure path calls
# through a conditional expression ((_drd.a if x else _drd.b)(...)).
calls = set()
for node in ast.walk(tree):
    if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == "_drd"):
        calls.add(node.attr)
for fn in ("is_delivered", "is_parked", "claim_for_send", "confirm", "unknown", "failed_terminal"):
    check(fn in calls, f"bridge calls _drd.{fn}() (delegation wired)")
import re as _re
m = _re.search(r"is_parked\(RESULTS_DIR, task_id\):\n(.*?)continue", src, _re.S)
check(bool(m) and "_archive_delivered_pair" in m.group(1),
      "bridge archives the pair inside the is_parked branch")
mark_calls = [n for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
              and n.func.id == "_mark_delivered"]
check(not mark_calls,
      "bridge no longer WRITES private sentinel state (_mark_delivered uncalled)")
check("_clear_delivered" in src,
      "legacy sentinel cleanup retained for the migration window")

print(f"\n{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
