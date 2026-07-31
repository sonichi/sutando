#!/usr/bin/env python3
"""Tests for session-recap extract.py --tail-bytes (boot-cost bound) — hermetic.

A synthetic transcripts dir (via the --transcripts-dir CLI hook — the
supported override surface; no env var) stands in for the real project dir,
so no live workspace or 58 MB transcript is needed. Pinned behaviors:
  1. --tail-bytes 0 (default) reads the whole file — unchanged behavior
  2. --tail-bytes N smaller than the file reads only the tail: the dump
     KEEPS the newest events (a catchup's open loops live at the END),
     drops the oldest, announces the bound with a marker line
  3. the seek lands mid-line and the partial first line is discarded, not
     emitted as a corrupt event
  4. --tail-bytes larger than the file behaves exactly like 0 (no marker)
  5. --transcripts-dir override is honored (what makes this test — and A/B
     timing runs from worktrees — possible at all)
  6. a seek landing EXACTLY on a record boundary keeps that complete
     record — the partial-line discard must not eat it (#2454 review)

Run: python3 tests/session-recap-extract-tail.test.py   (exit 0 pass / 1 fail)
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import time
from contextlib import redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "skills" / "session-recap" / "scripts" / "extract.py"

spec = importlib.util.spec_from_file_location("recap_extract", SCRIPT)
extract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(extract)

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else f" — {detail}"))
    if not cond:
        failures.append(name)


tmp = Path(tempfile.mkdtemp(prefix="recap-tail-"))


def event(i: int, ts: str) -> str:
    return json.dumps({"type": "user", "timestamp": ts,
                       "message": {"content": f"message number {i:04d}"}})


# Two main transcripts: an older "previous" session (the dump target) and a
# newer "current" one, so --session last resolves to the previous file.
prev = tmp / "aaaa-prev.jsonl"
prev.write_text("\n".join(event(i, f"2026-07-30T10:{i % 60:02d}:00Z")
                          for i in range(400)) + "\n")
now = time.time()
os.utime(prev, (now - 60, now - 60))
cur = tmp / "bbbb-cur.jsonl"
cur.write_text(event(0, "2026-07-31T09:00:00Z") + "\n")
os.utime(cur, (now, now))

def run(*extra: str) -> str:
    # In-process (importlib + argv patch, the repo's coverage-visible pattern):
    # subprocess invocations are invisible to the diff-coverage gate.
    argv, out = ["extract.py", "dump", "--session", "last",
                 "--filter", "user", "--max-chars", "0",
                 "--transcripts-dir", str(tmp), *extra], io.StringIO()
    old_argv = sys.argv
    sys.argv = argv
    try:
        with redirect_stdout(out):
            extract.main()
    finally:
        sys.argv = old_argv
    return out.getvalue()


# 1. default (no bound) reads everything
full = run()
check("default: whole file (first event present)", "message number 0000" in full)
check("default: whole file (last event present)", "message number 0399" in full)
check("default: no tail marker", "[tail:" not in full)

# 2./3. bounded read keeps the NEWEST end, drops the oldest, marks the bound,
# and emits no partial/corrupt first line
size = prev.stat().st_size
bounded = run("--tail-bytes", str(size // 4))
check("tail: newest event kept", "message number 0399" in bounded)
check("tail: oldest event dropped", "message number 0000" not in bounded)
check("tail: bound announced", f"[tail: last {size // 4} bytes of {size}" in bounded)
first_event_line = next(line for line in bounded.splitlines() if "USER:" in line)
check("tail: first emitted event is intact (seek discards the partial line)",
      first_event_line.rstrip().endswith(tuple(f"{i:04d}" for i in range(400))),
      repr(first_event_line[-40:]))
check("tail: shared suffix identical to unbounded dump",
      full.splitlines()[-1] == bounded.splitlines()[-1])

# 4. bound larger than the file = whole file, no marker
big = run("--tail-bytes", str(size * 10))
check("oversized bound: identical to default", big == full)

# 6. exact-boundary seek: every record is the same length L (fixed-width
# fields), so tail_bytes = k*(L+1) puts the seek exactly at the start of
# record 400-k — the byte before it is the previous record's newline. The
# discard must NOT eat that complete record: expect exactly k records,
# starting at 400-k. (An unconditional readline() yields k-1, starting at
# 400-k+1 — the off-by-one-record loss the reviewer's boundary probe found.)
line_len = len(event(0, "2026-07-30T10:00:00Z")) + 1  # +1 = newline
k = 10
at_boundary = run("--tail-bytes", str(k * line_len))
boundary_events = [ln for ln in at_boundary.splitlines() if "USER:" in ln]
check("boundary seek: complete record at the seek point is kept",
      f"message number {400 - k:04d}" in at_boundary, at_boundary[:200])
check("boundary seek: exactly k records survive",
      len(boundary_events) == k, f"got {len(boundary_events)}")

# ...and the mid-line case still discards the partial: 3 bytes past the
# boundary lands inside record 400-k, which must be dropped as partial.
mid_line = run("--tail-bytes", str(k * line_len - 3))
mid_events = [ln for ln in mid_line.splitlines() if "USER:" in ln]
check("mid-line seek: partial record dropped, k-1 complete records remain",
      len(mid_events) == k - 1
      and f"message number {400 - k:04d}" not in mid_line
      and f"message number {400 - k + 1:04d}" in mid_line,
      f"got {len(mid_events)}")

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All extract --tail-bytes checks passed.")
