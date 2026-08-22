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

# ── wiring: the bridge delegates at its choke points ───────────────────────
src = (REPO / "src" / "discord-bridge.py").read_text()
tree = ast.parse(src)
# Attribute ACCESSES, not just direct-call funcs: the failure path calls
# through a conditional expression ((_drd.a if x else _drd.b)(...)).
calls = set()
for node in ast.walk(tree):
    if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == "_drd"):
        calls.add(node.attr)
for fn in ("is_delivered", "claim_for_send", "confirm", "unknown", "failed_terminal"):
    check(fn in calls, f"bridge calls _drd.{fn}() (delegation wired)")
mark_calls = [n for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
              and n.func.id == "_mark_delivered"]
check(not mark_calls,
      "bridge no longer WRITES private sentinel state (_mark_delivered uncalled)")
check("_clear_delivered" in src,
      "legacy sentinel cleanup retained for the migration window")

print(f"\n{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
