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

if FAILURES:
    print(f"\nFAIL — {len(FAILURES)} check(s):")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1)
print("\nPASS — review-authority-guard tests")
