#!/usr/bin/env python3
"""Operator recovery for the delivery outbox: list / inspect / requeue.

PARKED is a durable terminal state and nothing in production could lift it —
`requeue_item` existed with test callers only. This is the reachable surface.
It owns no policy: every transition is `outbox`'s, so the CLI and any other
caller cannot drift apart.

src-canonical (see packages/ag2-sparrow/tools/sync_from_src.py MAP) and vendored
into the package, where `ag2-sparrow-outbox` exposes it. `sutando outbox`
delegates here rather than re-implementing.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path
from typing import Optional

# This module is src-canonical AND vendored into ag2_sparrow, so it must import
# both ways. Relative first: it fails unambiguously when there is no package.
try:
    from . import outbox, undelivered_quarantine
except ImportError:
    import outbox
    import undelivered_quarantine


def _default_operator() -> str:
    """Who to record. An unattended caller must still be attributable."""
    return (os.environ.get("SUTANDO_OPERATOR")
            or os.environ.get("SUDO_USER")
            or _login_name()
            or "unknown")


def _login_name() -> Optional[str]:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 — no passwd entry (container): not fatal
        return None


def _emit(obj, as_json: bool, where=None) -> None:
    if as_json:
        print(json.dumps(obj, sort_keys=True, default=str))
        return
    if isinstance(obj, list):
        if not obj:
            # Name the root: an absent root and an empty one both list nothing,
            # and "(no items)" alone cannot tell an operator which they hit.
            print(f"(no items in {where})" if where is not None else "(no items)")
            return
        for rec in obj:
            print(f"{rec.get('status','?'):<10} "
                  f"attempts={rec.get('attempts',0)} "
                  f"epoch={rec.get('resend_epoch',0)} "
                  f"{rec.get('item_id','?')}"
                  + (f"  reason={rec['reason']}" if rec.get("reason") else ""))
        return
    for k in sorted(obj):
        print(f"{k}: {obj[k]}")


def cmd_list(args) -> int:
    _emit(outbox.list_items(args.root, args.status), args.json, where=args.root)
    return 0


def cmd_inspect(args) -> int:
    rec = outbox.read_item(args.root, args.item_id)
    if rec is None:
        print(f"no such item: {args.item_id}", file=sys.stderr)
        return 2
    claim = outbox.read_delivery_claim(args.root, args.item_id)
    rec = dict(rec)
    rec["claim"] = f"{claim.drainer_id} pid={claim.pid} ({claim.state})" if claim else None
    _emit(rec, args.json)
    return 0


def cmd_requeue(args) -> int:
    """Exit 0 only when this call performed the transition; 3 = nothing to do.

    A distinct code matters for the idempotent re-run: "already queued" is not
    a failure, and a script must be able to tell it from "I recovered it".
    """
    result = outbox.requeue_item(
        args.root, args.item_id,
        reset_attempts=args.reset_attempts,
        operator=args.operator or _default_operator(),
        reason=args.reason)
    payload = {"item_id": args.item_id, "result": result.value}
    if result is outbox.RequeueOutcome.REQUEUED:
        payload["resend_epoch"] = outbox.resend_epoch_for(args.root, args.item_id)
        # The record is only half the recovery: the BODY was moved out of the
        # drain's view, and nothing re-reads the quarantine directory.
        results_dir = args.results_dir or Path(args.root).parent
        outcome, path = undelivered_quarantine.restore(results_dir, args.item_id)
        payload["body"] = outcome.value
        payload["body_path"] = str(path) if path else None
    _emit(payload, args.json)
    if result is outbox.RequeueOutcome.REQUEUED:
        return 0
    return 2 if result is outbox.RequeueOutcome.ABSENT else 3


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ag2-sparrow-outbox",
        description="Inspect and recover parked outbound deliveries.")
    p.add_argument("--root", type=Path, required=True,
                   help="outbox root (the directory holding .items/)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="cmd", required=True)

    ls = sub.add_parser("list", help="list items")
    ls.add_argument("--status", help="filter, e.g. PARKED")
    ls.set_defaults(func=cmd_list)

    ins = sub.add_parser("inspect", help="show one item and its claim")
    ins.add_argument("item_id")
    ins.set_defaults(func=cmd_inspect)

    rq = sub.add_parser("requeue", help="PARKED -> QUEUED for one item")
    rq.add_argument("item_id")
    rq.add_argument("--reset-attempts", action="store_true",
                    help="restore the full attempt budget (default: keep the "
                         "count, so one more failure re-parks)")
    rq.add_argument("--results-dir", type=Path,
                    help="override where result bodies live; defaults to the "
                         "outbox root's parent, which is where every lane "
                         "puts it (RESULTS_DIR/.outbox-*)")
    rq.add_argument("--operator", help="recorded as who did it")
    rq.add_argument("--reason", help="recorded as why")
    rq.set_defaults(func=cmd_requeue)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
