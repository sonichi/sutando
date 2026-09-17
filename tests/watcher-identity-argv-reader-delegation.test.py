#!/usr/bin/env python3
"""Guards the watcher_identity -> proc_argv boundary: ONE argv-vector reader.

`src/proc_argv.py` is the canonical reader of a pid's argv LIST (Linux
/proc/<pid>/cmdline, macOS KERN_PROCARGS2). The signaller (restart.sh via
watcher_identity.sh -> process-ops.sh), the reporter (health-check.py) and the
pool's worker bootstrap (through watcher_identity.classify_argv's default) must
all read the same kernel record through that one module, or two of them can
disagree about one pid. A private copy inside watcher_identity.py is that drift.

Two things must stay true:
  1. watcher_identity.proc_argv_vector really delegates to proc_argv.argv_vector
     (a rebound canonical reader is what the wrapper answers with).
  2. watcher_identity.py carries no second implementation: none of the kernel
     primitives the reader is made of appear there. The scan is TOKEN-SPECIFIC
     (the primitives, not the word "argv") and controlled against proc_argv.py,
     where every token must fire.

Run: python3 tests/watcher-identity-argv-reader-delegation.test.py
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
import proc_argv  # noqa: E402
import watcher_identity as wid  # noqa: E402

fails = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}" + ("" if cond else f" — {detail}"))
    if not cond:
        fails.append(label)


# ── 1. delegation is real ────────────────────────────────────────────────────
print("── delegation ──")
seen = []
_orig = proc_argv.argv_vector
try:
    proc_argv.argv_vector = lambda pid: seen.append(pid) or ["bash", "/x/watch-tasks-stream.sh"]
    got = wid.proc_argv_vector(424242)
finally:
    proc_argv.argv_vector = _orig
check("watcher_identity.proc_argv_vector delegates to proc_argv.argv_vector",
      seen == [424242], f"canonical reader saw {seen}")
check("...and answers with what the canonical reader returned",
      got == ["bash", "/x/watch-tasks-stream.sh"], repr(got))

# classify_argv's DEFAULT reader is the delegating name, so the worker bootstrap
# (which passes no reader) reaches proc_argv too.
seen.clear()
try:
    proc_argv.argv_vector = lambda pid: seen.append(pid) or ["bash", "/x/watch-tasks-stream.sh", "/inbox"]
    verdict = wid.classify_argv("bash /x/watch-tasks-stream.sh /inbox", pid=424243)
finally:
    proc_argv.argv_vector = _orig
check("classify_argv's default reader reaches proc_argv (the bootstrap's path)",
      seen == [424243] and verdict.watcher is True and verdict.operands == ["/inbox"],
      f"seen={seen} verdict={verdict}")

# ── 2. no private reader in watcher_identity.py ──────────────────────────────
print("── no second implementation ──")
src = (REPO / "src" / "watcher_identity.py").read_text()
code = "\n".join(ln for ln in src.splitlines()
                 if ln.strip() and not ln.lstrip().startswith("#"))
canonical = (REPO / "src" / "proc_argv.py").read_text()
PRIMITIVES = ("/proc/", "cmdline", "KERN_PROCARGS2", "sysctl", "ctypes", "create_string_buffer")
for tok in PRIMITIVES:
    check(f"the canonical reader uses {tok!r} (control: the scan can fire)", tok in canonical)
    check(f"watcher_identity.py does not carry {tok!r}", tok not in code,
          "a second argv-vector reader lives in watcher_identity.py")
check("watcher_identity.py imports the canonical module",
      "import proc_argv" in code)

print()
if fails:
    print(f"FAIL — {len(fails)}: {fails}")
    sys.exit(1)
print("PASS — watcher_identity delegates its argv-vector read to proc_argv")
