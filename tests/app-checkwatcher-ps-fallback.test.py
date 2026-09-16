#!/usr/bin/env python3
"""Sutando.app's watcher check must not read a broken `pgrep` as a dead watcher.

Measured 2026-09-13: with sysmond unreachable, `pgrep -f watch-tasks` printed nothing on
stdout and 'Cannot get process list' on stderr; the app discarded stderr, concluded the
watcher was dead, raised a HUD and typed 'watcher' into the core every cycle while the
watcher ran the whole time. The check now reads pgrep's stderr and exit status, falls back
to /bin/ps, and treats "neither could answer" as unknown rather than dead."""
import pathlib
import re
import sys

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "Sutando" / "main.swift"
text = SRC.read_text()
fn = re.search(r"func watcherProcessSeen\(\) -> Bool\? \{(.*?)\n    \}\n", text, re.S)
match_fn = re.search(r"func watcherLineMatches\(.*?\{(.*?)\n    \}\n", text, re.S)
checks = {
    "watcherProcessSeen exists and returns an optional (unknown is a third state)": fn is not None,
    "pgrep's stderr is captured, not discarded": fn is not None and "proc.standardError = errPipe" in fn.group(1)
        and "proc.standardError = FileHandle.nullDevice" not in fn.group(1),
    "only exit 1 with empty stderr means 'no match'": fn is not None and "terminationStatus == 1 && err.isEmpty" in fn.group(1),
    "a failed pgrep falls back to /bin/ps": fn is not None and '"/bin/ps"' in fn.group(1) and "watch-tasks" in fn.group(1),
    "checkWatcher treats nil as unknown and does not alert": "case .none:" in text and "not alerting on an unknown" in text,
    "checkWatcher alerts only on an explicit false": "case .some(false): break" in text,
    # #4269 review (yixuan-ag2): a bare "watch-tasks" substring matched ANY
    # process mentioning it, including the checker's own ps-driven ugrep.
    "ps failure is unknown, not a clean 'no match'": fn is not None
        and "ps.terminationStatus != 0" in fn.group(1) and "return nil" in fn.group(1),
    "ps requests pid alongside command (needed to exclude self)": fn is not None
        and '"pid,command"' in fn.group(1),
    "the matcher is a separate, testable function": match_fn is not None,
    "the matcher excludes the checking process's OWN pid": match_fn is not None
        and "linePID != selfPID" in match_fn.group(1),
    "the marker is the FULL script name, not the truncated substring the bug matched on":
        match_fn is not None and '"watch-tasks-stream.sh"' in match_fn.group(1)
        and '"watch-tasks"' not in match_fn.group(1),
    "the marker is anchored at a path/whitespace boundary on BOTH sides (beforeOK/afterOK)":
        match_fn is not None and "beforeOK" in match_fn.group(1) and "afterOK" in match_fn.group(1),
}
fails = [k for k, ok in checks.items() if not ok]
for k, ok in checks.items(): print(("ok   " if ok else "FAIL ") + k)
print("all checks pass" if not fails else f"FAILED ({len(fails)})"); sys.exit(1 if fails else 0)
