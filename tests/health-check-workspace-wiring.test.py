#!/usr/bin/env python3
"""workspace-wiring probe contract: fail on stranded data, warn on recoverable
breaks, silent when healthy, and registered in run_all_checks()."""
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

# CONTROL: healthy layout adds no line.
check("ok -> None (no line on healthy installs)", probe_with("ok"), None)

# Stranded data is the FAIL: auto-heal refused, a human must merge.
r = probe_with("materialized-with-data")
check("materialized-with-data -> fail", (r or {}).get("status"), "fail")
check("fail names the merge remedy", "merge stray files" in (r or {}).get("detail", ""), True)

# Boot-recoverable states warn.
for state in ("missing", "dangling", "wrong-target", "materialized-empty"):
    r = probe_with(state)
    check(f"{state} -> warn", (r or {}).get("status"), "warn")
check("warn names the ensure remedy", "--ensure" in (r or {}).get("detail", ""), True)
check("probe name is workspace-wiring", (r or {}).get("name"), "workspace-wiring")

# Registered, not merely defined: a check no aggregate calls is a latent
# no-op. Fresh module load, hermetic temp workspace, layout patched broken.
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
