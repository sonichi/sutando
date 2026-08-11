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

    # 6. MIGRATED WORKSPACE — the blocking finding on the first head. These four
    #    sentinels are production-owned: workspace_default.py writes them to the
    #    root for O(1) re-entry and deliberately KEEPS them. Warning on them would
    #    put a permanent WARN on every upgraded install, which trains operators to
    #    ignore the detector — the exact failure this probe exists to prevent.
    import tempfile as _tf
    td2 = Path(_tf.mkdtemp()); ws2 = td2 / "workspace"; (ws2 / "state").mkdir(parents=True)
    for sentinel in (".status-migrated", ".notes-migrated",
                     ".build_log-migrated", ".conversation-log-migrated"):
        (ws2 / sentinel).write_text("")
    check("sentinel-only migrated root stays SILENT", load(ws2).check_workspace_root_tidy(), None)

    #    PAIRED CONTROL: silence must come from recognising the sentinels, not
    #    from the probe going inert on a migrated workspace. One real loose file
    #    beside them must still warn, and must NOT name the sentinels.
    (ws2 / ".voice-agent.pid").write_text("1")
    r6 = load(ws2).check_workspace_root_tidy()
    check("a real loose file beside sentinels still warns", (r6 or {}).get("status"), "warn")
    check("...and names only the real one",
          ".voice-agent.pid" in (r6 or {}).get("detail", "")
          and ".status-migrated" not in (r6 or {}).get("detail", ""), True)

    #    Pattern, not four literals — `sutando-migrate.sh` finds the family with
    #    `-name ".*-migrated*"`, so a sentinel added later must be exempt too.
    td3 = Path(_tf.mkdtemp()); ws3 = td3 / "workspace"; (ws3 / "state").mkdir(parents=True)
    (ws3 / ".some-future-thing-migrated").write_text("")
    check("an UNLISTED sentinel matching the family glob is exempt",
          load(ws3).check_workspace_root_tidy(), None)

    # 7. Attribution must be unambiguous or absent. Two source files mentioning
    #    the same name previously produced a confident hits[0] — which named
    #    whichever sorted first, including a test that merely contains the string.
    td4 = Path(_tf.mkdtemp()); ws4 = td4 / "workspace"; (ws4 / "state").mkdir(parents=True)
    (ws4 / "ambiguous.tmp").write_text("")
    fake_src = td4 / "repo" / "src"; fake_src.mkdir(parents=True)
    (fake_src / "a.py").write_text("ambiguous.tmp")
    (fake_src / "b.py").write_text("ambiguous.tmp")
    m7 = load(ws4); m7.REPO_DIR = td4 / "repo"
    r7 = m7.check_workspace_root_tidy()
    check("two candidate writers -> no attribution rather than a wrong one",
          "written by" in (r7 or {}).get("detail", ""), False)
    check("...but the drift is still reported", "ambiguous.tmp" in (r7 or {}).get("detail", ""), True)

    # 8. POSITIVE CONTROL for attribution — without this, check 7 is unfalsifiable.
    #    "Two writers -> no attribution" passes identically whether the tightened
    #    rule works or attribution is broken outright and never fires at all. Only
    #    a case that MUST name a writer separates those two worlds.
    td5 = Path(_tf.mkdtemp()); ws5 = td5 / "workspace"; (ws5 / "state").mkdir(parents=True)
    (ws5 / ".sole-writer.pid").write_text("")
    src5 = td5 / "repo" / "src"; src5.mkdir(parents=True)
    (src5 / "only-me.py").write_text("path = '.sole-writer.pid'")
    (src5 / "unrelated.py").write_text("nothing to see")
    m8 = load(ws5); m8.REPO_DIR = td5 / "repo"
    r8 = m8.check_workspace_root_tidy()
    check("exactly one candidate writer -> the file IS named",
          "written by only-me.py" in (r8 or {}).get("detail", ""), True)

    # 9. The probe must be REGISTERED, not merely defined. A check that no
    #    aggregate ever calls is a latent no-op: it passes its own unit test
    #    forever while reporting nothing to any operator.
    td6 = Path(_tf.mkdtemp()); ws6 = td6 / "workspace"; (ws6 / "state").mkdir(parents=True)
    (ws6 / "definitely-loose.tmp").write_text("")
    m9 = load(ws6)
    names = [c.get("name") for c in m9.run_all_checks()]
    check("workspace-root-tidy is wired into run_all_checks()",
          "workspace-root-tidy" in names, True)

print(("FAILED: " + ", ".join(fails)) if fails else "workspace-root-tidy: all checks passed")
sys.exit(1 if fails else 0)
