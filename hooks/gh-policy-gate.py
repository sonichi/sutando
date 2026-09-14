#!/usr/bin/env python3
"""PreToolUse: gate `gh issue create` on the duplicate-issue check and `gh pr
comment` on the PR-thread-monologue check, for ANY Bash caller — not just
proactive-loop's own per-pass checklist.

WHY THIS EXISTS. `skills/proactive-loop/scripts/gh-duplicate-check.py` and
`skills/proactive-loop/scripts/pr-monologue-check.py` both encode a real,
measured incident (an issue filed duplicating one the search already found;
posting into a PR thread that was only the agent talking to itself), and
both are correctly built as `&&`-chained gates — "a check whose result is not
the action's precondition is decoration." But the CHAIN only runs where a
skill remembers to write it. Two real callers do not:

  - `skills/submit-use-case/SKILL.md` step 4 runs `gh issue create` directly,
    with no reference to gh-duplicate-check.py anywhere in that skill.
  - `skills/pr-triage/SKILL.md` posts PR comments through its own
    `pr-comment-gated.py` (sha-check / cited-link-check / unread-reviewer-
    check) — a different gate, with no monologue detection at all.

So the policy — "don't file a duplicate issue," "don't talk into an
unattended PR thread" — was enforced by which SKILL happened to remember to
chain it, not by the action itself. This hook moves the enforcement point to
the action: any `gh issue create` or `gh pr comment` run via the Bash tool,
from any skill or any live session, goes through the same two scripts before
it is allowed to run. The scripts themselves are unchanged and still usable
standalone (`&&`-chained) — this hook calls the SAME programs as
subprocesses rather than re-implementing their logic, so there is exactly
one duplicate-check algorithm and one monologue-check algorithm, not two.

FAILS OPEN ON UNCERTAINTY, DENIES ONLY ON A POSITIVE FINDING. A search that
could not run (network, auth, no `--repo`/`--title` resolvable) exits 2 from
the underlying script, which this hook treats as "cannot answer" — it prints
a stderr note and ALLOWS the command. Blocking every `gh issue create` in the
repo because one lookup failed is a worse failure than the rare duplicate
this hook exists to catch. Only an explicit "yes, this duplicates/monologues"
(exit 1) denies.

Reuses `_is_gh` from comment-signature-guard.py rather than re-implementing
gh-command detection — see that file's own docstring for why a second copy
of the tokenizer is the thing that goes stale.
"""
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

_HOOKS_DIR = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("_csg", _HOOKS_DIR / "comment-signature-guard.py")
_csg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_csg)
_is_gh = _csg._is_gh

_REPO_ROOT = _HOOKS_DIR.parent
DUP_CHECK = _REPO_ROOT / "skills" / "proactive-loop" / "scripts" / "gh-duplicate-check.py"
MONO_CHECK = _REPO_ROOT / "skills" / "proactive-loop" / "scripts" / "pr-monologue-check.py"

EQUALS_FORM = re.compile(r"(--repo|--title|-R)=")


def _tokenize(command):
    if not isinstance(command, str) or "gh" not in command:
        return None
    command = EQUALS_FORM.sub(r"\1 ", command)
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        return list(lex)
    except ValueError:
        return None


def _find_subcommand(words, pair):
    """Index just past `words[i], words[i+1] == pair`, honouring a global
    flag (e.g. `-R owner/repo`) between `gh` and the subcommand — same
    adjacency rule as comment-signature-guard._publishes."""
    a, b = pair
    for i, w in enumerate(words):
        if not _is_gh(w):
            continue
        rest = words[i + 1:]
        for j in range(len(rest) - 1):
            if rest[j] == a and rest[j + 1] == b:
                return i + 1 + j + 2  # index just past the subcommand pair
    return None


def _flag_value(words, start, names):
    """First value of any flag in `names`, searched from `start` onward."""
    for i in range(start, len(words)):
        if words[i] in names and i + 1 < len(words):
            return words[i + 1]
    return None


