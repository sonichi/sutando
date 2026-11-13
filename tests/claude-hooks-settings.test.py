#!/usr/bin/env python3
"""claude_hooks_settings: install once, prune dead copies of the same hook, touch nothing else.

The live defect: three SessionStart entries pointing at deleted /var/folders/…/repo/src/
personal-claude-compact-hint.sh copies, left by test runs; every compaction fired all four
and three failed "No such file". Neither installer could remove an entry.

Run: python3 tests/claude-hooks-settings.test.py
Exit: 0 = all pass, 1 = failure
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
import claude_hooks_settings as chs  # noqa: E402

_pass = 0
_fail = 0


def ok(label):
    global _pass
    print(f"  PASS: {label}")
    _pass += 1


def fail(label, detail=""):
    global _fail
    print(f"  FAIL: {label}" + (f" — {detail}" if detail else ""), file=sys.stderr)
    _fail += 1


def check(cond, label, detail=""):
    ok(label) if cond else fail(label, detail)


def entry(cmd, matcher="compact"):
    return {"matcher": matcher, "hooks": [{"type": "command", "command": cmd}]}


with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    live = tmp / "repo" / "src" / "personal-claude-compact-hint.sh"
    live.parent.mkdir(parents=True)
    live.write_text("#!/bin/bash\n")
    live_cmd = f'bash "{live}"'
    dead_cmds = [f'bash "{tmp}/gone{i}/repo/src/personal-claude-compact-hint.sh"' for i in range(3)]
    foreign_dead = f'bash "{tmp}/gone/other-tool.sh"'
    foreign_live = tmp / "mine.sh"
    foreign_live.write_text("#!/bin/bash\n")

    # ── script_path_of / family_of ────────────────────────────────────────────
    check(chs.script_path_of(live_cmd) == str(live), "script path parsed from a quoted bash command")
    check(chs.script_path_of("python3 '/x/y/z.py' --flag") == "/x/y/z.py", "script path parsed after an interpreter")
    check(chs.family_of(dead_cmds[0]) == "personal-claude-compact-hint.sh", "family is the script basename")
    check(chs.family_of("") == "", "an empty command has no family")

    # ── fresh install ─────────────────────────────────────────────────────────
    settings = tmp / "fresh" / ".claude" / "settings.json"
    status, removed = chs.install(settings, event="SessionStart", command=live_cmd, matcher="compact")
    data = json.loads(settings.read_text())
    check(status == "installed" and removed == [], "fresh install reports installed, removes nothing")
    check(data["hooks"]["SessionStart"] == [entry(live_cmd)], "fresh install writes one entry with the matcher")

    # ── idempotent ────────────────────────────────────────────────────────────
    status, removed = chs.install(settings, event="SessionStart", command=live_cmd, matcher="compact")
    check(status == "already installed" and len(json.loads(settings.read_text())["hooks"]["SessionStart"]) == 1,
          "a second install is a no-op")

    # ── prune dead same-family entries, keep everything else ──────────────────
    polluted = tmp / "polluted" / ".claude" / "settings.json"
    polluted.parent.mkdir(parents=True)
    polluted.write_text(json.dumps({"hooks": {
        "SessionStart": [entry(live_cmd)] + [entry(c) for c in dead_cmds]
                        + [entry(foreign_dead, matcher="")] + [entry(f'bash "{foreign_live}"', matcher="")],
        "Stop": [entry('bash "/nowhere/personal-claude-compact-hint.sh"', matcher="")],
    }}))
    status, removed = chs.install(polluted, event="SessionStart", command=live_cmd, matcher="compact")
    after = json.loads(polluted.read_text())
    cmds = [h["command"] for e in after["hooks"]["SessionStart"] for h in e["hooks"]]
    check(status == "already installed", "the live entry is recognised as present")
    check(sorted(removed) == sorted(dead_cmds), "exactly the three dead same-family entries are removed",
          f"removed={removed}")
    check(live_cmd in cmds, "the live same-family entry stays")
    check(foreign_dead in cmds, "a dead hook of ANOTHER family is left alone")
    check(f'bash "{foreign_live}"' in cmds, "a live foreign hook is left alone")
    check(len(after["hooks"]["Stop"]) == 1, "other events are untouched even for the same family")

    # ── a relative live path is judged against the project, not the cwd ──────
    rel_proj = tmp / "relproj"
    (rel_proj / "src").mkdir(parents=True)
    (rel_proj / "src" / "personal-claude-compact-hint.sh").write_text("#!/bin/bash\n")
    rel_settings = rel_proj / ".claude" / "settings.json"
    rel_settings.parent.mkdir(parents=True)
    rel_settings.write_text(json.dumps({"hooks": {"SessionStart": [
        entry('bash "src/personal-claude-compact-hint.sh"'),
        entry('bash "src/gone/personal-claude-compact-hint.sh"')]}}))
    cwd = os.getcwd()
    os.chdir(tmp)  # somewhere the relative path does NOT resolve from
    try:
        status, removed = chs.install(rel_settings, event="SessionStart", command=live_cmd, matcher="compact")
    finally:
        os.chdir(cwd)
    kept_rel = [h["command"] for e in json.loads(rel_settings.read_text())["hooks"]["SessionStart"] for h in e["hooks"]]
    check('bash "src/personal-claude-compact-hint.sh"' in kept_rel and len(removed) == 1
          and "src/gone/" in removed[0], "a relative live path is kept and a relative dead one removed, judged from the project dir",
          f"kept={kept_rel} removed={removed}")

    # ── prepend ───────────────────────────────────────────────────────────────
    first_cmd = f'bash "{foreign_live}"'
    ordered = tmp / "ordered" / ".claude" / "settings.json"
    chs.install(ordered, event="SessionStart", command=f'bash "{live}"', matcher="")
    chs.install(ordered, event="SessionStart", command=first_cmd, matcher="", prepend=True)
    data = json.loads(ordered.read_text())
    check(data["hooks"]["SessionStart"][0]["hooks"][0]["command"] == first_cmd, "prepend puts the entry first")

    # ── CLI ───────────────────────────────────────────────────────────────────
    cli_settings = tmp / "cli" / ".claude" / "settings.json"
    cli_settings.parent.mkdir(parents=True)
    cli_settings.write_text(json.dumps({"hooks": {"SessionStart": [entry(dead_cmds[0])]}}))
    r = subprocess.run([sys.executable, str(REPO / "src" / "claude_hooks_settings.py"), "install",
                        "--settings", str(cli_settings), "--command", live_cmd, "--matcher", "compact",
                        "--label", "PERSONAL_CLAUDE compact-reinject hook"],
                       capture_output=True, text=True, timeout=30)
    check(r.returncode == 0 and "removed 1 dead personal-claude-compact-hint.sh entry" in r.stdout
          and "PERSONAL_CLAUDE compact-reinject hook (installed)" in r.stdout,
          "the CLI names what it removed and what it installed", r.stdout + r.stderr)

    # ── malformed settings fail loudly, never silently succeed ────────────────
    bad = tmp / "bad" / ".claude" / "settings.json"
    bad.parent.mkdir(parents=True)
    bad.write_text("[]")
    r = subprocess.run([sys.executable, str(REPO / "src" / "claude_hooks_settings.py"), "install",
                        "--settings", str(bad), "--command", live_cmd], capture_output=True, text=True)
    check(r.returncode == 1 and "not an object" in r.stderr, "a malformed settings file is refused with a reason")

    # ── the real installer prunes too (it is what runs on every core launch) ──
    work = tmp / "work"
    (work / ".claude").mkdir(parents=True)
    (work / ".claude" / "settings.json").write_text(json.dumps({"hooks": {
        "SessionStart": [entry(c) for c in dead_cmds]}}))
    env = dict(os.environ, SUTANDO_CLAUDE_WORKING_DIR=str(work))
    r = subprocess.run(["bash", str(REPO / "scripts" / "install-personal-claude-hook.sh")],
                       capture_output=True, text=True, env=env, timeout=30)
    data = json.loads((work / ".claude" / "settings.json").read_text())
    fam = [h["command"] for e in data["hooks"]["SessionStart"] for h in e["hooks"]
           if "personal-claude-compact-hint.sh" in h["command"]]
    check(r.returncode == 0 and len(fam) == 1 and str(REPO) in fam[0] and "removed 3 dead" in r.stdout,
          "install-personal-claude-hook.sh leaves exactly one live compact-hint entry", r.stdout + r.stderr)

    r2 = subprocess.run(["bash", str(REPO / "scripts" / "install-session-start-hook.sh")],
                        capture_output=True, text=True, env=env, timeout=30)
    data = json.loads((work / ".claude" / "settings.json").read_text())
    first = data["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    check(r2.returncode == 0 and "schedule-crons-session-hint.sh" in first,
          "install-session-start-hook.sh still prepends its entry so it fires first", r2.stdout + r2.stderr)

    # ── the CLI and its branches, IN PROCESS (a subprocess is invisible to the coverage tracer) ──
    import contextlib
    import io
    inproc = tmp / "inproc" / ".claude" / "settings.json"
    inproc.parent.mkdir(parents=True)
    inproc.write_text(json.dumps({"hooks": {"SessionStart": [entry(dead_cmds[0])]}}))
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = chs.main(["install", "--settings", str(inproc), "--command", live_cmd, "--matcher", "compact"])
    check(rc == 0 and "removed 1 dead personal-claude-compact-hint.sh entry:" in out.getvalue()
          and "personal-claude-compact-hint.sh SessionStart hook (installed)" in out.getvalue(),
          "main(): one dead entry → singular 'entry', default label, rc 0", out.getvalue() + err.getvalue())
    inproc.write_text(json.dumps({"hooks": {"SessionStart": [entry(c) for c in dead_cmds[:2]]}}))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = chs.main(["install", "--settings", str(inproc), "--command", live_cmd, "--label", "L"])
    check(rc == 0 and "removed 2 dead personal-claude-compact-hint.sh entries:" in out.getvalue()
          and "L (installed)" in out.getvalue(), "main(): two dead entries → plural, custom label", out.getvalue())
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = chs.main(["install", "--settings", str(inproc), "--command", live_cmd, "--label", "L"])
    check(rc == 0 and "✂" not in out.getvalue() and "L (already installed)" in out.getvalue(),
          "main(): nothing to remove → no ✂ line, already installed", out.getvalue())
    badp = tmp / "inproc-bad" / ".claude" / "settings.json"
    badp.parent.mkdir(parents=True)
    badp.write_text("[]")
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = chs.main(["install", "--settings", str(badp), "--command", live_cmd])
    check(rc == 1 and "not an object" in err.getvalue() and out.getvalue() == "",
          "main(): malformed settings → rc 1, reason on stderr, nothing on stdout", err.getvalue())

    # ── parser branches ────────────────────────────────────────────────────────
    check(chs.script_path_of('bash "/unbalanced/quote.sh') == "/unbalanced/quote.sh".join(['"', ""]) or
          chs.script_path_of('bash "/unbalanced/quote.sh') == '"/unbalanced/quote.sh',
          "an unbalanced quote falls back to whitespace splitting instead of raising")
    check(chs.script_path_of("cp a b") == "cp", "a command with no interpreter yields its first token")
    check(chs.script_path_of("--flag-only") is None, "a command of only flags yields None")
    check(chs.script_path_of("bash") is None, "an interpreter with nothing after it yields None")
    check(chs.script_path_of("   ") is None, "whitespace yields None")

    # ── prune_dead edge branches ───────────────────────────────────────────────
    check(chs.prune_dead({"hooks": {"SessionStart": [entry(dead_cmds[0])]}}, "SessionStart", "") == [],
          "an empty family prunes nothing")
    odd = {"hooks": {"SessionStart": ["not-a-dict", entry(dead_cmds[0])]}}
    removed = chs.prune_dead(odd, "SessionStart", "personal-claude-compact-hint.sh")
    check(removed == [dead_cmds[0]] and odd["hooks"]["SessionStart"][0] == "not-a-dict",
          "a non-dict entry is kept in place while the dead one beside it is removed")


    # ── an unexpanded variable is not judgeable by existence ──────────────────
    # Re-added as an absolute path, never the portable one: a false prune is permanent.
    var_cmd = 'bash "$CLAUDE_PROJECT_DIR/src/personal-claude-compact-hint.sh"'
    var_settings = {"hooks": {"SessionStart": [entry(var_cmd)]}}
    var_removed = chs.prune_dead(var_settings, "SessionStart",
                                 "personal-claude-compact-hint.sh")
    check(var_removed == [],
          "a hook whose path carries an unexpanded variable is never pruned",
          f"removed {var_removed}")
    check(var_settings["hooks"]["SessionStart"] != [],
          "that hook survives in the settings it was found in")
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td)
        (proj / "src").mkdir()
        (proj / "src" / "personal-claude-compact-hint.sh").write_text("#!/bin/bash\n")
        kept = {"hooks": {"SessionStart": [entry(var_cmd)]}}
        check(chs.prune_dead(kept, "SessionStart",
                             "personal-claude-compact-hint.sh",
                             project_dir=proj) == [],
              "nor when a project_dir is supplied and the expansion would resolve")

    # ── a copy left by an OLDER install tree that still exists is re-pointed, not kept ──
    # Its script exists, so prune_dead keeps it; without re-pointing it fires from the old tree.
    old_tree = tmp / "old app" / "engine" / "sutando"
    new_tree = tmp / "new app" / "engine" / "sutando"
    for tree in (old_tree, new_tree):
        (tree / "src").mkdir(parents=True)
        (tree / "src" / "watcher-rearm-session-hint.sh").write_text("#!/bin/bash\n")
    old_cmd = f'bash "{old_tree}/src/watcher-rearm-session-hint.sh"'
    new_cmd = f'bash "{new_tree}/src/watcher-rearm-session-hint.sh"'
    custom = f'bash -x "{old_tree}/src/watcher-rearm-session-hint.sh"'
    moved = tmp / "moved" / ".claude" / "settings.json"
    moved.parent.mkdir(parents=True)
    moved.write_text(json.dumps({"hooks": {"SessionStart": [
        entry(old_cmd, matcher="compact|resume"), entry(custom, matcher=""),
        entry("echo not-sutando", matcher="")]}}))
    status, removed = chs.install(moved, event="SessionStart", command=new_cmd, matcher="compact|resume")
    got = json.loads(moved.read_text())["hooks"]["SessionStart"]
    cmds = [h["command"] for e in got for h in e["hooks"]]
    check(status == "re-pointed" and removed == [], "an older tree's copy reports re-pointed", status)
    check(got[0] == entry(new_cmd, matcher="compact|resume"),
          "the copy is rewritten in place to this checkout, matcher kept", json.dumps(got))
    check(old_cmd not in cmds and cmds.count(new_cmd) == 1, "no entry still runs the older tree's script")
    check(custom in cmds, "an operator variant of the same script (extra flag) is not ours and stays")
    check("echo not-sutando" in cmds, "a foreign non-Sutando hook stays")
    status, _ = chs.install(moved, event="SessionStart", command=new_cmd, matcher="compact|resume")
    check(status == "already installed", "a hook already on this checkout is skipped", status)

    # A copy beside an already-correct entry is dropped rather than doubled.
    both = tmp / "both" / ".claude" / "settings.json"
    both.parent.mkdir(parents=True)
    both.write_text(json.dumps({"hooks": {"SessionStart": [entry(new_cmd), entry(old_cmd)]}}))
    status, _ = chs.install(both, event="SessionStart", command=new_cmd, matcher="compact")
    cmds = [h["command"] for e in json.loads(both.read_text())["hooks"]["SessionStart"] for h in e["hooks"]]
    check(status == "re-pointed" and cmds == [new_cmd], "a copy beside the correct entry is dropped", str(cmds))
    odd = {"hooks": {"SessionStart": ["not-a-dict", entry(old_cmd), entry(new_cmd)]}}
    changed = chs.repoint_other_checkouts(odd, "SessionStart", new_cmd, "compact")
    check(changed == [old_cmd] and odd["hooks"]["SessionStart"] == ["not-a-dict", entry(new_cmd)],
          "a copy BEFORE the correct entry is rewritten once, a non-dict entry kept", json.dumps(odd))

    # A copy under another matcher is not ours: re-pointing it would drop or change a trigger.
    def run_rows(rows, requested):
        path = tmp / f"matcher-{len(list(tmp.glob('matcher-*')))}" / ".claude" / "settings.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"hooks": {"SessionStart": [entry(c, matcher=m) for c, m in rows]}}))
        status, _ = chs.install(path, event="SessionStart", command=new_cmd, matcher=requested)
        got = json.loads(path.read_text())["hooks"]["SessionStart"]
        return status, sorted((h["command"], e["matcher"]) for e in got for h in e["hooks"])

    status, got = run_rows([(old_cmd, "compact"), (new_cmd, "resume")], "resume")
    check(got == sorted([(old_cmd, "compact"), (new_cmd, "resume")]),
          "old@compact + new@resume, requesting resume: the resume trigger is kept", f"{status} {got}")
    status, got = run_rows([(new_cmd, "resume"), (old_cmd, "compact")], "resume")
    check(got == sorted([(new_cmd, "resume"), (old_cmd, "compact")]),
          "new@resume + old@compact, requesting resume: the compact trigger is kept", f"{status} {got}")
    status, got = run_rows([(old_cmd, "compact")], "compact|resume")
    check(status == "installed" and (new_cmd, "compact|resume") in got,
          "old@compact, requesting compact|resume: the requested matcher is applied", f"{status} {got}")

    # The exact command under another matcher does not count as installed for the requested one.
    status, got = run_rows([(new_cmd, "compact")], "compact|resume")
    check(status == "installed" and got == sorted([(new_cmd, "compact"), (new_cmd, "compact|resume")]),
          "new@compact, requesting compact|resume: added alongside, compact entry kept", f"{status} {got}")
    path = tmp / "matcher-rerun" / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"hooks": {"SessionStart": [entry(new_cmd, matcher="compact")]}}))
    for _ in range(2):
        status, _ = chs.install(path, event="SessionStart", command=new_cmd, matcher="compact|resume")
    rows = sorted((h["command"], e["matcher"]) for e in json.loads(path.read_text())["hooks"]["SessionStart"]
                  for h in e["hooks"])
    check(status == "already installed" and rows == sorted([(new_cmd, "compact"), (new_cmd, "compact|resume")]),
          "rerun with compact|resume is idempotent", f"{status} {rows}")
    no_matcher = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": new_cmd}]}]}}
    path = tmp / "matcher-absent" / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(no_matcher))
    status, _ = chs.install(path, event="SessionStart", command=new_cmd, matcher="")
    check(status == "already installed", "an entry with no matcher key counts as the empty matcher", status)

    check(not chs.is_other_checkout_copy(f'bash "{tmp}/elsewhere/watcher-rearm-session-hint.sh"', new_cmd),
          "a same-named script outside a src/, scripts/ or skills/ tail is not a checkout copy")

    # The real installer, end to end, on the reported hook.
    work2 = tmp / "work2"
    (work2 / ".claude").mkdir(parents=True)
    stale = f'bash "{old_tree}/src/watcher-rearm-session-hint.sh"'
    (work2 / ".claude" / "settings.json").write_text(json.dumps({"hooks": {
        "SessionStart": [entry(stale, matcher="compact|resume")]}}))
    r = subprocess.run(["bash", str(REPO / "scripts" / "install-watcher-rearm-hook.sh")], capture_output=True,
                       text=True, env=dict(os.environ, SUTANDO_CLAUDE_WORKING_DIR=str(work2)), timeout=30)
    cmds = [h["command"] for e in json.loads((work2 / ".claude" / "settings.json").read_text())["hooks"]["SessionStart"]
            for h in e["hooks"]]
    check(r.returncode == 0 and cmds == [f'bash "{REPO}/src/watcher-rearm-session-hint.sh"']
          and "(re-pointed)" in r.stdout, "install-watcher-rearm-hook.sh re-points an older tree's entry",
          r.stdout + r.stderr + str(cmds))

    # ── the current entry's own settings survive, whichever order it sits in ──────
    # An older live checkout's copy before the current entry must not replace it: the
    # current hook owns its timeout and async metadata.
    older = tmp / "older" / "src" / "personal-claude-compact-hint.sh"
    older.parent.mkdir(parents=True, exist_ok=True)
    older.write_text("#!/bin/bash\n")
    older_cmd = f'bash "{older}"'
    check(chs.is_other_checkout_copy(older_cmd, live_cmd), "fixture: the older tree's entry is a copy")

    def rich(cmd, timeout, matcher="compact", **extra):
        return {"matcher": matcher,
                "hooks": [dict({"type": "command", "command": cmd, "timeout": timeout}, **extra)]}

    CURRENT = dict(timeout=600, **{"async": True, "statusMessage": "keep me"})
    for label, order in (
            ("older first", lambda: [rich(older_cmd, 1), rich(live_cmd, **CURRENT)]),
            ("current first", lambda: [rich(live_cmd, **CURRENT), rich(older_cmd, 1)])):
        s = {"hooks": {"SessionStart": order()}}
        chs.repoint_other_checkouts(s, "SessionStart", live_cmd, matcher="compact")
        hooks = [h for e in s["hooks"]["SessionStart"] for h in (e.get("hooks") or [])]
        check(len(hooks) == 1 and hooks[0].get("command") == live_cmd,
              f"{label}: one current entry survives", str(hooks))
        check(len(hooks) == 1 and hooks[0].get("timeout") == 600,
              f"{label}: the current entry's timeout survives", str(hooks))
        check(len(hooks) == 1 and hooks[0].get("async") is True
              and hooks[0].get("statusMessage") == "keep me",
              f"{label}: the current entry's async metadata survives", str(hooks))

    # Two older copies and no current entry: the first in file order is the one re-pointed,
    # so the entry keeps its position; the rest are dropped as duplicates.
    older2 = tmp / "older2" / "src" / "personal-claude-compact-hint.sh"
    older2.parent.mkdir(parents=True, exist_ok=True)
    older2.write_text("#!/bin/bash\n")
    s = {"hooks": {"SessionStart": [rich(older_cmd, 1), rich(f'bash "{older2}"', 2)]}}
    chs.repoint_other_checkouts(s, "SessionStart", live_cmd, matcher="compact")
    hooks = [h for e in s["hooks"]["SessionStart"] for h in (e.get("hooks") or [])]
    check(len(hooks) == 1 and hooks[0].get("command") == live_cmd and hooks[0].get("timeout") == 1,
          "two older copies, no current: the first in file order is re-pointed", str(hooks))

    # A current entry under another matcher is not ours to touch.
    s = {"hooks": {"SessionStart": [rich(older_cmd, 1), rich(live_cmd, 600, matcher="resume")]}}
    chs.repoint_other_checkouts(s, "SessionStart", live_cmd, matcher="compact")
    kept = s["hooks"]["SessionStart"]
    check(any(e.get("matcher") == "resume" and e["hooks"][0].get("timeout") == 600 for e in kept),
          "a current entry under another matcher is left untouched", str(kept))

    # A non-dict entry and a non-dict hook are carried through untouched.
    s = {"hooks": {"SessionStart": ["not-an-entry", {"matcher": "compact", "hooks": ["not-a-hook"]},
                                    rich(older_cmd, 1)]}}
    chs.repoint_other_checkouts(s, "SessionStart", live_cmd, matcher="compact")
    kept = s["hooks"]["SessionStart"]
    check("not-an-entry" in kept and any(e.get("hooks") == ["not-a-hook"] for e in kept if isinstance(e, dict)),
          "a non-dict entry and a non-dict hook survive the migration", str(kept))

    # Idempotence: a second pass over the settled settings changes nothing.
    s = {"hooks": {"SessionStart": [rich(older_cmd, 1), rich(live_cmd, **CURRENT)]}}
    chs.repoint_other_checkouts(s, "SessionStart", live_cmd, matcher="compact")
    once = json.dumps(s, sort_keys=True)
    again = chs.repoint_other_checkouts(s, "SessionStart", live_cmd, matcher="compact")
    check(again == [] and json.dumps(s, sort_keys=True) == once,
          "running the migration twice changes nothing", once)


print(f"\n{_pass} passed, {_fail} failed")
sys.exit(1 if _fail else 0)
