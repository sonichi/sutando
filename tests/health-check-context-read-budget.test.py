#!/usr/bin/env python3
"""context-read-budget: warn when hosts/<host>/current-track.md outgrows the per-pass read budget."""
import importlib.util
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MOD = REPO / "src" / "health-check.py"
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    print(("ok   " if ok else "FAIL ") + f"- {name}" + ("" if ok else f" (got {got!r}, want {want!r})"))
    fails += 0 if ok else 1


def load(ws):
    spec = importlib.util.spec_from_file_location("hc_under_test", MOD)
    m = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    m.WORKSPACE_DIR = ws
    m._host_label = lambda: "testhost"
    return m


with tempfile.TemporaryDirectory() as d:
    ws = Path(d); (ws / "hosts" / "testhost").mkdir(parents=True)
    m = load(ws)
    check("no file -> None", m.check_context_read_budget(), None)
    (ws / "hosts" / "testhost" / "current-track.md").write_text("# small\n" * 10)
    check("under budget -> None", m.check_context_read_budget(), None)
    (ws / "hosts" / "testhost" / "current-track.md").write_text("x" * (m.CURRENT_TRACK_READ_BUDGET + 1))
    r = m.check_context_read_budget()
    check("over budget -> warn", (r or {}).get("status"), "warn")
    check("names the probe", (r or {}).get("name"), "context-read-budget")
    check("names the fix", "current-track-rotate.py" in (r or {}).get("detail", ""), True)
    check("names the size", str(m.CURRENT_TRACK_READ_BUDGET + 1) in (r or {}).get("detail", ""), True)

print("PASS" if not fails else f"FAILED ({fails})")
sys.exit(1 if fails else 0)