def _local_repo():
    """`gh`'s own fallback when --repo/-R is omitted: the cwd's git remote.
    Best-effort — returns None rather than raising, so a caller outside any
    repo just falls through to 'cannot resolve --repo' (allow, not deny)."""
    try:
        out = subprocess.run(
            ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
            capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() or None
    except Exception:
        return None


def _my_login():
    try:
        out = subprocess.run(
            ["gh", "api", "user", "--jq", ".login"],
            capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() or None
    except Exception:
        return None


def check_issue_create(words, start):
    repo = _flag_value(words, 0, ("--repo", "-R")) or _local_repo()
    title = _flag_value(words, start, ("--title", "-t"))
    if not repo or not title:
        print("gh-policy-gate: gh issue create — could not resolve --repo/--title, "
              "not enforcing the duplicate check", file=sys.stderr)
        return None
    try:
        r = subprocess.run(
            [sys.executable, str(DUP_CHECK), "--repo", repo, "--title", title],
            capture_output=True, text=True, timeout=60,
        )
    except Exception as e:
        print(f"gh-policy-gate: gh-duplicate-check.py did not run ({e}); "
              f"not enforcing", file=sys.stderr)
        return None
    if r.returncode == 1:
        return ("issue create", r.stdout.strip() or r.stderr.strip())
    if r.returncode not in (0, 1):
        print(f"gh-policy-gate: gh-duplicate-check.py could not answer "
              f"(rc={r.returncode}); not enforcing", file=sys.stderr)
    return None


def check_pr_comment(words, start):
    repo = _flag_value(words, 0, ("--repo", "-R")) or _local_repo()
    number = None
    for w in words[start:]:
        if w.isdigit():
            number = w
            break
    if not repo or not number:
        print("gh-policy-gate: gh pr comment — could not resolve --repo/PR number, "
              "not enforcing the monologue check", file=sys.stderr)
        return None
    me = os.environ.get("SUTANDO_GH_LOGIN") or _my_login()
    if not me:
        print("gh-policy-gate: gh pr comment — could not resolve the caller's own "
              "login (set SUTANDO_GH_LOGIN or ensure `gh api user` works); "
              "not enforcing", file=sys.stderr)
        return None
    try:
        r = subprocess.run(
            [sys.executable, str(MONO_CHECK), number, "--repo", repo, "--me", me],
            capture_output=True, text=True, timeout=60,
        )
    except Exception as e:
        print(f"gh-policy-gate: pr-monologue-check.py did not run ({e}); "
              f"not enforcing", file=sys.stderr)
        return None
    if r.returncode == 1:
        return ("pr comment", r.stdout.strip() or r.stderr.strip())
    if r.returncode not in (0, 1):
        print(f"gh-policy-gate: pr-monologue-check.py could not answer "
              f"(rc={r.returncode}); not enforcing", file=sys.stderr)
    return None


def evaluate(command):
    """Returns (subcommand, reason) to deny, or None to allow."""
    words = _tokenize(command)
    if not words:
        return None
    idx = _find_subcommand(words, ("issue", "create"))
    if idx is not None:
        return check_issue_create(words, idx)
    idx = _find_subcommand(words, ("pr", "comment"))
    if idx is not None:
        return check_pr_comment(words, idx)
    return None


def main(argv):
    if os.environ.get("SUTANDO_ALLOW_UNGATED_GH") == "1":
        return 0
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if payload.get("tool_name") != "Bash":
        return 0
    found = evaluate((payload.get("tool_input") or {}).get("command"))
    if not found:
        return 0
    sub, reason = found
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": (
            f"BLOCKED: `gh {sub}` refused by its gate script — {reason} "
            f"Override once with SUTANDO_ALLOW_UNGATED_GH=1. [gh-policy-gate]"),
    }}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
