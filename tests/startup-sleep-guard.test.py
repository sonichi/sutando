#!/usr/bin/env python3
"""Pin startup.sh's sleep-guard check: only a caffeinate holding -s satisfies it.

A bare `pgrep -q caffeinate` was satisfied by the short `caffeinate -i -t 300`
processes worker sessions spawn, so startup skipped the -s guard and a lid-closed
Mac on AC really slept once they exited. This test uses the REAL pattern from
src/sleep_guard.py (which startup.sh calls), both against argv strings and against
live processes it spawns itself (matched by pid, so other caffeinates do not count).
"""
import pathlib
import re
import shutil
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
STARTUP = REPO / "src" / "startup.sh"


def _pattern() -> str:
    sys.path.insert(0, str(REPO / "src"))
    import sleep_guard
    return sleep_guard.PATTERN


def _startup_delegates() -> list[str]:
    text = STARTUP.read_text()
    fails = []
    if 'src/sleep_guard.py" ensure' not in text:
        fails.append("src/startup.sh no longer calls sleep_guard.py ensure")
    if "caffeinate" in text:
        fails.append("src/startup.sh names caffeinate itself instead of leaving it to sleep_guard.py")
    return fails


def _argv_cases(pat: str) -> list[str]:
    rx = re.compile(pat)
    held = ["caffeinate -s", "caffeinate -d -i -s", "caffeinate -dims", "/usr/bin/caffeinate -s -w 123",
            "caffeinate -s -t 30", "caffeinate -dis"]
    not_held = ["caffeinate -i -t 300", "caffeinate -d", "caffeinate -i", "caffeinate", "mycaffeinate -s",
                "grep caffeinate -s"]
    fails = [f"should hold: {a!r}" for a in held if not rx.search(a)]
    fails += [f"should NOT hold: {a!r}" for a in not_held if rx.search(a)]
    return fails


def _live_cases(pat: str) -> list[str]:
    if sys.platform != "darwin" or not shutil.which("caffeinate"):
        print("skip live cases: no caffeinate on this host")
        return []
    fails = []
    for args, expect in ((["-i", "-t", "20"], False), (["-s", "-t", "20"], True)):
        p = subprocess.Popen(["caffeinate", *args])
        try:
            listed = subprocess.run(["pgrep", "-f", pat], capture_output=True, text=True).stdout.split()
            if (str(p.pid) in listed) != expect:
                fails.append(f"live caffeinate {' '.join(args)}: matched={str(p.pid) in listed}, want {expect}")
        finally:
            p.terminate()
            p.wait()
    return fails


def main() -> int:
    pat = _pattern()
    fails = _startup_delegates() + _argv_cases(pat) + _live_cases(pat)
    for f in fails:
        print("FAIL", f)
    print("all passed" if not fails else f"{len(fails)} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
