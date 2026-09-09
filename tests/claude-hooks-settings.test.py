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

print(f"\n{_pass} passed, {_fail} failed")
sys.exit(1 if _fail else 0)
