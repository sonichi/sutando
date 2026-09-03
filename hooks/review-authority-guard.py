#!/usr/bin/env python3
"""review-authority-guard — PreToolUse hook that denies FORMAL GitHub reviews
(APPROVE / REQUEST_CHANGES, and optionally COMMENT) while the owner's standing
answer on review authority is unresolved.

Why this exists as a hook and not a note: on 2026-09-01 the agent declined to
file formal reviews three times, then filed an APPROVE on the fourth ask after
verifying the change carefully. Careful verification is not authorization. The
approval turned out to be the only *gating* approval on that PR, so half a merge
gate was cleared by an agent whose owner had not answered whether it may do that.
A boundary held by remembering it on every request fails eventually; this runs
unconditionally.

Authority state lives in ``<workspace>/state/authority.json``:

    {"github_formal_review": "hold" | "findings-only" | "allow"}

  hold           deny APPROVE, REQUEST_CHANGES and COMMENT (review in-room instead)
  findings-only  deny APPROVE and REQUEST_CHANGES; allow --comment
  allow          allow all formal reviews

MISSING or UNREADABLE state means ``hold``. That is policy, not a bug: the
restrictive reading is what applies until the owner rules, and this surface is
narrow enough (formal reviews only) that defaulting closed cannot wedge the core.
Genuine hook exceptions still fail OPEN, per the repo's hook contract.

NOT blocked, deliberately: ``--comment`` under findings-only, review DISMISSAL
(reducing one's own standing review is never the risky direction), plain PR
comments, and every non-review ``gh`` call.

Escape hatch: ``SUTANDO_ALLOW_FORMAL_GH_REVIEWS=1``.

Registration: per-node deploy into $CLAUDE_CONFIG_DIR — see hooks/README.md.
"""
import json
import os
import re
import shlex
import sys
from typing import Optional

STATE_REL = os.path.join("state", "authority.json")
KEY = "github_formal_review"
BLOCKING = {"approve": "APPROVE", "request-changes": "REQUEST_CHANGES"}

# `gh api .../reviews` carrying an event, e.g. -f event=APPROVE or JSON input.
_API_EVENT = re.compile(r"\b(APPROVE|REQUEST_CHANGES)\b")
_API_REVIEWS = re.compile(r"/pulls/\d+/reviews\b")
# Dismissal is a REDUCTION of standing — never gated.
_DISMISSAL = re.compile(r"/reviews/\d+/dismissals\b")


def _workspace() -> str:
    """Locate the workspace holding state/authority.json.

    Deployment COPIES this file out of the repo (into $CLAUDE_CONFIG_DIR/hooks),
    so a purely repo-relative guess points at a directory that does not exist —
    and a missing state file reads as 'hold', which looks correct while being
    permanently deaf to the file. Search for the state file itself instead.
    """
    env = os.environ.get("SUTANDO_WORKSPACE_FOR_HOOK", "").strip()
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    cand = [os.path.join(os.path.dirname(here), "workspace")]  # in-repo layout
    d = here
    for _ in range(6):                                        # deployed layout
        d = os.path.dirname(d)
        if not d or d == os.path.dirname(d):
            break
        cand.append(d)
        cand.append(os.path.join(d, "workspace"))
    for c in cand:
        if os.path.isfile(os.path.join(c, STATE_REL)):
            return c
    return cand[0]


def read_state(workspace: str) -> str:
    """Return the authority mode. Missing/unreadable/unknown -> 'hold'."""
    try:
        with open(os.path.join(workspace, STATE_REL)) as fh:
            val = json.load(fh).get(KEY)
    except Exception:
        return "hold"
    if isinstance(val, str) and val.strip().lower() in ("hold", "findings-only", "allow"):
        return val.strip().lower()
    return "hold"


def _segments(command: str):
    """Split a compound shell command into candidate invocations."""
    return [s for s in re.split(r"&&|\|\||[;|\n]", command) if s.strip()]


def classify(command: str) -> Optional[str]:
    """Return 'APPROVE' / 'REQUEST_CHANGES' / 'COMMENT', or None if not a formal review."""
    if not isinstance(command, str) or "gh" not in command:
        return None
    for seg in _segments(command):
        if _DISMISSAL.search(seg):
            continue
        try:
            words = shlex.split(seg)
        except ValueError:
            words = seg.split()
        if not words:
            continue
        low = [w.lower() for w in words]
        if "gh" in low and "pr" in low and "review" in low:
            starts = [i for i, w in enumerate(low) if w == "gh"]
            if any(low[i + 1:i + 3] == ["pr", "review"] for i in starts):
                for w in low:
                    flag = w.lstrip("-")
                    if w.startswith("--") and flag in BLOCKING:
                        return BLOCKING[flag]
                    if w in ("-a",):
                        return "APPROVE"
                    if w in ("-r",):
                        return "REQUEST_CHANGES"
                if any(w == "--comment" or w == "-c" for w in low):
                    return "COMMENT"
                # `gh pr review` with no event flag opens an interactive prompt.
                return "COMMENT"
        if "gh" in low and "api" in low and _API_REVIEWS.search(seg):
            m = _API_EVENT.search(seg)
            if m:
                return m.group(1)
    return None


def reason(event: str, mode: str, workspace: str) -> str:
    return (
        f"BLOCKED: filing a formal GitHub review ({event}) is gated on this install. "
        f"Authority state is '{mode}' in {os.path.join(workspace, STATE_REL)} "
        f"({KEY}); a missing or unreadable file also means 'hold'. "
        "An APPROVE/REQUEST_CHANGES is not just publishing — it moves a merge gate, "
        "and merges are the owner's. This guard exists because on 2026-09-01 a careful, "
        "correct review was filed as a formal APPROVE without that ruling, and it was the "
        "only gating approval on the PR. Verifying a change well is not authorization to "
        "vote on it. Do this instead: post the findings in-room and let a human or an "
        "authorised agent file the review, or use `--comment` if the state is "
        "'findings-only'. When the owner rules, set the state file and this lifts. "
        "Override for one session with SUTANDO_ALLOW_FORMAL_GH_REVIEWS=1. "
        "[review-authority-guard]"
    )


def main() -> None:
    if os.environ.get("SUTANDO_ALLOW_FORMAL_GH_REVIEWS", "").strip() == "1":
        sys.exit(0)
    data = json.loads(sys.stdin.read())
    if str(data.get("tool_name") or "") != "Bash":
        sys.exit(0)
    command = (data.get("tool_input") or {}).get("command")
    event = classify(command or "")
    if event is None:
        sys.exit(0)
    workspace = _workspace()
    mode = read_state(workspace)
    if mode == "allow":
        sys.exit(0)
    if mode == "findings-only" and event == "COMMENT":
        sys.exit(0)
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason(event, mode, workspace),
    }}))
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # fail-open: a crashing hook must never wedge the core
        print(f"[review-authority-guard] non-fatal error, allowing: {e}", file=sys.stderr)
        sys.exit(0)
