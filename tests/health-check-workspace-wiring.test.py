#!/usr/bin/env python3
"""The workspace-wiring probe's contract: fail on stranded data, warn on
boot-recoverable breaks, silent when healthy — and actually registered.

Follows tests/health-check-workspace-root-tidy.test.py's pattern: load
health-check as a module, drive the probe through a substituted
inspect_layout, and assert run_all_checks() carries the probe (a check no
aggregate calls is a latent no-op).
"""
import importlib.util
import sys
import tempfile
from pathlib import Path

MOD = Path(__file__).resolve().parent.parent / "src" / "health-check.py"

fails = []
def check(name, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}: got {got!r}, want {want!r}")
    if not ok: fails.append(name)

spec = importlib.util.spec_from_file_location("hc_wiring_test", MOD)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

def probe_with(state, detail="d"):
    m.inspect_layout = lambda root: {"path": "p", "app_target": "t", "state": state, "detail": detail}
    return m.check_workspace_wiring()

# 1. CONTROL — healthy layout adds no line.
check("ok -> None (no line on healthy installs)", probe_with("ok"), None)

# 2. Stranded data is the FAIL: writes are landing outside the durable
#    workspace and auto-heal refused (would orphan the files).
r = probe_with("materialized-with-data")
check("materialized-with-data -> fail", (r or {}).get("status"), "fail")
check("fail names the merge remedy", "merge stray files" in (r or {}).get("detail", ""), True)

# 3. Boot-recoverable states warn.
for state in ("missing", "dangling", "wrong-target", "materialized-empty"):
    r = probe_with(state)
    check(f"{state} -> warn", (r or {}).get("status"), "warn")
check("warn names the ensure remedy", "--ensure" in (r or {}).get("detail", ""), True)
check("probe name is workspace-wiring", (r or {}).get("name"), "workspace-wiring")

# 4. Registered, not merely defined: run_all_checks() must carry the probe
#    when the layout is broken. Fresh module load (unpatched), pointed at a
#    temp workspace so the run is hermetic; the live checkout's REPO_DIR is
#    healthy, so patch inspect_layout again for the aggregate pass.
spec2 = importlib.util.spec_from_file_location("hc_wiring_test2", MOD)
m2 = importlib.util.module_from_spec(spec2)
spec2.loader.exec_module(m2)
with tempfile.TemporaryDirectory() as td:
    ws = Path(td) / "workspace"
    (ws / "state").mkdir(parents=True); (ws / "tasks").mkdir(); (ws / "results").mkdir()
    m2.WORKSPACE_DIR = ws
    m2.inspect_layout = lambda root: {"path": "p", "app_target": "t",
                                      "state": "missing", "detail": "probe-registration test"}
    names = [c.get("name") for c in m2.run_all_checks()]
    check("workspace-wiring is wired into run_all_checks()", "workspace-wiring" in names, True)

print(("FAILED: " + ", ".join(fails)) if fails else "workspace-wiring: all checks passed")
sys.exit(1 if fails else 0)
