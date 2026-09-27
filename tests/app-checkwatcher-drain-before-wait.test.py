#!/usr/bin/env python3
"""Sutando.app's watcher check must drain `ps`'s pipe BEFORE waiting for it to exit.

`watcherProcessSeen` runs `/bin/ps -axo pid,command` through a `Pipe` and, until this fix,
called `ps.waitUntilExit()` before `readDataToEndOfFile()`. A pipe buffers 64 KiB; once the
listing outgrows that (measured 99,742 bytes on 2026-09-19 with each claude core/worker argv
carrying an inline `--settings` JSON), `ps` blocks on write, the app's main thread blocks on
exit, and the menu bar hangs until something kills the child. Deterministic, not a race, and
the `terminationStatus != 0` guard cannot see it: it covers a FAILED `ps`, not a HUNG one.

Two checks. The static one pins the order inside the function. The behavioral one runs both
orders against a child that emits more than a pipe's worth of output: read-then-wait finishes,
wait-then-read must not (it is killed at the timeout) -- so the control fails with the fix
removed and passes with it in place."""
import pathlib
import re
import subprocess
import sys

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "Sutando" / "main.swift"
text = SRC.read_text()
fn = re.search(r"func watcherProcessSeen\(\) -> Bool\? \{(.*?)\n    \}\n", text, re.S)
body = fn.group(1) if fn else ""
read_at = body.find("readDataToEndOfFile()")
wait_at = body.find("ps.waitUntilExit()")

checks = {
    "watcherProcessSeen exists": fn is not None,
    "the function reads ps's output": read_at >= 0,
    "the function waits for ps to exit": wait_at >= 0,
    "the pipe is drained BEFORE the wait (read precedes waitUntilExit)": 0 <= read_at < wait_at,
    "the drained bytes are what gets parsed (no second read after the wait)":
        body.count("readDataToEndOfFile()") == 1,
}

HARNESS = '''
import Foundation
// A child that writes well past a pipe's 64 KiB buffer, like `ps -axo pid,command`
// does on a host with several claude argvs.
let p = Process()
p.executableURL = URL(fileURLWithPath: "/bin/sh")
p.arguments = ["-c", "head -c 300000 /dev/zero | tr '\\\\0' x"]
let pipe = Pipe()
p.standardOutput = pipe
p.standardError = FileHandle.nullDevice
try! p.run()
%s
print("bytes=\\(out.count) rc=\\(p.terminationStatus)")
exit(out.count == 300000 && p.terminationStatus == 0 ? 0 : 1)
'''
ORDERS = {
    "fixed order (read, then wait) completes":
        ("let out = pipe.fileHandleForReading.readDataToEndOfFile()\np.waitUntilExit()", True),
    "buggy order (wait, then read) hangs on a >64 KiB listing (killed at timeout)":
        ("p.waitUntilExit()\nlet out = pipe.fileHandleForReading.readDataToEndOfFile()", False),
}
for desc, (order, should_finish) in ORDERS.items():
    tmp = pathlib.Path(f"/tmp/_checkwatcher_drain_{'ok' if should_finish else 'hang'}.swift")
    tmp.write_text(HARNESS % order)
    try:
        try:
            proc = subprocess.run(["xcrun", "swift", str(tmp)], capture_output=True, text=True, timeout=60)
            finished = proc.returncode == 0
            if should_finish and not finished:
                print(proc.stdout, proc.stderr, file=sys.stderr)
        except subprocess.TimeoutExpired:
            finished = False
        checks["behavioral: " + desc] = finished == should_finish
    except OSError as e:
        print(f"SKIP behavioral ({desc}): swift unavailable: {e}", file=sys.stderr)
    finally:
        tmp.unlink(missing_ok=True)

fails = [k for k, ok in checks.items() if not ok]
for k, ok in checks.items():
    print(("ok   " if ok else "FAIL ") + k)
print("all checks pass" if not fails else f"FAILED ({len(fails)})")
sys.exit(1 if fails else 0)
