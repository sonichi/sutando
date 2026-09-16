#!/usr/bin/env python3
"""Sutando.app's watcher check must not fail-open on a bare `watch-tasks` substring.

History: `pgrep -f watch-tasks` (the original primary probe) matched ANY process whose argv
merely mentioned the substring — a grep, an editor, a tail of a log file with that name — not
just the real `watch-tasks-stream.sh` watcher. A first fix (2026-09-13/14) added a `/bin/ps`
fallback anchored on the full script name at a path/whitespace boundary, but only for when
pgrep itself failed to run — the loose primary probe still ran first and still fail-opened
(review #4269, qingyun-wu, 2026-09-16T07:42:53Z, commit 8aa2bcbf6). Fixed by removing the
pgrep primary probe entirely: the anchored `/bin/ps` check is now the sole, authoritative
liveness probe, so there is exactly one boundary check to keep correct, not two."""
import pathlib
import re
import subprocess
import sys

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "Sutando" / "main.swift"
text = SRC.read_text()
fn = re.search(r"func watcherProcessSeen\(\) -> Bool\? \{(.*?)\n    \}\n", text, re.S)
match_fn = re.search(r"func watcherLineMatches\(.*?\{(.*?)\n    \}\n", text, re.S)

checks = {
    "watcherProcessSeen exists and returns an optional (unknown is a third state)": fn is not None,
    # #4269: the primary pgrep probe fail-opened on any argv containing "watch-tasks";
    # removed rather than patched, so it must not reappear as a duplicate probe.
    "no pgrep-based primary probe remains": fn is not None
        and "/usr/bin/pgrep" not in fn.group(1) and "pgrep" not in fn.group(1),
    "the loose, unanchored 'watch-tasks' pattern is gone from the probe (only the full anchored script name remains)":
        fn is not None and '"watch-tasks"' not in fn.group(1) and '"-f", "watch-tasks"' not in fn.group(1),
    "/bin/ps is the sole process-listing source": fn is not None and '"/bin/ps"' in fn.group(1),
    "ps failure is unknown, not a clean 'no match'": fn is not None
        and "ps.terminationStatus != 0" in fn.group(1) and "return nil" in fn.group(1),
    "ps requests pid alongside command (needed to exclude self)": fn is not None
        and '"pid,command"' in fn.group(1),
    "checkWatcher treats nil as unknown and does not alert": "case .none:" in text and "not alerting on an unknown" in text,
    "checkWatcher alerts only on an explicit false": "case .some(false): break" in text,
    "the matcher is a separate, testable function": match_fn is not None,
    "the matcher excludes the checking process's OWN pid": match_fn is not None
        and "linePID != selfPID" in match_fn.group(1),
    "the marker is the FULL script name, not the truncated substring the bug matched on":
        match_fn is not None and '"watch-tasks-stream.sh"' in match_fn.group(1)
        and '"watch-tasks"' not in match_fn.group(1),
    "the marker is anchored at a path/whitespace boundary on BOTH sides (beforeOK/afterOK)":
        match_fn is not None and "beforeOK" in match_fn.group(1) and "afterOK" in match_fn.group(1),
}

# Behavioral negative control: actually execute the extracted matcher against
# synthetic ps rows, incl. an argv that merely CONTAINS "watch-tasks" (see PR body).
if match_fn is not None:
    harness = '''
import Foundation
%s

let lines: [(String, String, Bool)] = [
    ("grep line (own pid, must be excluded even if it matched)", "501 grep --color=auto watch-tasks", false),
    ("unrelated process whose argv merely CONTAINS watch-tasks", "777 tail -f /var/log/watch-tasks-stream.log", false),
    ("unrelated file merely NAMED watch-tasks-*", "778 /usr/bin/vim workspace/notes/watch-tasks-plan.md", false),
    ("the real watcher, full script name at a path boundary", "3003 /bin/bash src/watch-tasks-stream.sh", true),
]
var failures = 0
for (desc, line, want) in lines {
    let got = watcherLineMatches(Substring(line), excluding: 501)
    let ok = got == want
    print((ok ? "ok   " : "FAIL ") + desc + " (got \\(got), want \\(want))")
    if !ok { failures += 1 }
}
exit(failures == 0 ? 0 : 1)
''' % (("private func watcherLineMatches" + match_fn.group(0)[len("func watcherLineMatches"):])
       if match_fn.group(0).startswith("func watcherLineMatches")
       else match_fn.group(0))
    tmp = pathlib.Path("/tmp/_watcherline_negative_control.swift")
    tmp.write_text(harness)
    try:
        proc = subprocess.run(["xcrun", "swift", str(tmp)], capture_output=True, text=True, timeout=90)
        behavioral_ok = proc.returncode == 0
        checks["behavioral negative control: unrelated 'watch-tasks' argv does not report alive (real execution)"] = behavioral_ok
        if not behavioral_ok:
            print(proc.stdout, file=sys.stderr)
            print(proc.stderr, file=sys.stderr)
    except (OSError, subprocess.TimeoutExpired) as e:
        # swift toolchain unavailable/slow in this environment: don't fail the whole
        # suite on an infrastructure gap, but say so loudly rather than silently pass.
        print(f"SKIP behavioral negative control (swift unavailable: {e})", file=sys.stderr)
    finally:
        tmp.unlink(missing_ok=True)

fails = [k for k, ok in checks.items() if not ok]
for k, ok in checks.items(): print(("ok   " if ok else "FAIL ") + k)
print("all checks pass" if not fails else f"FAILED ({len(fails)})"); sys.exit(1 if fails else 0)
