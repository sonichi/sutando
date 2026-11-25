#!/usr/bin/env python3
"""Pin the sleep-guard match startup.sh relies on: only a long-lived caffeinate holding -s counts.

A bare `pgrep -q caffeinate` was satisfied by short `caffeinate -i -t 300` processes, so startup
skipped the -s guard and a lid-closed Mac on AC slept once they exited. A `-s` caffeinate with
`-t`, `-w` or a wrapped command also ends by itself, so it must not count either. Uses the real
src/sleep_guard.py (which startup.sh calls), against argv strings and against live processes it
spawns itself (matched by pid, so other caffeinates on the host do not count).
"""
import pathlib
import shutil
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
STARTUP = REPO / "src" / "startup.sh"
sys.path.insert(0, str(REPO / "src"))
import sleep_guard  # noqa: E402


def _startup_delegates() -> list[str]:
    text = STARTUP.read_text()
    fails = []
    if 'src/sleep_guard.py" ensure' not in text:
        fails.append("src/startup.sh no longer calls sleep_guard.py ensure")
    if "caffeinate" in text:
        fails.append("src/startup.sh names caffeinate itself instead of leaving it to sleep_guard.py")
    return fails


def _argv_cases() -> list[str]:
    held = ["caffeinate -s", "caffeinate -d -i -s", "caffeinate -dims", "/usr/bin/caffeinate -s", "caffeinate -dis"]
    not_held = ["caffeinate -i -t 300", "caffeinate -d", "caffeinate -i", "caffeinate", "mycaffeinate -s",
                "grep caffeinate -s", "caffeinate -s -t 30", "/usr/bin/caffeinate -s -w 123",
                "caffeinate -i -s make build", "caffeinate make -s", "caffeinate -st 60"]
    fails = [f"should hold: {a!r}" for a in held if not sleep_guard.holds(a)]
    fails += [f"should NOT hold: {a!r}" for a in not_held if sleep_guard.holds(a)]
    return fails


def _live_cases() -> list[str]:
    if sys.platform != "darwin" or not shutil.which("caffeinate"):
        print("skip live cases: no caffeinate on this host")
        return []
    from sutando_platform import process_snapshot
    fails = []
    for args, expect in ((["-i", "-t", "20"], False), (["-s", "-t", "20"], False), (["-s"], True)):
        p = subprocess.Popen(["caffeinate", *args])
        try:
            rows = [r.split(None, 2) for r in (process_snapshot() or "").splitlines()]
            mine = [r[2] for r in rows if len(r) == 3 and r[0] == str(p.pid)]
            got = bool(mine) and sleep_guard.holds(mine[0])
            if got != expect:
                fails.append(f"live caffeinate {' '.join(args)}: held={got}, want {expect}")
        finally:
            p.terminate()
            p.wait()
    return fails


def main() -> int:
    fails = _startup_delegates() + _argv_cases() + _live_cases()
    for f in fails:
        print("FAIL", f)
    print("all passed" if not fails else f"{len(fails)} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
