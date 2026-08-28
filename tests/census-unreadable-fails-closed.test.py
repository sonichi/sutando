#!/usr/bin/env python3
"""An unreadable in-window task file must NOT produce a MET census gate.

The gate is rollout evidence: MET can enable enforcement. A present-but-torn
file is evidence we do not have, not evidence of absence — dropping it silently
lets one transient read certify a window that still contains an unsigned writer.
"""
import importlib.util
import pathlib
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
REPO_SRC = REPO / "src" / "task_envelope_census.py"
spec = importlib.util.spec_from_file_location("cen", REPO_SRC)
cen = importlib.util.module_from_spec(spec)
sys.modules["cen"] = cen
spec.loader.exec_module(cen)

fails = []


def check(name, got, want):
    if got != want:
        fails.append(name); print(f"  FAIL: {name}\n        got {got!r} want {want!r}")
    else:
        print(f"  OK: {name}")


def ws_with(files):
    d = pathlib.Path(tempfile.mkdtemp())
    (d / "tasks").mkdir(parents=True)
    for name, payload in files.items():
        p = d / "tasks" / name
        p.write_bytes(payload) if isinstance(payload, bytes) else p.write_text(payload)
    return d


VERIFIED = "id: task-ok0000000001\nenvelope_hmac: v1:deadbeef\nsource: voice\ntask: hi\n"

# Control: a clean window still reports 0 unreadable, so the field is not always-on.
clean = cen.census(workspace=ws_with({"task-ok0000000001.txt": VERIFIED}), days=3650)
check("clean window: unreadable == 0", clean.get("unreadable", 0), 0)
check("clean window: the verified file was scanned", clean["scanned"], 1)

# The defect: one torn file alongside a verified one.
torn = cen.census(workspace=ws_with({
    "task-ok0000000001.txt": VERIFIED,
    "task-torn000000001.txt": b"id: task-torn000000001\nsource: voice\ntask: \xff\xfe",
}), days=3650)
check("torn file is COUNTED, not silently dropped", torn.get("unreadable", 0), 1)
check("torn file is not counted as scanned", torn["scanned"], 1)


# The other side of the discrimination: an entry that is GONE (broken symlink —
# read raises OSError, exists() is False) must not inflate the counter.
d = ws_with({"task-ok0000000001.txt": VERIFIED})
(d / "tasks" / "task-gone000000001.txt").symlink_to(d / "tasks" / "nothing-here.txt")
gone = cen.census(workspace=d, days=3650)
check("vanished entry does NOT count as unreadable", gone.get("unreadable", 0), 0)
check("vanished entry is not scanned either", gone["scanned"], 1)

# The transition the reviewer asked for: torn -> whole, with the verified task
# as the control. The CLI line is the actual rollout evidence, so assert on it.
import subprocess
import sys as _sys
d = ws_with({"task-ok0000000001.txt": VERIFIED})
torn_p = d / "tasks" / "task-flip000000001.txt"
torn_p.write_bytes(b"id: task-flip000000001\nsource: voice\ntask: \xff\xfe")


def cli(ws):
    r = subprocess.run([_sys.executable, str(REPO_SRC), "--workspace", str(ws), "--days", "3650"],
                       capture_output=True, text=True)
    return r.stdout.strip().splitlines()[-1].strip()


before_n = cen.census(workspace=d, days=3650).get("unreadable", 0)
before_line = cli(d)
torn_p.write_text("id: task-flip000000001\nenvelope_hmac: v1:deadbeef\nsource: voice\ntask: now whole\n")
after_n = cen.census(workspace=d, days=3650).get("unreadable", 0)
after_line = cli(d)

check("torn: unreadable == 1", before_n, 1)
check("whole: unreadable back to 0", after_n, 0)
check("torn: gate reads UNKNOWN", "UNKNOWN" in before_line, True)
check("whole: gate reads MET", "MET" in after_line, True)
check("control: the verified task was scanned throughout",
      cen.census(workspace=d, days=3650)["scanned"], 2)

print()
print(f"{len(fails)} failure(s)" if fails else "all checks passed")
sys.exit(1 if fails else 0)
