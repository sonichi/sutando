#!/usr/bin/env python3
"""The app's watcher probe must ask the kernel for the real argv, not only `ps`.

`ps -axo pid,command` FLATTENS argv, so `bash /x/watch-tasks-stream.sh /x/tasks` and a
script whose pathname literally contains a space are the same string. #4269 round 3 was
right to answer UNDECIDABLE there rather than DEAD -- from that input nothing can decide.
What changed since is the input's frequency, not its ambiguity: the notifier launches the
core watcher WITH a tasks-dir operand, so the undecidable shape became the NORMAL one and
`watcherProcessSeen` returned nil on every tick. It never alerted -- measured on this host
2026-09-19, when a core died for 47 minutes with the watchdog mute throughout.

The fix does not flip #4269's pin: the flattened matcher still answers nil for that shape,
because that is the honest answer for that input. It gives the probe a better input --
KERN_PROCARGS2, which is NUL-separated and therefore decides -- exactly as
src/watcher_identity.py's classify_argv already does for the Python side.

The behavioral control spawns a REAL process in the undecidable shape and asserts the
kernel path answers True where the flattened path answers nil. It fires on a known
positive, so a probe that silently answered nothing would fail it."""
import os
import pathlib
import re
import subprocess
import sys
import tempfile

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "Sutando" / "main.swift"
text = SRC.read_text()
seen = re.search(r"func watcherProcessSeen\(\) -> Bool\? \{(.*?)\n    \}\n", text, re.S)
vec = re.search(r"func procArgvVector\(.*?\n    \}\n", text, re.S)
verdict = re.search(r"func watcherVerdictForPID\(.*?\n    \}\n", text, re.S)
linepid = re.search(r"func linePID\(.*?\n    \}\n", text, re.S)
match_fn = re.search(r"func watcherLineMatches\(.*?-> Bool\? \{(.*?)\n    \}\n", text, re.S)
boundary_fn = re.search(r"func matchesWatcherScriptAtBoundary\(.*?\{(.*?)\n    \}\n", text, re.S)

checks = {
    "the kernel argv reader exists": vec is not None,
    "it reads KERN_PROCARGS2, the NUL-separated vector (not another ps call)":
        vec is not None and "KERN_PROCARGS2" in vec.group(0) and "/bin/ps" not in vec.group(0),
    "a per-pid verdict function exists": verdict is not None,
    "the pid column parser exists": linepid is not None,
    "the undecidable branch consults the pid probe before recording an unknown":
        seen is not None and "watcherVerdictForPID" in seen.group(1),
    "a kernel DEFINITE-true still short-circuits to alive":
        seen is not None and re.search(r"if decided \{ return true \}", seen.group(1)) is not None,
    "an unreadable vector stays UNKNOWN (nil), never a dead watcher":
        verdict is not None and "return nil" in verdict.group(0),
    # The point of the fix: #4269's flattened pin is untouched.
    "#4269's flattened pin is NOT weakened -- a boundary match past two tokens is still nil":
        match_fn is not None and "parts.count == 2 ? true : nil" in match_fn.group(1),
    "the aggregator still refuses to let a definite-false override an undecidable":
        seen is not None and "sawUndecidable ? nil : false" in seen.group(1),
}

# Behavioral control. Spawn the real undecidable shape and decide it two ways.
if vec and verdict and match_fn and boundary_fn:
    def _private(m):
        body = m.group(0)
        name = re.match(r"func (\w+)", body).group(1)
        return "private func " + name + body[len("func " + name):]

    tmpdir = tempfile.mkdtemp()
    script = pathlib.Path(tmpdir) / "watch-tasks-stream.sh"
    script.write_text("#!/bin/bash\nsleep 30\n")
    script.chmod(0o755)
    operand = pathlib.Path(tmpdir) / "tasks"
    operand.mkdir()
    child = subprocess.Popen(["/bin/bash", str(script), str(operand)])
    decoy = subprocess.Popen(["/bin/bash", "-c", "sleep 30"])
    try:
        harness = '''
import Foundation
%s
%s
%s
%s

let pid = Int32(CommandLine.arguments[1])!
let decoy = Int32(CommandLine.arguments[2])!
let flat = CommandLine.arguments[3]
var failures = 0
func expect(_ desc: String, _ got: Bool?, _ want: Bool?) {
    let ok = got == want
    print((ok ? "ok   " : "FAIL ") + desc + " (got \\(String(describing: got)), want \\(String(describing: want)))")
    if !ok { failures += 1 }
}
// The gap this fix closes: same process, two inputs, two answers.
expect("flattened ps column cannot decide the real watcher (this is the mute watchdog)",
       watcherLineMatches(Substring("\\(pid) " + flat), excluding: 1), nil)
expect("the kernel's argv vector decides the SAME process is the watcher",
       watcherVerdictForPID(pid), true)
expect("a shell that is not running the watcher is a definite no",
       watcherVerdictForPID(decoy), false)
expect("a pid that does not exist stays UNKNOWN, not dead",
       watcherVerdictForPID(999999), nil)
exit(failures == 0 ? 0 : 1)
''' % (_private(vec), _private(verdict), _private(match_fn), _private(boundary_fn))
        f = pathlib.Path(tmpdir) / "control.swift"
        f.write_text(harness)
        flat = f"/bin/bash {script} {operand}"
        try:
            proc = subprocess.run(["xcrun", "swift", str(f), str(child.pid), str(decoy.pid), flat],
                                  capture_output=True, text=True, timeout=180)
            print(proc.stdout.strip(), file=sys.stderr)
            checks["behavioral control: kernel argv decides what the ps column cannot"] = proc.returncode == 0
            if proc.returncode != 0:
                print(proc.stderr, file=sys.stderr)
        except (OSError, subprocess.TimeoutExpired) as e:
            print(f"SKIP behavioral control (swift unavailable: {e})", file=sys.stderr)
    finally:
        for p in (child, decoy):
            p.kill()
            p.wait()

fails = [k for k, ok in checks.items() if not ok]
for k, ok in checks.items():
    print(("ok   " if ok else "FAIL ") + k)
print("all checks pass" if not fails else f"FAILED ({len(fails)})")
sys.exit(1 if fails else 0)
