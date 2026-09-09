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

A MISSING file means ``findings-only``: the two events that move a merge gate
stay denied until the owner rules, while a COMMENTED review — which moves nothing
and is the only durable place a finding lives — stays possible. A file that is
PRESENT but unreadable or carries an unknown mode means ``hold``: someone wrote
a ruling and it cannot be read, so the restrictive reading applies. Genuine hook
exceptions still fail OPEN, per the repo's hook contract.

NOT blocked, deliberately: ``--comment`` under findings-only, review DISMISSAL,
variable indirection (``GH=gh; $GH ...`` needs real shell expansion — a non-goal here)
(reducing one's own standing review is never the risky direction), plain PR
comments, and every non-review ``gh`` call.

Escape hatch: ``SUTANDO_ALLOW_FORMAL_GH_REVIEWS=1``.

Registration: per-node deploy into $CLAUDE_CONFIG_DIR — see hooks/README.md.
Workspace: SUTANDO_HOOK_WORKSPACE > workspace_default.resolve_workspace (in-repo)
> an upward search for the state file (deployed copy).
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

# `gh api .../reviews`: the event ASSIGNMENT (-f event=APPROVE, "event": "APPROVE"),
# never a bare word — a COMMENT whose body discusses approval is not an approval.
_API_EVENT = re.compile(r"""\bevent\b["']?\s*[:=]\s*["']?(APPROVE|REQUEST_CHANGES|COMMENT)\b""")
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
    env = os.environ.get("SUTANDO_HOOK_WORKSPACE", "").strip()
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    src = os.path.join(os.path.dirname(here), "src")
    if os.path.isfile(os.path.join(src, "workspace_default.py")):  # in-repo layout
        sys.path.insert(0, src)
        from workspace_default import resolve_workspace
        return str(resolve_workspace())
    cand = [os.path.join(os.path.dirname(here), "workspace")]
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
    """Return the authority mode. Missing -> 'findings-only'; present but
    unreadable or unknown -> 'hold' (a ruling was written and cannot be read)."""
    path = os.path.join(workspace, STATE_REL)
    if not os.path.exists(path):
        return "findings-only"
    try:
        with open(path) as fh:
            val = json.load(fh).get(KEY)
    except Exception:
        return "hold"
    if isinstance(val, str) and val.strip().lower() in ("hold", "findings-only", "allow"):
        return val.strip().lower()
    return "hold"


def _segments(command: str):
    """Split a compound shell command into candidate invocations."""
    return [s for s in re.split(r"&&|\|\||[;|\n]", command) if s.strip()]


_SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
_INTERPRETERS = {"python", "python3", "node", "ruby", "perl"}
# A list-literal argv (`['gh','pr','review']`) is one program string to shlex; dropping
# quotes, commas and brackets restores adjacency so the token check sees it.
_LITERAL_NOISE = re.compile(r"""[\[\]\(\),'"]""")


def _wrapped_command(words):
    """The string a wrapper would run (`bash -c`, `eval`, `python3 -c`, `node -e`):
    one shlex token, so it is re-classified; interpreter strings de-literalised first.
    Not covered: variable indirection (`$GH pr review`) and program text via a pipe."""
    if not words:
        return None
    head = words[0].rsplit("/", 1)[-1].lower()
    if head == "eval":
        return " ".join(words[1:]) or None
    if head in _SHELLS:
        for i, w in enumerate(words[1:-1], 1):
            if w == "-c" or (w.startswith("-") and not w.startswith("--") and "c" in w):
                return words[i + 1]
    if head in _INTERPRETERS or head.startswith("python"):
        for i, w in enumerate(words[1:-1], 1):
            if w in ("-c", "-e"):
                return _LITERAL_NOISE.sub(" ", words[i + 1])
    return None


# A heredoc body: program text on stdin (`python3 - <<'PY'`) or JSON for `--input -`.
_HEREDOC = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n(.*?)\n\s*\1\s*(?:\n|$)", re.S)


def _heredoc_owner(prefix):
    """Head of the command that owns a heredoc: 'gh', an interpreter name, or None."""
    line = prefix.rsplit("\n", 1)[-1]
    seg = re.split(r"&&|\|\||[;|]", line)[-1]
    try:
        words = shlex.split(seg)
    except ValueError:
        words = seg.split()
    if not words:
        return None
    head = words[0].rsplit("/", 1)[-1].lower()
    if head == "gh":
        return "gh"
    if head in _INTERPRETERS or head.startswith("python"):
        return head
    return None


def _is_gh(word: str) -> bool:
    """A path-qualified `gh` is still gh. `_heredoc_owner` already basenames;
    the main scan compared the whole word, so an absolute path slipped past."""
    return word.rsplit("/", 1)[-1] == "gh"


def _has_gh(words) -> bool:
    return any(_is_gh(w) for w in words)


def classify(command: str) -> Optional[str]:
    """Return 'APPROVE' / 'REQUEST_CHANGES' / 'COMMENT', or None if not a formal review."""
    # Case-fold here too: the token scan lowercases, but this prefilter runs
    # first, and a case-insensitive filesystem runs `GH` as the same binary.
    if not isinstance(command, str) or "gh" not in command.lower():
        return None
    for m in _HEREDOC.finditer(command):
        # Only program text or API input is scanned; a `cat`/`tee` heredoc that
        # merely mentions the command is documentation, not a review.
        head = _heredoc_owner(command[:m.start()])
        if head is None:
            continue
        body = m.group(2)
        if head == "gh":
            e = _API_EVENT.search(body) if _API_REVIEWS.search(command) else None
            if e:
                return e.group(1)
            continue
        nested = classify(_LITERAL_NOISE.sub(" ", body))
        if nested is not None:
            return nested
    # A wrapper's quoted -c string may itself contain && or ; — classify it
    # whole before the separator split can cut through the quotes.
    try:
        whole = shlex.split(command)
    except ValueError:
        whole = []
    inner = _wrapped_command(whole)
    if inner is not None:
        nested = classify(inner)
        if nested is not None:
            return nested
    # Bodies are settled above; the segment pass must not re-read them as commands.
    command = _HEREDOC.sub("\n", command)
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
        inner = _wrapped_command(words)
        if inner is not None:
            nested = classify(inner)
            if nested is not None:
                return nested
        if _has_gh(low) and "pr" in low and "review" in low:
            starts = [i for i, w in enumerate(low) if _is_gh(w)]
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
        if _has_gh(low) and "api" in low and _API_REVIEWS.search(seg):
            m = _API_EVENT.search(seg)
            if m:
                return m.group(1)
            # A reviews POST whose event rides in --input/stdin is unreadable here;
            # fail open rather than guess from prose.
    return None


def reason(event: str, mode: str, workspace: str) -> str:
    return (
        f"BLOCKED: filing a formal GitHub review ({event}) is gated on this install. "
        f"Authority state is '{mode}' in {os.path.join(workspace, STATE_REL)} "
        f"({KEY}); a missing file means 'findings-only', an unreadable one 'hold'. "
        "An APPROVE/REQUEST_CHANGES is not just publishing — it moves a merge gate, "
        "and merges are the owner's. This guard exists because on 2026-09-01 a careful, "
        "correct review was filed as a formal APPROVE without that ruling, and it was the "
        "only gating approval on the PR. Verifying a change well is not authorization to "
        "vote on it. Do this instead: file the findings as a COMMENTED review "
        "(`gh pr review --comment`) — that moves no gate and keeps the record on the PR; "
        f"if the state is 'hold' or an unreadable file, {{\"{KEY}\": \"findings-only\"}} "
        "in that file is the setting that restores --comment while the votes stay gated. "
        "Otherwise post in-room and let a human or an authorised agent file the review. "
        "When the owner rules, set the state file and this lifts. "
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
