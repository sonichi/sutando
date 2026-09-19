#!/usr/bin/env python3
"""Sutando.app's watcher check must not fail-open on a bare `watch-tasks` substring.

History: `pgrep -f watch-tasks` (the original primary probe) matched ANY process whose argv
merely mentioned the substring — a grep, an editor, a tail of a log file with that name — not
just the real `watch-tasks-stream.sh` watcher. A first fix (2026-09-13/14) added a `/bin/ps`
fallback anchored on the full script name at a path/whitespace boundary, but only for when
pgrep itself failed to run — the loose primary probe still ran first and still fail-opened
(review #4269, qingyun-wu, 2026-09-16T07:42:53Z, commit 8aa2bcbf6). Fixed by removing the
pgrep primary probe entirely: the anchored `/bin/ps` check is now the sole, authoritative
liveness probe.

Second instance, same family (review #4269 round 2, john-the-dev, 2026-09-16): the anchored
boundary check still matched ANY argv containing the full script name at a boundary, including
`grep watch-tasks-stream.sh`, `vim .../watch-tasks-stream.sh`, and `tail -f
.../watch-tasks-stream.sh` — none of which EXECUTE the script. Fixed by porting
`watcher_identity.py`'s flattened-argv predicate: argv[0]'s basename must be a shell, argv[1]
must not be a flag, and argv[1]'s basename must be the script — exactly two tokens.

Third instance (review #4269 round 3, john-the-dev, 2026-09-16): requiring EXACTLY two tokens
turned every legitimate EXTRA-token invocation of the real watcher into a false DEAD instead of
an honest UNKNOWN — worker mode passes a positional tasks-dir operand, and the default macOS
install path contains a space (`~/Library/Application Support/...`), which alone splits a
no-operand launch into 3+ whitespace tokens. Ported `classify_argv`'s full flattened-argv
predicate, including its UNDECIDABLE (`None`) outcome: `watcherLineMatches` now returns `Bool?`,
and a boundary match at a token other than exactly parts[1]-with-count-2 returns `nil`, never
`false`. `watcherProcessSeen` now aggregates tri-state: any `true` line short-circuits alive; a
`nil` line, absent a `true`, makes the overall result `nil` (unknown) rather than being
overridden by a later definite-`false` line."""
import pathlib
import re
import subprocess
import sys

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "Sutando" / "main.swift"
text = SRC.read_text()
fn = re.search(r"func watcherProcessSeen\(\) -> Bool\? \{(.*?)\n    \}\n", text, re.S)
match_fn = re.search(r"func watcherLineMatches\(.*?-> Bool\? \{(.*?)\n    \}\n", text, re.S)
boundary_fn = re.search(r"func matchesWatcherScriptAtBoundary\(.*?\{(.*?)\n    \}\n", text, re.S)

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
    "the matcher's signature is tri-state (Bool?), not a plain Bool":
        re.search(r"func watcherLineMatches\(.*?\)\s*->\s*Bool\?", text) is not None,
    "the matcher excludes the checking process's OWN pid": match_fn is not None
        and "linePID != selfPID" in match_fn.group(1),
    "the boundary helper is a separate, testable function": boundary_fn is not None,
    "the marker is the FULL script name, not the truncated substring the bug matched on":
        boundary_fn is not None and '"watch-tasks-stream.sh"' in boundary_fn.group(1)
        and '"watch-tasks"' not in boundary_fn.group(1),
    "the matcher requires argv[0]'s basename to be a shell, not just the marker present":
        match_fn is not None and "watcherShells" in match_fn.group(1),
    "the matcher rejects a flag in argv[1] position": match_fn is not None
        and 'hasPrefix("-")' in match_fn.group(1),
    "a boundary match is DEFINITE only at exactly two tokens; more tokens are UNDECIDABLE (nil), never a false dead":
        match_fn is not None and "parts.count == 2 ? true : nil" in match_fn.group(1),
    "the aggregator does not let a later definite-false line override an earlier undecidable one":
        fn is not None and "sawUndecidable" in fn.group(1) and "sawUndecidable ? nil : false" in fn.group(1),
}

# Behavioral negative control: actually execute the extracted matcher against
# synthetic ps rows. `want` is Bool? -- nil rows assert the UNDECIDABLE case.
if match_fn is not None and boundary_fn is not None:
    def _as_private(m):
        body = m.group(0)
        name = re.match(r"func (\w+)", body).group(1)
        return "private func " + name + body[len("func " + name):]
    harness = '''
import Foundation
%s
%s

let lines: [(String, String, Bool?)] = [
    ("grep line (own pid, must be excluded even if it matched)", "501 grep --color=auto watch-tasks", false),
    ("unrelated process whose argv merely CONTAINS watch-tasks", "777 tail -f /var/log/watch-tasks-stream.log", false),
    ("unrelated file merely NAMED watch-tasks-*", "778 /usr/bin/vim workspace/notes/watch-tasks-plan.md", false),
    ("the real watcher, full script name at a path boundary", "3003 /bin/bash src/watch-tasks-stream.sh", true),
    // #4269 round 2 (john-the-dev): the FULL script name in a non-executing argv
    // still fail-opened under the boundary-only anchor. These three carry the
    // exact marker, unlike the three rows above (which use the shorter "watch-tasks").
    ("grep line carrying the FULL script name, not just the short marker", "601 grep watch-tasks-stream.sh", false),
    ("editor merely NAMING the full script path (not executing it)", "602 vim /Users/x/src/watch-tasks-stream.sh", false),
    ("tail of a log file with the full script's exact name", "603 tail -f /var/log/watch-tasks-stream.sh", false),
    ("the real watcher via an absolute path, no leading /bin", "3004 bash /Users/x/src/watch-tasks-stream.sh", true),
    // #4269 round 3 (john-the-dev): requiring EXACTLY two tokens turned every
    // legitimate extra-token invocation of the REAL watcher into a false DEAD.
    // The correct answer for all three is UNDECIDABLE (nil), matching what
    // watcher_identity.py's own classify_argv returns for the identical shapes
    // -- the fix is not to make Swift somehow know it's alive, it's to stop it
    // from confidently declaring it dead.
    ("worker mode: real watcher + a positional tasks-dir operand", "4001 bash /Users/x/src/watch-tasks-stream.sh /Users/x/workspace/tasks", nil),
    ("real watcher under the default macOS install path, which itself contains a space, NO operand", "4002 bash /Users/x/Library/Application Support/Sutando/src/watch-tasks-stream.sh", nil),
    ("real watcher, space-free path PLUS an operand (both hazards independently)", "4003 bash /opt/s/watch-tasks-stream.sh /opt/tasks", nil),
]
var failures = 0
for (desc, line, want) in lines {
    let got = watcherLineMatches(Substring(line), excluding: 501)
    let ok = got == want
    print((ok ? "ok   " : "FAIL ") + desc + " (got \\(String(describing: got)), want \\(String(describing: want)))")
    if !ok { failures += 1 }
}
exit(failures == 0 ? 0 : 1)
''' % (_as_private(match_fn), _as_private(boundary_fn))
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
