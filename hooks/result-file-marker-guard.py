#!/usr/bin/env python3
"""result-file-marker-guard — PreToolUse hook that DENIES writing a result body
whose ``[file:|send:|attach:]`` marker points outside the send allowlist.

Why (owner incident 2026-08-04, #susan): the agent finished a 6-minute video,
wrote ``[file: …/skill-repos/video-production/…/talk.mp4]`` into a result, and
reported the task delivered. ``skill-repos/`` is not on the allowlist
(``src/send_allowlist.py``), so the bridge posted a literal
``(file not allowed: /Users/…/talk.mp4)`` into the owner's channel and delivered
nothing. The owner found it, not the agent:

    "Can't see this file. And I don't want to babysit. Can you improve"

**The failure reports success in every cheap way available to the author.** The
file exists on disk, the path is absolute and correct, the marker regex matches,
the Write succeeds, the result file lands, and the bridge consumes it and
archives the task — so every signal the agent normally checks says delivered.
The allowlist is enforced at the *far* end, after the last point the agent
looks. Only opening the channel shows the failure, which is precisely the
"babysitting" the owner objected to.

So the check belongs at the moment the marker is AUTHORED, not at send time, and
it must be a mechanism rather than a discipline
(``feedback_guarantee_is_structural_not_disciplinary``): a remembered
"validate before writing" step is exactly what was missing.

Scope — deliberately narrow:
  * Only Write/Edit/MultiEdit whose target resolves under ``<workspace>/results/``.
    Notes, docs, scratch files and source edits pass through untouched.
  * Only bodies containing an attachment marker. A result with no marker is
    never inspected.
  * Uses the SAME two modules the delivery path uses — ``result_markers`` for
    parsing and ``send_allowlist.is_path_sendable`` for the verdict — so the
    guard cannot drift from the policy it enforces. That is the whole point:
    a re-implemented copy would eventually accept what the bridge rejects, and
    the resulting false PASS is worse than no guard at all.

Denial is the right call over a warning: a warning still produces a broken
message in the owner's channel. Denied, the agent stages the file into an
allowed root (``results/`` is the usual answer) and re-writes — which takes one
extra command and always works.

Escape hatch: ``SUTANDO_SKIP_FILE_MARKER_GUARD=1``.

Fail-OPEN on any internal error — a crashing hook must never wedge the core
(same contract as ``skip-ask-user-question.py`` / ``gmail-write-guard.py``).
Note the asymmetry with ``context-source-guard.py``, which fails CLOSED: that
one prevents blacklisted content entering context, where the cost of being
wrong is a leak. Here the cost of being wrong is a message the owner can see
and re-request, so wedging the core would be the larger harm.

Registration: manual per-node deploy — see hooks/README.md.
"""
import json
import os
import sys
from pathlib import Path

WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


def _repo_root():
    """This file lives at <repo>/hooks/, whether run from the repo or a copy
    deployed into ~/.claude/hooks/. Prefer the real repo when reachable."""
    here = Path(__file__).resolve().parent.parent
    if (here / "src" / "send_allowlist.py").is_file():
        return here
    for cand in (Path.home() / "stando-ui" / "sutando",):
        if (cand / "src" / "send_allowlist.py").is_file():
            return cand
    return None


def _body_from(tool_name, ti):
    """The text this call would put on disk, across the edit-tool shapes."""
    if tool_name == "Write":
        return ti.get("content") or ""
    if tool_name == "Edit":
        return ti.get("new_string") or ""
    if tool_name == "MultiEdit":
        return "\n".join(e.get("new_string") or "" for e in (ti.get("edits") or []))
    if tool_name == "NotebookEdit":
        return ti.get("new_source") or ""
    return ""


def _deny(reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}))
    sys.exit(0)


def main():
    if os.environ.get("SUTANDO_SKIP_FILE_MARKER_GUARD") == "1":
        sys.exit(0)

    payload = json.load(sys.stdin)
    tool_name = payload.get("tool_name") or ""
    if tool_name not in WRITE_TOOLS:
        sys.exit(0)

    ti = payload.get("tool_input") or {}
    target = ti.get("file_path") or ti.get("notebook_path") or ""
    if not target:
        sys.exit(0)

    body = _body_from(tool_name, ti)
    # Cheap reject before importing anything: no marker, nothing to check.
    if not any(k in body for k in ("[file:", "[send:", "[attach:")):
        sys.exit(0)

    repo = _repo_root()
    if repo is None:
        sys.exit(0)  # can't resolve policy -> fail open
    sys.path.insert(0, str(repo / "src"))
    from workspace_default import resolve_workspace
    from send_allowlist import is_path_sendable, SEND_ALLOWED_ROOTS, SEND_ALLOWED_PREFIXES
    from result_markers import parse_markers

    results_dir = os.path.realpath(resolve_workspace() / "results")
    real_target = os.path.realpath(os.path.expanduser(target))
    if not real_target.startswith(results_dir + os.sep):
        sys.exit(0)  # not a deliverable result body

    bad = []
    for act in parse_markers(body).actions:
        if act.kind != "attach":
            continue
        p = os.path.expanduser(act.value.strip())
        if not is_path_sendable(p):
            bad.append((p, "no such file" if not os.path.isfile(p) else "outside the allowlist"))
    if not bad:
        sys.exit(0)

    roots = "\n".join(f"    {r}" for r in (*SEND_ALLOWED_ROOTS, *SEND_ALLOWED_PREFIXES))
    listed = "\n".join(f"    {p}  ({why})" for p, why in bad)
    _deny(
        "RESULT FILE-MARKER GUARD: this result body attaches a file the bridge will "
        "REFUSE to send, so the owner would receive a literal "
        "'(file not allowed: …)' line and no attachment — while the task still "
        "archives as delivered.\n\n"
        f"Unsendable:\n{listed}\n\n"
        f"Deliverable roots/prefixes (src/send_allowlist.py):\n{roots}\n\n"
        "Fix: stage the file into an allowed root first (copy or re-encode it "
        "directly into <workspace>/results/), then point the marker at that copy. "
        "[result-file-marker-guard]"
    )


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # fail-open: never wedge the core on a guard bug
        print(f"[result-file-marker-guard] non-fatal error, allowing: {e}", file=sys.stderr)
        sys.exit(0)
