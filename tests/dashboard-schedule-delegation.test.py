#!/usr/bin/env python3
"""Guards the dashboard → dashboard_schedules boundary.

Two things must stay true:
  1. dashboard.py's schedule entry points really delegate to the module (not a
     copy that drifts).
  2. No dashboard route rebuilds its own crons.json read/merge/write. That was
     the whole point of the extraction; a future handler doing its own
     json.loads + os.replace would silently reintroduce the lost-update bug the
     transaction lock exists to prevent (CR #2164).

The source scan is deliberately TOKEN-SPECIFIC — it flags the atomic-write
primitives, not the words "cron" or "schedule" — so it cannot be satisfied by
renaming and cannot false-positive on presentation code.
"""
import sys
import re
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO / "src"))
import dashboard as dash  # noqa: E402
import dashboard_schedules as ds  # noqa: E402

fails = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}" + ("" if cond else f" — {detail}"))
    if not cond:
        fails.append(label)


# ── 1. delegation is real ────────────────────────────────────────────────────
print("── delegation ──")
seen = {}

_orig_up, _orig_del = ds.upsert_schedule, ds.delete_schedule
_orig_read, _orig_write = ds.read_crons, ds.write_crons
try:
    ds.upsert_schedule = lambda path, body: seen.setdefault("upsert", (path, body)) and (200, {})
    ds.delete_schedule = lambda path, name: seen.setdefault("delete", (path, name)) and (200, {})
    ds.read_crons = lambda path: seen.setdefault("read", path) and []
    ds.write_crons = lambda path, jobs: seen.setdefault("write", (path, jobs))

    dash.upsert_schedule({"name": "x"})
    dash.delete_schedule("x")
    dash._read_crons()
    dash._write_crons([])
finally:
    ds.upsert_schedule, ds.delete_schedule = _orig_up, _orig_del
    ds.read_crons, ds.write_crons = _orig_read, _orig_write

check("dashboard.upsert_schedule delegates to the module", "upsert" in seen)
check("dashboard.delete_schedule delegates to the module", "delete" in seen)
check("dashboard._read_crons delegates to the module", "read" in seen)
check("dashboard._write_crons delegates to the module", "write" in seen)
check("dashboard supplies the resolved path, not the module resolving it",
      seen.get("upsert", (None,))[0] == dash._crons_path(),
      str(seen.get("upsert", (None,))[0]))
check("the re-exported lock IS the module's lock (not a second one)",
      dash._CRONS_LOCK is ds._CRONS_LOCK)

# ── 2. no route-local persistence in dashboard.py ────────────────────────────
print("── no route-local crons persistence ──")
src = (REPO / "src" / "dashboard.py").read_text()
code_lines = [ln for ln in src.splitlines()
              if ln.strip() and not ln.lstrip().startswith("#")]
body = "\n".join(code_lines)

# The atomic-write primitives. Their presence in dashboard.py means someone
# rebuilt a write path outside the module.
check("dashboard.py does not call os.replace (atomic write belongs to the module)",
      "os.replace(" not in body,
      "found os.replace in dashboard.py")
check("dashboard.py does not build a .tmp schedule file",
      not re.search(r"\.json\.\{?.*tmp", body) and ".tmp" not in body,
      "found a .tmp construction in dashboard.py")
check("dashboard.py does not construct its own crons lock",
      "threading.Lock()" not in body,
      "found threading.Lock() in dashboard.py")
# A read of the crons path outside the module would be json.loads over it.
check("dashboard.py does not json-parse the crons file itself",
      not re.search(r"_crons_path\(\)[^\n]*read_text|json\.loads\([^\n]*_crons_path", body),
      "found a direct crons.json parse in dashboard.py")

print()
if fails:
    print(f"FAIL — {len(fails)}: {fails}")
    sys.exit(1)
print("PASS — dashboard schedule delegation boundary")
