#!/usr/bin/env python3
"""The workspace-root contract had a migrator and no detector.

CLAUDE.md reserves the workspace root for top-level directories, with loose
state under `state/`. `workspace_default._migrate_root_status` enforces that
via a hardcoded five-name list — the right shape for a MIGRATOR (it relocates
files it knows about) and the wrong shape for enforcement, because anything
added later is exempt by construction.

Found on a live host: `.last-pq-notify` and `.voice-agent.pid` had accumulated
at the root, in none of the 23 existing probes and none of the migrator's five
names.
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

def load(ws):
    spec = importlib.util.spec_from_file_location("hc_under_test", MOD)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    m.WORKSPACE_DIR = ws
    return m

with tempfile.TemporaryDirectory() as td:
    ws = Path(td) / "workspace"; (ws / "state").mkdir(parents=True)
    (ws / "tasks").mkdir(); (ws / "results").mkdir()

    # 1. CONTROL — a clean root returns None, so a healthy install gains no line.
    #    Without this, "always warn" would satisfy every other assertion here.
    for allowed in ("build_log.md", "pending-questions.md", "session-state.md", ".gitkeep"):
        (ws / allowed).write_text("x")
    m = load(ws)
    check("clean root (only sanctioned files + dirs) -> None", m.check_workspace_root_tidy(), None)

    # 2. the two real deviants are detected
    (ws / ".last-pq-notify").write_text("1785876643 2f35a3c6")
    (ws / ".voice-agent.pid").write_text("4242")
    r = load(ws).check_workspace_root_tidy()
    check("loose state is flagged", (r or {}).get("name"), "workspace-root-tidy")
    check("warn, never fail", (r or {}).get("status"), "warn")
    check("names every deviant",
          all(n in (r or {}).get("detail", "") for n in (".last-pq-notify", ".voice-agent.pid")), True)
    check("does NOT name the sanctioned files",
          any(n in (r or {}).get("detail", "") for n in ("build_log.md", "session-state.md")), False)

    # 3. DIRECTORIES are never flagged — the contract reserves the root FOR them,
    #    so a probe that flagged `state/` would invert the rule it enforces.
    (ws / "notes").mkdir()
    r2 = load(ws).check_workspace_root_tidy()
    check("directories are not flagged", "notes" in (r2 or {}).get("detail", ""), False)

    # 4. a missing workspace is another probe's problem, not this one's
    m3 = load(Path(td) / "does-not-exist")
    check("absent workspace -> None", m3.check_workspace_root_tidy(), None)

    # 5. FAIL-SAFE BRANCHES. Both `except OSError` paths are the ones that decide
    #    what happens when the filesystem will not answer, and an untested
    #    fail-safe is indistinguishable from one that never runs. Drive them by
    #    making the directory listing raise.
    class _Boom(type(ws)):
        def iterdir(self):
            raise OSError("simulated unreadable directory")

    m4 = load(ws)
    m4.WORKSPACE_DIR = _Boom(str(ws))
    check("unreadable workspace root -> None, not a crash", m4.check_workspace_root_tidy(), None)

    # The writer-scan's OSError path: root is readable, src/ is not. The probe
    # must still report the drift, just without attributing a writer.
    m5 = load(ws)
    m5.REPO_DIR = Path(td) / "no-such-repo"
    r5 = m5.check_workspace_root_tidy()
    check("unreadable src/ still reports the drift", (r5 or {}).get("status"), "warn")
    check("...and simply omits the writer attribution",
          "written by" in (r5 or {}).get("detail", ""), False)

print(("FAILED: " + ", ".join(fails)) if fails else "workspace-root-tidy: all checks passed")
sys.exit(1 if fails else 0)
