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
spec = importlib.util.spec_from_file_location("cen", REPO / "src" / "task_envelope_census.py")
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

print()
print(f"{len(fails)} failure(s)" if fails else "all checks passed")
sys.exit(1 if fails else 0)
