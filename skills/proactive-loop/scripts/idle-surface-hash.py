#!/usr/bin/env python3
"""Canonical dedup key for the proactive-loop idle surface (step 6.5).

The surface posts a held-list once per *changed set*. Hashing the rendered
sentence re-fires it on every re-wording, so the key carries ids and blockers
only — no prose, counts or dates.

Existing as a script is the point: while the rule lived in skill prose the hash
was built ad hoc by the agent each pass, and an agent handed "sha1 the
held-list" naturally hashes the sentence it was about to send.

  echo '[["3166","owner"],["3274","owner"]]' \\
      | idle-surface-hash.py --state <workspace>/state/idle-streak.json
  -> post   c0ffee...        (differs from last_surfaced_hash)
  -> quiet  c0ffee...        (unchanged)

`--commit` records the hash so the next identical set stays quiet.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import sys
import time
from pathlib import Path


def _token(s) -> str:
    """Case- and whitespace-insensitive."""
    return " ".join(str(s or "").split()).casefold()


def _blocker(s) -> str:
    """Leading token only. `gated_on` names WHO the item waits on, not why, so
    two descriptions of one blocker must not produce two keys."""
    t = _token(s).split(":")[0].split()
    return t[0] if t else ""


def canonical_key(items) -> str:
    """`id:blocker` lines, sorted. Order- and wording-independent by construction.

    `gated_on` is reduced to its leading token — `owner`, `ci`, `upstream`,
    `peer-review` — so a blocker CHANGING re-surfaces while re-describing the
    same blocker does not. `id` is NOT reduced: it must already be a stable
    identifier (a PR number, a fixed slug), never a rendered description.
    """
    out = []
    for it in items:
        if isinstance(it, dict):
            ident, gate = it.get("id"), it.get("gated_on", "")
        else:
            ident, gate = (list(it) + [""])[:2]
        ident = _token(ident)
        if not ident:
            raise ValueError(f"held-list entry has no id: {it!r}")
        # A wrong/renamed gate key would otherwise reduce to "", leaving a key
        # that is stable and add-sensitive but never moves when a blocker does.
        blocker = _blocker(gate)
        if not blocker:
            raise ValueError(f"held-list entry has no gated_on: {it!r}")
        out.append(f"{ident}:{blocker}")
    return "\n".join(sorted(set(out)))


def held_hash(items) -> str:
    return hashlib.sha1(canonical_key(items).encode("utf-8")).hexdigest()[:16]


def read_state(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text())
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(path: Path, doc: dict) -> None:
    """Per-PID staging: several loop processes may publish this file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".json.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(doc))
    os.replace(tmp, path)


def record_outcome(path: Path, outcome: str) -> dict:
    """Maintain `streak` and the two cumulative totals, under an exclusive lock.

    Counters, unlike `last_surfaced_hash`, cannot use last-writer-wins: two
    processes that both read total=5 would both write 6 and one pass would
    vanish. The hash path is unaffected — replacing it with a stale-but-valid
    hash costs at most one extra surface.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(".json.lock")
    with open(lock, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            doc = read_state(path)
            noop = outcome == "noop"
            doc["streak"] = int(doc.get("streak") or 0) + 1 if noop else 0
            key = "noop_total" if noop else "substantive_total"
            doc[key] = int(doc.get(key) or 0) + 1
            doc["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            write_state(path, doc)
            return doc
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--state", required=True, help="path to state/idle-streak.json")
    ap.add_argument("--commit", action="store_true",
                    help="record the hash when it differs")
    ap.add_argument("--items", help="JSON held-list; default reads stdin")
    ap.add_argument("--pass-outcome", choices=("substantive", "noop"),
                    help="record this pass and return; maintains streak + totals")
    a = ap.parse_args(argv)

    # Keyed on input ARRIVING, not isatty(): under cron stdin is a pipe even
    # when nothing is sent, and an isatty() gate would block on that read.
    raw = a.items if a.items is not None else (
        "" if sys.stdin.isatty() else sys.stdin.read())

    # A substantive pass has no held-list, and the counters must still move.
    if a.pass_outcome and not raw.strip():
        doc = record_outcome(Path(a.state), a.pass_outcome)
        print(f"{a.pass_outcome} streak={doc['streak']} "
              f"noop_total={doc.get('noop_total', 0)} "
              f"substantive_total={doc.get('substantive_total', 0)}")
        return 0

    try:
        items = json.loads(raw)
    except ValueError as e:
        print(f"error: held-list is not JSON ({e})", file=sys.stderr)
        return 2
    if not isinstance(items, list):
        print("error: held-list must be a JSON array", file=sys.stderr)
        return 2

    try:
        h = held_hash(items)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    path = Path(a.state)
    doc = read_state(path)
    changed = doc.get("last_surfaced_hash") != h
    if changed and a.commit:
        doc["last_surfaced_hash"] = h
        doc["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        write_state(path, doc)
    if a.pass_outcome:
        record_outcome(path, a.pass_outcome)
    print(f"{'post' if changed else 'quiet'} {h}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
