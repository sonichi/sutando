#!/usr/bin/env python3
"""Bump the `attempts:` counter on a task file (restart-safety #4).

Called by `src/watch-tasks-stream.sh` before each `TASK_FILE: <name>`
emit. Implementation notes:

  - Inserts `attempts: 1` BEFORE the `task:` delimiter line for fresh
    tasks (no existing `attempts:` field).
  - Increments existing `attempts:` lines by 1.
  - Position matters: task-file parsers across the codebase stop
    scanning at `task:` (the body delimiter); fields AFTER `task:`
    are invisible to them. The counter must precede.
  - Atomic write via tmp + rename — never leaves a half-written file
    on disk. On any error, log to stderr and exit 0 (a missed bump
    is non-fatal; the agent reads a slightly-stale count).
  - Idempotent on malformed files: a file with no `task:` line is
    left untouched (defensive — non-task files in tasks/ should not
    get a phantom `attempts:` header).

Usage:
  python3 src/task_bump_attempts.py <absolute-path-to-task-file>
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


_ATTEMPTS_RE = re.compile(r"^attempts:\s*(\d+)")


def bump_attempts(file_path: Path) -> int:
    """Bump the `attempts:` counter on `file_path`. Returns the new
    count (or 0 on no-op / error).

    No-op cases (returns 0, file untouched):
      - File missing.
      - File has no `task:` line (malformed / not a task file).
      - Read/write error.
    """
    try:
        raw = file_path.read_text(encoding="utf-8")
    except OSError:
        return 0
    lines = raw.split("\n")
    task_idx = next(
        (i for i, ln in enumerate(lines) if ln.startswith("task:")),
        -1,
    )
    if task_idx < 0:
        # Not a well-formed task file — leave it alone.
        return 0
    attempts_idx = next(
        (i for i in range(task_idx) if lines[i].startswith("attempts:")),
        -1,
    )
    if attempts_idx >= 0:
        m = _ATTEMPTS_RE.match(lines[attempts_idx])
        new_count = (int(m.group(1)) + 1) if m else 1
        lines[attempts_idx] = f"attempts: {new_count}"
    else:
        new_count = 1
        lines.insert(task_idx, "attempts: 1")
    updated = "\n".join(lines)
    try:
        # In-place write (NOT tmp+rename) — per @qingyun-wu and
        # @liususan091219 PR #1049 reviews. The prior tmp+rename
        # approach was atomic but triggered fswatch's `Renamed`
        # event on the destination path: watch-tasks-stream.sh
        # subscribes to `--event Created --event Renamed` for the
        # tasks/ dir, so a rename onto `tasks/<name>.txt` re-fires
        # the watcher's emit-loop → bumper runs again → another
        # rename → another emit → infinite self-trigger loop. Lucy
        # repro'd this on Studio: `attempts=1022` within 60 seconds.
        #
        # In-place write via open(file_path, "w") (truncate + write)
        # only fires an Updated/Modified event, which the watcher
        # is NOT subscribed to. The trade: we lose crash-atomicity.
        # `open(w)` truncates first, so a crash mid-write leaves
        # the file empty and the original content is GONE — this
        # is a real (though tiny-window) data-loss risk for that
        # one task, not a "delays re-processing" risk. Honest
        # mitigations:
        #
        #   1. The bumper is small + fast — the write window is
        #      microseconds for a ~200-byte task file.
        #   2. Task-file parsers fail closed on malformed/empty
        #      files (no `task:` line → no claim). The agent
        #      skips silently rather than processing corrupted
        #      half-data. The task is LOST, not mis-processed.
        #
        # The trade is worth it vs. the definite, frequent self-
        # trigger loop — but the residual data-loss risk is real,
        # not zero. A future watcher-side dedup (`.mjs` `seen` set
        # already does this in the fork) would let us re-introduce
        # atomic tmp+rename — separate PR.
        with open(file_path, "w", encoding="utf-8") as fh:
            fh.write(updated)
    except OSError as e:
        print(f"[task_bump_attempts] write failed for {file_path}: {e}", file=sys.stderr)
        return 0
    return new_count


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: task_bump_attempts.py <task-file-path>", file=sys.stderr)
        return 2
    bump_attempts(Path(argv[1]))
    # Always exit 0 — a missed bump is non-fatal, the watcher must
    # still emit `TASK_FILE: <name>` for the agent.
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
