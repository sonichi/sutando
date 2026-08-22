#!/usr/bin/env python3
"""result-file-marker-guard — PreToolUse hook that DENIES writing a result body
whose ``[file:|send:|attach:]`` marker points outside the send allowlist **for
the adapter that will actually deliver it**.

Why (owner incident 2026-08-04, #susan): the agent finished a 6-minute video,
wrote ``[file: …/skill-repos/video-production/…/talk.mp4]`` into a result, and
reported the task delivered. ``skill-repos/`` is not on the allowlist
(``src/send_allowlist.py``), so the bridge posted a literal
``(file not allowed: /Users/…/talk.mp4)`` into the owner's channel and delivered
nothing. The owner found it, not the agent:

    "Can't see this file. And I don't want to babysit. Can you improve"

**The failure reports success in every cheap way available to the author.** The
file exists, the path is absolute and correct, the marker regex matches, the
Write succeeds, the result file lands, and the bridge consumes it and archives
the task — so every signal the agent normally checks says delivered. The
allowlist is enforced at the *far* end, after the last point the agent looks.

So the check belongs at the moment the marker is AUTHORED, and it must be a
mechanism rather than a discipline
(``feedback_guarantee_is_structural_not_disciplinary``).

Scope — deliberately narrow:
  * Only Write/Edit/MultiEdit whose target resolves under ``<workspace>/results/``.
  * Only bodies containing an attachment marker.
  * Parsing and the verdict come from the SAME modules the delivery path uses
    (``result_markers`` + ``send_allowlist``), so the guard cannot drift from
    the policy it enforces. A re-implemented copy would eventually accept what
    the bridge rejects, and that false PASS is worse than no guard.

ADAPTER CONTEXT (qingyun-wu, PR #2596 review). The allowlist is not global:
Slack deliberately extends it with its adapter-local ``<workspace>/slack-inbox/``
so an uploaded file can be echoed back (``src/slack-bridge.py:153-158``).
Judging every result against the canonical Discord/Telegram policy would deny a
currently-supported Slack reply. So the guard resolves the DESTINATION first —
``results/task-<id>.txt`` names the task, and the task file's ``source:`` field
names the adapter — and applies that adapter's policy. When the destination
cannot be established (a proactive body, or a task archived out from under us)
it falls back to the CANONICAL roots only, never the union — see the comment at
that branch for why the union was unsound and why the routing state cannot
recover the answer.

REPO ROOT (bassilkhilo-ag2 + qingyun-wu, PR #2596 review). This file needs
``src/`` on ``sys.path``. It must NOT discover that by walking up from
``__file__``: the deploy step copies the hook out of the checkout, so the walk
resolves to the deploy dir, and the repo bans that pattern outright
(``scripts/lint-workspace-resolution.sh`` — it breaks under symlinked/bundled
layouts). The location is therefore CONFIGURED, not guessed: ``--repo <path>``
(written by the registration snippet in hooks/README.md) or
``$SUTANDO_REPO_ROOT``. If neither resolves, the hook says so **loudly on
stderr** and allows — the v1 of this hook exited silently, which made an
unresolvable root indistinguishable from a clean pass, i.e. exactly the
"reports success in every cheap way" defect it exists to prevent.

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
import re
import sys

WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

# Adapter -> extra roots beyond the canonical allowlist, relative to the
# workspace. Mirrors what each bridge passes to is_path_sendable(extra_roots=…).
# Keep in step with the bridges; a bridge that adds a root and not an entry here
# gets a FALSE DENY, which the tests below are meant to make loud.
ADAPTER_EXTRA_ROOTS = {
    "slack": ("slack-inbox",),          # src/slack-bridge.py:153-158
    "discord": (),
    "telegram": (),
}


def _warn(msg):
    print(f"[result-file-marker-guard] {msg}", file=sys.stderr)


def _repo_root(argv):
    """CONFIGURED, never guessed — see the module docstring."""
    for i, a in enumerate(argv):
        if a == "--repo" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--repo="):
            return a.split("=", 1)[1]
    return os.environ.get("SUTANDO_REPO_ROOT") or None


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


def _adapter_for(result_path, workspace):
    """The bridge that will deliver this result, from the task it answers.

    `results/task-<id>.txt` -> `tasks/task-<id>.txt` (or its archive) -> `source:`.
    Returns None when it can't be established; the caller then uses the
    CANONICAL roots only (see that branch), never a union of provider-local ones.
    """
    m = re.match(r"^(?:[^.]+\.)?task-(.+)\.txt$", os.path.basename(result_path))
    if not m:
        return None
    tid = m.group(1)
    for rel in (f"tasks/task-{tid}.txt", f"tasks/archive/task-{tid}.txt"):
        p = os.path.join(str(workspace), rel)
        try:
            with open(p, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if line.startswith("source:"):
                        return line.split(":", 1)[1].strip().lower()
        except OSError:
            continue
    return None


def _deny(reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}))
    sys.exit(0)


def main(argv):
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

    repo = _repo_root(argv)
    if not repo or not os.path.isfile(os.path.join(repo, "src", "send_allowlist.py")):
        _warn("INERT: repo root not configured (pass --repo <path> in the hook "
              "registration, or set $SUTANDO_REPO_ROOT). Attachment markers are "
              "NOT being checked — see hooks/README.md.")
        sys.exit(0)
    sys.path.insert(0, os.path.join(repo, "src"))
    from workspace_default import resolve_workspace
    from policy.egress.attachment import is_path_sendable, SEND_ALLOWED_ROOTS, SEND_ALLOWED_PREFIXES
    from result_markers import parse_markers

    workspace = resolve_workspace()
    results_dir = os.path.realpath(os.path.join(str(workspace), "results"))
    real_target = os.path.realpath(os.path.expanduser(target))
    if not real_target.startswith(results_dir + os.sep):
        sys.exit(0)  # not a deliverable result body

    adapter = _adapter_for(real_target, workspace)
    if adapter in ADAPTER_EXTRA_ROOTS:
        extra = tuple(str(workspace / r) for r in ADAPTER_EXTRA_ROOTS[adapter])
        scope = f"the {adapter} adapter"
    else:
        # Destination NOT established -> canonical roots ONLY, never the union.
        #
        # v2 of this hook used the union here, reasoning that a false deny for
        # an unnameable destination is unsatisfiable. john-the-dev reproduced
        # why that is wrong: `results/proactive-*.txt` has no task to name a
        # source, so the union authorized Slack's `slack-inbox/` for a file
        # that Discord or Telegram would then refuse — recreating the exact
        # silent-failure this guard exists to prevent, with a clean pass in
        # front of it.
        #
        # Nor is the destination recoverable from `state/last-owner-activity.json`
        # as the review suggested, and I checked the delivery code rather than
        # assuming: discord and telegram gate their claim on
        # `proactive_routing.should_claim_proactive`, but slack-bridge.py:1443
        # claims proactive files by RACE-RENAME, skipping only bodies carrying a
        # Discord `[channel:]` marker. Three claimants, no deterministic winner —
        # so for a proactive body "which adapter delivers this" has no answer at
        # authoring time.
        #
        # The union is therefore unsound and the routing state cannot fix it.
        # Canonical-only is: every adapter accepts these roots, so an allow here
        # is an allow everywhere. It costs one deny — a provider-local path in a
        # proactive body — and that deny is trivially satisfiable by staging into
        # `results/`, which the reason text says.
        extra = ()
        scope = ("an unresolved destination (proactive/non-task result; canonical "
                 "roots only, since any adapter may claim it)")

    bad = []
    for act in parse_markers(body).actions:
        if act.kind != "attach":
            continue
        p = os.path.expanduser(act.value.strip())
        if not is_path_sendable(p, extra_roots=extra):
            bad.append((p, "no such file" if not os.path.isfile(p) else "outside the allowlist"))
    if not bad:
        sys.exit(0)

    roots = "\n".join(f"    {r}" for r in (*SEND_ALLOWED_ROOTS, *extra, *SEND_ALLOWED_PREFIXES))
    listed = "\n".join(f"    {p}  ({why})" for p, why in bad)
    _deny(
        "RESULT FILE-MARKER GUARD: this result body attaches a file the bridge will "
        "REFUSE to send, so the owner would receive a literal "
        "'(file not allowed: …)' line and no attachment — while the task still "
        "archives as delivered.\n\n"
        f"Unsendable for {scope}:\n{listed}\n\n"
        f"Deliverable roots/prefixes (src/send_allowlist.py):\n{roots}\n\n"
        "Fix: stage the file into an allowed root first (copy or re-encode it "
        "directly into <workspace>/results/), then point the marker at that copy. "
        "[result-file-marker-guard]"
    )


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except SystemExit:
        raise
    except Exception as e:  # fail-open: never wedge the core on a guard bug
        _warn(f"non-fatal error, allowing: {e}")
        sys.exit(0)
