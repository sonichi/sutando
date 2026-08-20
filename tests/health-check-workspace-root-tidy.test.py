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

    # 10. `.env` is CONTRACT-SANCTIONED, not drift. `sutando_config.resolve_dotenv`
    #     resolves repo-root -> workspace (#1871), and health-check's own `.env`
    #     probe reads and validates that second tier — so the two probes disagreed
    #     about the same file: one required it, the other called it escaped state.
    td7 = Path(_tf.mkdtemp()); ws7 = td7 / "workspace"; (ws7 / "state").mkdir(parents=True)
    (ws7 / ".env").write_text("GEMINI_API_KEY=x")
    check("a workspace .env is not drift", load(ws7).check_workspace_root_tidy(), None)

    # 11. Lock guards live at the root by six-site convention (voice-lock.ts,
    #     startup-runtime.sh, restart.sh, restart-voice-agent.sh, voice-lock.test.py).
    #     Moving one without the others leaves two processes disagreeing about where
    #     the lock is — a double-started voice agent, worse than the warn.
    #     Both are exempt BY NAME, not by a `*.lock.guard` glob: state/locks/ is where
    #     workspace_lock.py writes this artifact type, so an unknown guard at the root
    #     is a resolution bug the probe must keep catching — test 13 pins that.
    td8 = Path(_tf.mkdtemp()); ws8 = td8 / "workspace"; (ws8 / "state").mkdir(parents=True)
    (ws8 / ".voice-agent.lock.guard").write_text("")
    (ws8 / ".backend-supervisor.lock.guard").write_text("")
    check("lock guards are not drift", load(ws8).check_workspace_root_tidy(), None)

    # 12. The exemptions must not swallow the one file that IS this repo's drift.
    #     Without this, adding `.env` + the guard glob could have been written as a
    #     blanket dotfile pass and every check above would still be green.
    td9 = Path(_tf.mkdtemp()); ws9 = td9 / "workspace"; (ws9 / "state").mkdir(parents=True)
    for name in (".env", ".voice-agent.lock.guard", ".backend-supervisor.lock.guard",
                 ".voice-agent.pid"):
        (ws9 / name).write_text("")
    r12 = load(ws9).check_workspace_root_tidy()
    check("the real deviant is still flagged", ".voice-agent.pid" in (r12 or {}).get("detail", ""), True)
    check("and the exempt files are not named alongside it",
          any(n in (r12 or {}).get("detail", "")
              for n in (".env", ".lock.guard")), False)

    # 13. An UNKNOWN root .lock.guard is still drift. state/locks/ is where
    #     workspace_lock.py already writes this artifact type, and this probe
    #     only lists ROOT files — so a role guard appearing at the root is a
    #     workspace-resolution bug, the class the probe exists to catch. A
    #     `*.lock.guard` glob would have hidden it; the two real root guards
    #     are exempt by name instead.
    td11 = Path(_tf.mkdtemp()); ws11 = td11 / "workspace"; (ws11 / "state").mkdir(parents=True)
    (ws11 / "sync-worker.lock.guard").write_text("")
    r11 = load(ws11).check_workspace_root_tidy()
    check("an unknown root .lock.guard is still drift",
          "sync-worker.lock.guard" in (r11 or {}).get("detail", ""), True)

    # 14. A guard-LIKE name that is not a guard stays flagged — the exemption
    #     must not degrade into "anything containing lock".
    td10 = Path(_tf.mkdtemp()); ws10 = td10 / "workspace"; (ws10 / "state").mkdir(parents=True)
    (ws10 / "voice.lock").write_text("")
    (ws10 / ".env.local").write_text("")
    r13 = load(ws10).check_workspace_root_tidy()
    check("a bare .lock is still drift", "voice.lock" in (r13 or {}).get("detail", ""), True)
    check("and so is .env.local (only the resolver's own name is sanctioned)",
          ".env.local" in (r13 or {}).get("detail", ""), True)

    # 15. A write-once notice sentinel has NO destination: init.sh reads it AT
    #     the root and nothing unlinks it, so flagging it is a permanent WARN.
    td12 = Path(_tf.mkdtemp()); ws12 = td12 / "workspace"; (ws12 / "state").mkdir(parents=True)
    (ws12 / ".legacy-notice-printed").write_text("")
    (ws12 / ".voice-agent.pid").write_text("4242")
    r14 = load(ws12).check_workspace_root_tidy()
    d14 = (r14 or {}).get("detail", "")
    check("the notice sentinel is not flagged", ".legacy-notice-printed" in d14, False)
    check("the real deviant beside it still is", ".voice-agent.pid" in d14, True)
    check("so the probe still fires rather than going silent", r14 is not None, True)

print(("FAILED: " + ", ".join(fails)) if fails else "workspace-root-tidy: all checks passed")
sys.exit(1 if fails else 0)
