"""
Regression guard for the two orphan-leak fixes in src/watch-tasks-stream.sh
(issue #1088).

Mode A (EPIPE-on-dead-reader): watcher used `echo` which silently buffers
~100 events in the kernel pipe after the consumer dies.  Fix: `printf || exit 0`
/ `printf || break` — printf returns non-zero immediately on EPIPE.

Mode B (PPID=1 reparenting): parent shell exits without SIGTERMing the watcher;
watcher lives until host reboot.  Fix: background orphan-guard subshell that
checks PPID every 10 s and sends SIGTERM when PPID becomes 1.
"""
import re
import sys
from pathlib import Path

SRC = (Path(__file__).resolve().parent.parent / "src" / "watch-tasks-stream.sh").read_text()

PASS = True


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS
    status = "ok" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))
    if not condition:
        PASS = False


# Mode A: printf-EPIPE guard in the streaming loop (main event path)
check(
    "streaming loop uses printf || exit 0 (not bare echo) for Mode-A EPIPE",
    bool(re.search(r"printf\s+'TASK_FILE[^']*'\s+[^|]+\|\|\s+exit\s+0", SRC)),
    "expect: printf 'TASK_FILE: %s\\n' ... || exit 0",
)

# Mode A: printf-EPIPE guard in the initial scan
check(
    "initial scan uses printf || break (not bare echo) for Mode-A EPIPE",
    bool(re.search(r"printf\s+'TASK_FILE[^']*'\s+[^|]+\|\|\s+break", SRC)),
    "expect: printf 'TASK_FILE: %s\\n' ... || break",
)

# Mode A: no bare `echo "TASK_FILE:` anywhere (all emit paths must use printf)
check(
    "no bare echo TASK_FILE remains in the script",
    "echo \"TASK_FILE:" not in SRC and "echo 'TASK_FILE:" not in SRC,
    "all TASK_FILE emit lines must use printf, not echo",
)

# Mode B: orphan guard subshell present
check(
    "orphan-guard subshell polls PPID for Mode-B reparenting",
    bool(re.search(r"ppid=.*ps\s+-o\s+ppid=\s+-p\s+\$\$", SRC)),
    "expect: ppid=$(ps -o ppid= -p $$) in a background loop",
)

# Mode B: orphan guard kills the main process when PPID=1
check(
    "orphan guard kills $$ when ppid is 1",
    bool(re.search(r'"\$ppid"\s*=\s*"1".*kill.*\$\$', SRC, re.DOTALL))
    or bool(re.search(r"\$ppid.*=.*1.*&&.*kill.*\$\$", SRC)),
    "expect: [ \"$ppid\" = \"1\" ] && kill \"$$\"",
)

# Mode B: orphan guard PID captured so the EXIT trap can clean it up
check(
    "ORPHAN_GUARD_PID captured after background subshell",
    "ORPHAN_GUARD_PID=$!" in SRC,
    "expect: ORPHAN_GUARD_PID=$! immediately after ( ... ) &",
)

# Mode B: EXIT trap kills the orphan guard on clean shutdown
check(
    "EXIT trap kills ORPHAN_GUARD_PID on exit",
    bool(re.search(r"trap.*ORPHAN_GUARD_PID.*EXIT", SRC, re.DOTALL))
    or bool(re.search(r"kill.*ORPHAN_GUARD_PID.*2>/dev/null.*EXIT", SRC)),
    "trap must include kill $ORPHAN_GUARD_PID and EXIT",
)

# Mode B: trap covers TERM so SIGTERM fires the cleanup
check(
    "EXIT trap also covers INT and TERM signals",
    bool(re.search(r"trap\s+'[^']+'\s+EXIT\s+INT\s+TERM", SRC))
    or bool(re.search(r"trap\s+'[^']+'\s+EXIT.*TERM", SRC)),
    "expect: trap '...' EXIT INT TERM",
)

sys.exit(0 if PASS else 1)
