#!/usr/bin/env python3
"""PreToolUse review-authority-guard: formal GitHub reviews (APPROVE /
REQUEST_CHANGES) must be DENIED while the owner's ruling is unresolved, while
dismissals, plain comments and every non-review command pass through
(hooks/review-authority-guard.py).

Run:  python3 tests/review-authority-guard.test.py
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HOOK = str(Path(__file__).resolve().parent.parent / "hooks" / "review-authority-guard.py")
FAILURES = []


def run(command, mode="__absent__", tool="Bash", env_extra=None):
    """Invoke the hook with a workspace whose authority state is `mode`."""
    with tempfile.TemporaryDirectory() as td:
        os.makedirs(os.path.join(td, "state"), exist_ok=True)
        if mode != "__absent__":
            with open(os.path.join(td, "state", "authority.json"), "w") as fh:
                fh.write(mode if mode.startswith("{") else json.dumps({"github_formal_review": mode}))
        env = dict(os.environ)
        env["SUTANDO_HOOK_WORKSPACE"] = td
        env.pop("SUTANDO_ALLOW_FORMAL_GH_REVIEWS", None)
        env.update(env_extra or {})
        p = subprocess.run([sys.executable, HOOK], input=json.dumps(
            {"tool_name": tool, "tool_input": {"command": command}}),
            capture_output=True, text=True, env=env)
    denied = '"permissionDecision": "deny"' in p.stdout
    return denied, p.stdout, p.returncode


def check(label, got, want):
    if got != want:
        FAILURES.append(f"{label}: got {got!r}, want {want!r}")
        print(f"  FAIL {label}: got {got!r} want {want!r}")
    else:
        print(f"  ok   {label}")


APPROVE = "gh pr review 3679 --repo sonichi/sutando --approve --body-file /tmp/r.md"
REQCH = "gh pr review 42 --request-changes --body 'no'"
COMMENT = "gh pr review 42 --comment --body 'note'"
DISMISS = "gh api --method PUT repos/o/r/pulls/3679/reviews/5082607312/dismissals --input /tmp/d.json"

print("1. hold (the default while unanswered) denies every formal review")
check("approve denied", run(APPROVE, "hold")[0], True)
check("request-changes denied", run(REQCH, "hold")[0], True)
check("comment denied under hold", run(COMMENT, "hold")[0], True)

print("2. MISSING state file behaves as findings-only — votes gated, --comment stays possible")
# A PATH-QUALIFIED gh is still gh. The scan compared the whole word against "gh",
# so an absolute path was not classified as a review at all and the gate let it by.
check("approve denied when gh is path-qualified",
      run("/opt/homebrew/bin/" + APPROVE, "hold")[0], True)
check("approve denied when gh is relative-pathed",
      run("./bin/" + APPROVE, "hold")[0], True)
check("request-changes denied when gh is path-qualified",
      run("/usr/local/bin/" + REQCH, "hold")[0], True)
# The basename match must stay EXACT: a different binary ending in those two
# letters is not gh, and denying it would be a false positive.
check("a binary merely ending in gh is NOT gh",
      run("/usr/bin/notgh pr review 1 --approve", "hold")[0], False)

# macOS filesystems are case-insensitive, so `GH` is the same binary, and the
# early `"gh" not in command` prefilter ran before the token scan lowercased.
check("approve denied when the path is mixed-case",
      run("/opt/homebrew/bin/" + APPROVE.replace("gh ", "GH ", 1), "hold")[0], True)
check("approve denied when a bare gh is uppercase",
      run(APPROVE.replace("gh ", "GH ", 1), "hold")[0], True)
check("request-changes denied when the path is mixed-case",
      run("/usr/local/bin/" + REQCH.replace("gh ", "Gh ", 1), "hold")[0], True)
# Same boundary as the lowercase control: case-folding must not widen the match.
check("a mixed-case binary merely ending in gh is NOT gh",
      run("/usr/bin/notGH pr review 1 --approve", "hold")[0], False)
check("an uppercase NON-review gh subcommand is still allowed",
      run("GH pr view 1", "hold")[0], False)

# keweichen's coverage note: the `gh api .../reviews` branch was only ever
# exercised UNQUALIFIED, so reverting the basename fix left every path arm green.
check("the api review form is denied when gh is path-qualified",
      run("/opt/homebrew/bin/gh api repos/o/r/pulls/1/reviews -f event=APPROVE",
          "hold")[0], True)
check("the api review form is denied when gh is mixed-case",
      run("/opt/homebrew/bin/GH api repos/o/r/pulls/1/reviews -f event=APPROVE",
          "hold")[0], True)

check("approve denied with no state file", run(APPROVE)[0], True)
check("request-changes denied with no state file", run(REQCH)[0], True)
check("comment ALLOWED with no state file", run(COMMENT)[0], False)

print("2b. a PRESENT but unreadable/unknown state file is hold — a ruling exists and cannot be read")
check("unparseable state denies approve", run(APPROVE, "{not json")[0], True)
check("unparseable state denies comment", run(COMMENT, "{not json")[0], True)
check("unknown mode value denies approve", run(APPROVE, "yolo")[0], True)
check("unknown mode value denies comment", run(COMMENT, "yolo")[0], True)

print("3. findings-only allows --comment but still gates the votes")
check("approve denied", run(APPROVE, "findings-only")[0], True)
check("request-changes denied", run(REQCH, "findings-only")[0], True)
check("comment ALLOWED", run(COMMENT, "findings-only")[0], False)

print("4. allow lets everything through")
check("approve allowed", run(APPROVE, "allow")[0], False)
check("request-changes allowed", run(REQCH, "allow")[0], False)

print("5. reductions and unrelated commands are never gated")
check("dismissal allowed under hold", run(DISMISS, "hold")[0], False)
# Discriminating case: a dismissal whose MESSAGE contains the word APPROVE —
# without the dismissal skip the event regex matches the prose and blocks a REDUCTION.
check("dismissal naming APPROVE in its message still allowed", run(
    "gh api --method PUT repos/o/r/pulls/42/reviews/9/dismissals "
    "-f message='re-file this as your own APPROVE if useful' -f event=DISMISS",
    "hold")[0], False)
check("plain pr comment allowed", run("gh pr comment 42 --body hi", "hold")[0], False)
check("pr view allowed", run("gh pr view 42 --json state", "hold")[0], False)
check("unrelated command allowed", run("git status", "hold")[0], False)
check("non-Bash tool ignored", run(APPROVE, "hold", tool="Read")[0], False)

print("6. compound commands cannot smuggle a review past the split")
check("&& chain denied", run(f"cd /tmp && {APPROVE}", "hold")[0], True)
check("semicolon chain denied", run(f"echo hi; {APPROVE}", "hold")[0], True)
check("gh api reviews with event=APPROVE denied",
      run("gh api repos/o/r/pulls/42/reviews -f event=APPROVE", "hold")[0], True)
# Discriminating case: an EARLIER benign `gh` must not shadow a later review —
# unsplit, the first `gh` is `gh pr view`, adjacency fails, the approve slips through.
check("benign gh first, review second, still denied",
      run(f"gh pr view 1 --json state && {APPROVE}", "hold")[0], True)
# Discriminating case for per-segment splitting: a dismissal CHAINED with a fresh
# approve — unsplit, the dismissal match skips the whole string, approve included.
check("dismissal chained with an approve does NOT shield it", run(
    f"{DISMISS} && gh api repos/o/r/pulls/42/reviews -f event=APPROVE", "hold")[0], True)

print("7. escape hatch")
check("env override allows", run(APPROVE, "hold",
      env_extra={"SUTANDO_ALLOW_FORMAL_GH_REVIEWS": "1"})[0], False)

print("8. the denial must be actionable, not a bare refusal")
_, out, _ = run(APPROVE, "hold")
for token in ("authority.json", "in-room", "SUTANDO_ALLOW_FORMAL_GH_REVIEWS", "--comment",
              '{\\"github_formal_review\\": \\"findings-only\\"}'):
    check(f"reason names {token}", token in out, True)

print("9b. a DEPLOYED copy (outside the repo) still finds the state file")
import shutil
with tempfile.TemporaryDirectory() as td:
    ws = os.path.join(td, "workspace")
    os.makedirs(os.path.join(ws, "state"))
    with open(os.path.join(ws, "state", "authority.json"), "w") as fh:
        json.dump({"github_formal_review": "allow"}, fh)
    depdir = os.path.join(ws, ".claude-sutando", "hooks")   # the real deploy layout
    os.makedirs(depdir)
    dep = os.path.join(depdir, "review-authority-guard.py")
    shutil.copy(HOOK, dep)
    env = dict(os.environ); env.pop("SUTANDO_HOOK_WORKSPACE", None)
    env.pop("SUTANDO_ALLOW_FORMAL_GH_REVIEWS", None)
    r = subprocess.run([sys.executable, dep], input=json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": APPROVE}}),
        capture_output=True, text=True, env=env)
    # 'allow' must reach the deployed copy: if it cannot find the file it reads
    # 'hold' and denies — passing for the wrong reason is what this case catches.
    check("deployed hook READS the state file (allow -> permitted)",
          '"permissionDecision": "deny"' in r.stdout, False)

print("9. the hook never wedges the core")
check("exit code is 0 even when denying", run(APPROVE, "hold")[2], 0)
check("malformed stdin fails OPEN", subprocess.run(
    [sys.executable, HOOK], input="not json", capture_output=True, text=True).returncode, 0)

_spec = importlib.util.spec_from_file_location("review_authority_guard", HOOK)
_guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_guard)
classify = _guard.classify
print("10. shell-wrapper indirection: the inner command is one shlex token and is re-classified")
_R = "gh pr review"
for _cmd, _want in (
    (f'bash -c "{_R} 123 --approve"', "APPROVE"),
    (f"bash -c '{_R} 123 --approve'", "APPROVE"),
    (f'sh -c "{_R} 123 --request-changes -b x"', "REQUEST_CHANGES"),
    (f'/bin/zsh -lc "cd repo && {_R} 123 -a"', "APPROVE"),
    (f'eval "{_R} 123 --approve"', "APPROVE"),
    (f'bash -c "{_R} 123 --comment -b ok"', "COMMENT"),
    ('bash -c "gh pr view 123"', None),
    ('bash -c "echo hello"', None),
):
    check(f"wrapper: {_cmd}", classify(_cmd), _want)

print("11. interpreter indirection: -c/-e strings and list-literal argv are de-literalised")
_R = "gh pr review"
for _cmd, _want in (
    ("""python3 -c "import subprocess; subprocess.run(['gh','pr','review','123','--approve'])" """, "APPROVE"),
    ("""python3 -c 'import subprocess; subprocess.run(["gh", "pr", "review", "123", "--request-changes", "-b", "x"])' """, "REQUEST_CHANGES"),
    ("""python3.12 -c "subprocess.run(['gh','pr','review','7','--comment','-b','ok'])" """, "COMMENT"),
    (f"""node -e "require('child_process').execSync('{_R} 5 --approve')" """, "APPROVE"),
    ("""python3 -c "subprocess.run(['gh','api','repos/o/r/pulls/3/reviews','-f','event=APPROVE'])" """, "APPROVE"),
    ('python3 -c "print(\'hello\')"', None),
    ("python3 -c \"subprocess.run(['gh','pr','view','123'])\"", None),
):
    check(f"interp: {_cmd.strip()}", classify(_cmd), _want)

print("12. gh api reviews: the event ASSIGNMENT decides, body prose never does")
for _cmd, _want in (
    ("gh api repos/o/r/pulls/3/reviews -f event=COMMENT -f body='I do not APPROVE of this'", "COMMENT"),
    ("gh api repos/o/r/pulls/3/reviews -f event=COMMENT -f body='needs REQUEST_CHANGES later'", "COMMENT"),
    ("gh api repos/o/r/pulls/3/reviews -f event=APPROVE -f body='ok'", "APPROVE"),
    ("gh api repos/o/r/pulls/3/reviews --raw-field event=REQUEST_CHANGES", "REQUEST_CHANGES"),
    ("""gh api repos/o/r/pulls/3/reviews --input - <<< '{"event": "APPROVE", "body": "x"}'""", "APPROVE"),
    ("gh api repos/o/r/pulls/3/reviews -f body='APPROVE this please'", None),
):
    check(f"api: {_cmd}", classify(_cmd), _want)

print("13. heredoc indirection: program text on stdin is de-literalised and classified")
for _cmd, _want in (
    ("python3 - <<'PY'\nimport subprocess\nsubprocess.run(['gh','pr','review','3756','--approve','--body','ok'])\nPY", "APPROVE"),
    ("python3 - <<PY\nimport subprocess\nsubprocess.run([\"gh\", \"pr\", \"review\", \"1\", \"--request-changes\", \"-b\", \"x\"])\nPY\necho done", "REQUEST_CHANGES"),
    ("python3 - <<'PY'\nsubprocess.run(['gh','pr','review','1','--comment','-b','ok'])\nPY", "COMMENT"),
    ("gh api repos/o/r/pulls/3/reviews --input - <<'JSON'\n{\"event\": \"APPROVE\", \"body\": \"x\"}\nJSON", "APPROVE"),
    ("python3 - <<'PY'\nprint('hello gh')\nPY", None),
    ("cat <<'EOF' > note.md\ngh is installed; review the pr later\nEOF", None),
    # A heredoc owned by cat/tee is documentation, whatever it quotes (john-the-dev, #3756).
    (f"cat > /tmp/d.md <<'DOC'\nTo approve, run {_R} 123 --approve and you are done.\nDOC", None),
    (f"tee notes.md <<EOF\nI usually {_R} 5 --request-changes when the tests are red.\nEOF", None),
    (f"cat > review.md <<'MD'\nVerified at abc: {_R} 7 --approve returns APPROVE, dismissal returns None.\nMD\ngh pr comment 7 --body-file review.md", None),
):
    check(f"heredoc: {_cmd.splitlines()[0]} …", classify(_cmd), _want)

print("14. scripts/authority.py is the sanctioned writer: a recorded ruling reaches the hook")
CLI = str(Path(HOOK).parent.parent / "scripts" / "authority.py")
# Under the coverage gate, children run through `coverage run` so the CLI and hook lines count.
PYBASE = [sys.executable]
if os.environ.get("SUTANDO_TEST_SUBPROCESS_COVERAGE") == "1":
    PYBASE += ["-m", "coverage", "run", f"--rcfile={Path(HOOK).parent.parent / '.coveragerc'}"]


def cli(ws, *args):
    env = dict(os.environ)
    env["SUTANDO_HOOK_WORKSPACE"] = ws
    return subprocess.run([*PYBASE, CLI, *args], capture_output=True, text=True, env=env)


def _raw(path):
    return Path(path).read_text() if os.path.exists(path) else ""


def _rd(path):
    try:
        return json.loads(_raw(path) or "{}")
    except ValueError:
        return {}


def hook_in(ws, command=APPROVE):
    env = dict(os.environ)
    env["SUTANDO_HOOK_WORKSPACE"] = ws
    env.pop("SUTANDO_ALLOW_FORMAL_GH_REVIEWS", None)
    p = subprocess.run([*PYBASE, HOOK], input=json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": command}}),
        capture_output=True, text=True, env=env)
    return '"permissionDecision": "deny"' in p.stdout, p.stdout


with tempfile.TemporaryDirectory() as ws:
    state = os.path.join(ws, "state", "authority.json")
    r = cli(ws, "get")
    check("get on a missing file names the default", (r.returncode, "findings-only" in r.stdout,
          "does not exist" in r.stdout), (0, True, True))
    r = cli(ws, "set", "github_formal_review", "allow", "--source", "owner DM 2026-08-25")
    check("RAISE without --owner-event/--quote refused", (r.returncode, "explicit owner ruling" in r.stderr,
          os.path.exists(state)), (2, True, False))
    r = cli(ws, "set", "github_formal_review", "allow", "--source", "owner DM", "--owner-event", "$abc123")
    check("RAISE with --owner-event but no --quote refused", (r.returncode, os.path.exists(state)), (2, False))
    r = cli(ws, "set", "github_formal_review", "allow", "--source", "owner DM 2026-08-25",
            "--owner-event", "$abc123", "--quote", "you do formal reviews")
    check("set allow exits 0", r.returncode, 0)
    rec = _rd(state)
    check("set records the mode", rec.get("github_formal_review"), "allow")
    check("set records the source", rec.get("source"), "owner DM 2026-08-25")
    check("set stamps granted_at in UTC", str(rec.get("granted_at", "")).endswith("Z"), True)
    out = cli(ws, "get").stdout
    check("get round-trips the recorded mode", "github_formal_review: allow" in out, True)
    check("set records owner_event and quote, get shows them",
          (_rd(state).get("owner_event"), _rd(state).get("quote"),
           "owner_event=$abc123" in out, "quote=you do formal reviews" in out),
          ("$abc123", "you do formal reviews", True, True))
    check("get ignores the legacy key positional", cli(ws, "get", "github_formal_review").returncode, 0)
    check("after set allow the hook ALLOWS --approve", hook_in(ws)[0], False)
    r = cli(ws, "set", "github_formal_review", "findings-only", "--source", "owner DM")
    check("LOWER without --owner-event/--quote succeeds", (r.returncode, _rd(state).get("github_formal_review"),
          "owner_event" in _rd(state)), (0, "findings-only", False))
    check("after set findings-only the hook DENIES --approve", hook_in(ws)[0], True)
    check("...and still allows --comment", hook_in(ws, COMMENT)[0], False)
    before = _raw(state)
    r = cli(ws, "set", "github_formal_review", "yes-please", "--source", "x")
    check("invalid mode refused with non-zero exit", r.returncode != 0, True)
    check("invalid mode leaves the file untouched", _raw(state), before)
    r = cli(ws, "set", "github_formal_review", "allow", "--source", "  ")
    check("blank --source refused", (r.returncode != 0, _raw(state) == before), (True, True))
    check("atomic write leaves no temp files", sorted(os.listdir(os.path.dirname(state))) if os.path.isdir(os.path.dirname(state)) else [], ["authority.json"])

with tempfile.TemporaryDirectory() as ws:
    os.makedirs(os.path.join(ws, "state"))
    state = os.path.join(ws, "state", "authority.json")
    with open(state, "w") as fh:
        json.dump({"github_formal_review": "hold", "other_ruling": "keep-me"}, fh)
    cli(ws, "set", "github_formal_review", "allow", "--source", "owner", "--owner-event", "$e", "--quote", "yes")
    check("set preserves other keys", _rd(state).get("other_ruling"), "keep-me")

with tempfile.TemporaryDirectory() as ws:
    os.makedirs(os.path.join(ws, "state"))
    state = os.path.join(ws, "state", "authority.json")
    Path(state).write_text("{not json")
    r = cli(ws, "get")
    check("get on an unreadable file reports hold", (r.returncode, "github_formal_review: hold" in r.stdout), (0, True))
    r = cli(ws, "set", "github_formal_review", "hold", "--source", "owner")
    check("corrupt file NOT overwritten without --replace-corrupt",
          (r.returncode, "--replace-corrupt" in r.stderr, _raw(state)), (2, True, "{not json"))
    r = cli(ws, "set", "github_formal_review", "hold", "--source", "owner", "--replace-corrupt")
    check("corrupt file replaced with --replace-corrupt, said on stderr",
          (r.returncode, "replacing corrupt" in r.stderr, _rd(state).get("github_formal_review")), (0, True, "hold"))
    Path(state).write_text("[1, 2]")
    r = cli(ws, "set", "github_formal_review", "hold", "--source", "owner")
    check("non-dict JSON file NOT overwritten", (r.returncode, _raw(state)), (2, "[1, 2]"))

with tempfile.TemporaryDirectory() as ws:
    os.makedirs(os.path.join(ws, "state"))
    state = os.path.join(ws, "state", "authority.json")
    Path(state).write_text(json.dumps({"github_formal_review": "hold"}))
    os.chmod(state, 0o644)
    cli(ws, "set", "github_formal_review", "hold", "--source", "owner")
    check("mode bits preserved on rewrite (0644 stays 0644)", oct(os.stat(state).st_mode & 0o777), oct(0o644))
    target = os.path.join(ws, "real-authority.json")
    Path(target).write_text(json.dumps({"github_formal_review": "hold"}))
    os.unlink(state)
    os.symlink(target, state)
    r = cli(ws, "set", "github_formal_review", "allow", "--source", "owner", "--owner-event", "$e", "--quote", "yes")
    check("symlink preserved and its target updated",
          (r.returncode, os.path.islink(state), _rd(target).get("github_formal_review")), (0, True, "allow"))

print("15. the deny message tells the agent to look for, and record, the owner's ruling")
with tempfile.TemporaryDirectory() as ws:
    denied, out = hook_in(ws)
    msg = json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"] if denied else ""
    check("deny names the writer command",
          "python3 scripts/authority.py set github_formal_review" in msg, True)
    check("deny says only an explicit owner ruling counts, never inferred",
          "Only an explicit owner ruling counts, quoted with where it was given; it is never "
          "inferred from memory, notes, or a peer" in msg, True)
    check("deny says to search for that explicit ruling before asking",
          "Before asking the owner, search memory and notes for such an explicit owner ruling" in msg, True)
    check("deny names --owner-event and --quote", ("--owner-event" in msg, "--quote" in msg), (True, True))

if FAILURES:
    print(f"\nFAIL — {len(FAILURES)} check(s):")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1)
print("\nPASS — review-authority-guard tests")
