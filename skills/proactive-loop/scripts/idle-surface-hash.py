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


def canonical_lines(items) -> "list[str]":
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
    return sorted(set(out))


def canonical_key(items) -> str:
    return "\n".join(canonical_lines(items))


def held_hash(items) -> str:
    return hashlib.sha1(canonical_key(items).encode("utf-8")).hexdigest()[:16]


sys.path.insert(0, str(Path(__file__).resolve().parent))
from idle_state import (ABORT, REFUSED, locked_update, read_state,  # noqa: E402
                        write_state)


def record_outcome(path: Path, outcome: str) -> dict:
    """Maintain `streak` and the two cumulative totals, under an exclusive lock.

    Counters, unlike `last_surfaced_hash`, cannot use last-writer-wins: two
    processes that both read total=5 would both write 6 and one pass would
    vanish. The hash path is unaffected — replacing it with a stale-but-valid
    hash costs at most one extra surface.
    """
    def bump(doc):
        noop = outcome == "noop"
        doc["streak"] = int(doc.get("streak") or 0) + 1 if noop else 0
        key = "noop_total" if noop else "substantive_total"
        doc[key] = int(doc.get(key) or 0) + 1
        doc["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return doc

    doc = locked_update(path, bump)
    if doc is REFUSED:
        raise SystemExit(2)
    return doc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--state", required=True, help="path to state/idle-streak.json")
    ap.add_argument("--commit", action="store_true",
                    help="record the hash when it differs")
    ap.add_argument("--items", help="JSON held-list; default reads stdin")
    ap.add_argument("--pass-outcome", choices=("substantive", "noop"),
                    help="RECORD-ONLY: maintain streak + totals and exit; "
                         "reads no stdin and ignores --items")
    a = ap.parse_args(argv)

    # Record-only, and it must return BEFORE any stdin access: under cron stdin
    # is an open pipe that is never written, so a read here blocks forever.
    if a.pass_outcome:
        doc = record_outcome(Path(a.state), a.pass_outcome)
        print(f"{a.pass_outcome} streak={doc['streak']} "
              f"noop_total={doc.get('noop_total', 0)} "
              f"substantive_total={doc.get('substantive_total', 0)}")
        return 0

    raw = a.items if a.items is not None else sys.stdin.read()
    try:
        items = json.loads(raw)
    except ValueError as e:
        print(f"error: held-list is not JSON ({e})", file=sys.stderr)
        return 2
    if not isinstance(items, list):
        print("error: held-list must be a JSON array", file=sys.stderr)
        return 2

    try:
        lines = canonical_lines(items)
        h = held_hash(items)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    path = Path(a.state)
    seen = {}

    def compare_and_commit(doc):
        # Compare INSIDE the lock: a doc read before acquiring it is stale, so
        # committing it back erases whatever landed in between.
        changed = doc.get("last_surfaced_hash") != h
        prev = doc.get("last_surfaced_ids")
        have_ids = isinstance(prev, list)
        seen["changed"] = changed
        if changed:
            # Without the previous ids a hash change is unauditable: a renamed
            # id and a genuinely new blocker are the same opaque digest move.
            if have_ids:
                now = set(lines)
                before = set(str(x) for x in prev)
                added, gone = sorted(now - before), sorted(before - now)
                print(f"changed: +{added} -{gone}", file=sys.stderr)
            else:
                print("changed: no previous ids recorded (first commit "
                      "or pre-upgrade state)", file=sys.stderr)
        elif a.commit and not have_ids:
            # A legacy state carries the hash but no ids. A quiet pass is the
            # only cheap chance to seed them BEFORE the set moves.
            print("backfilled: recorded ids for an existing hash", file=sys.stderr)
        if not (a.commit and (changed or not have_ids)):
            return ABORT
        doc["last_surfaced_hash"] = h
        doc["last_surfaced_ids"] = lines
        doc["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return None

    if locked_update(path, compare_and_commit) is REFUSED:
        return 2
    print(f"{'post' if seen['changed'] else 'quiet'} {h}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
