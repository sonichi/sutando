#!/usr/bin/env python3
"""Soak census for HMAC task envelopes: the read-only measurement behind the
"writer census reaches zero" gate.

The #3014 rollout is soak-first: consumers warn on `unsigned` and only flip
to enforcement once every live writer stamps. This tool produces that
evidence — it verifies every task file in `tasks/` and `tasks/archive/`
(bounded by --days against the id/header timestamp) and reports verdict
counts plus a per-`source:` breakdown, so the remaining unsigned writers are
named by source instead of guessed. Verification-only: uses `verify_text`
(read-only `load_key`), never creates the key, never modifies a file.

CLI: `task_envelope_census.py [--days N] [--json]`
Exit 0 always (telemetry, not a gate); `--json` for machine readers.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from task_envelope import verify_text  # noqa: E402
from workspace_default import resolve_workspace  # noqa: E402

# (?!\d): an id with MORE digits (e.g. an 18-digit gateway id) is not an
# epoch — its first 13 digits parse as a far-future date, pinning it in-window.
_ID_MS = re.compile(r"task-(\d{13})(?!\d)")


def _task_epoch(name: str, text: str, mtime: float) -> float:
    m = _ID_MS.search(name)
    if m:
        return int(m.group(1)) / 1000.0
    for line in text.split("\n")[:6]:
        if line.startswith("timestamp:"):
            try:
                from datetime import datetime, timezone
                return datetime.fromisoformat(
                    line.split(":", 1)[1].strip().replace("Z", "+00:00")
                ).astimezone(timezone.utc).timestamp()
            except ValueError:
                break
    return mtime


def _source_of(text: str) -> str:
    for line in text.split("\n")[:12]:
        if line.startswith("source:"):
            return line.split(":", 1)[1].strip() or "(empty)"
    return "(none)"


def census(workspace: Path | None = None, days: float = 7.0) -> dict:
    ws = workspace or resolve_workspace()
    cutoff = time.time() - days * 86400
    verdicts: Counter = Counter()
    by_source: dict = defaultdict(Counter)
    scanned = 0
    # rglob: the archiver nests monthly dirs (tasks/archive/YYYY-MM/) — a
    # flat glob silently drops those writers from the census (review blocker).
    for d in (ws / "tasks",):
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("task-*.txt")):
            # One failure boundary for read AND stat: the archiver can move
            # the file between them, and a vanished file is a skip, not an abort.
            try:
                text = p.read_text(encoding="utf-8")
                mtime = p.stat().st_mtime
            except OSError:
                continue
            if _task_epoch(p.name, text, mtime) < cutoff:
                continue
            scanned += 1
            v = verify_text(text, ws)["verdict"]
            verdicts[v] += 1
            by_source[_source_of(text)][v] += 1
    return {
        "scanned": scanned,
        "days": days,
        "verdicts": dict(verdicts),
        "by_source": {s: dict(c) for s, c in sorted(by_source.items())},
        "unsigned_sources": sorted(
            s for s, c in by_source.items() if c.get("unsigned", 0) > 0),
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--json", action="store_true")
    # Explicit workspace: the default resolver is checkout-relative, which is
    # wrong when this file is invoked from a worktree/bundle copy.
    ap.add_argument("--workspace", type=Path, default=None)
    args = ap.parse_args(argv[1:])
    r = census(workspace=args.workspace, days=args.days)
    if args.json:
        print(json.dumps(r, indent=1))
        return 0
    print(f"envelope census — {r['scanned']} task file(s), last {r['days']:g}d")
    for v in ("verified", "unsigned", "invalid", "unverifiable"):
        if r["verdicts"].get(v):
            print(f"  {v:12} {r['verdicts'][v]}")
    for s, c in r["by_source"].items():
        print(f"  source {s}: " + ", ".join(
            f"{k}={n}" for k, n in sorted(c.items())))
    if r["unsigned_sources"]:
        print("  UNSIGNED writers still live (census gate not met): "
              + ", ".join(r["unsigned_sources"]))
    else:
        print("  census gate MET within window — no unsigned writers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
