#!/usr/bin/env python3
"""PreToolUse result-file-marker-guard: deny a result body that attaches a file
the bridge will refuse to send (hooks/result-file-marker-guard.py).

Drives the real hook via stdin, the way Claude Code invokes it. Self-contained:
a temp workspace is pointed at via CLAUDE_CONFIG_DIR, and every attached file is
a real file created here, so a DENY can only come from the allowlist decision
and never from "the path doesn't exist" — the way this test would otherwise pass
for the wrong reason.

Reproduces the 2026-08-04 incident directly: a real, existing .mp4 under
`skill-repos/` attached from a result body. The bridge posted
`(file not allowed: …)` into the owner's channel and the task archived as
delivered.

Run: python3 tests/result-file-marker-guard.test.py
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HOOK = str(Path(__file__).resolve().parent.parent / "hooks" / "result-file-marker-guard.py")

WS = Path(tempfile.mkdtemp())
(WS / ".claude-sutando").mkdir(parents=True, exist_ok=True)
RESULTS = WS / "results"
RESULTS.mkdir(parents=True, exist_ok=True)
OUTSIDE = WS / "skill-repos" / "video-production"
OUTSIDE.mkdir(parents=True, exist_ok=True)

# Both files EXIST. Only their location differs — that is the whole policy.
SENDABLE = RESULTS / "talk-v10.mp4"
SENDABLE.write_bytes(b"\x00" * 32)
UNSENDABLE = OUTSIDE / "talk-v9-small.mp4"
UNSENDABLE.write_bytes(b"\x00" * 32)

# Redirect BOTH the hook's scope check and send_allowlist's roots at the temp
# workspace via the documented test-only hatch (sutando_config.py:341). They must
# come from the same resolve_workspace() call the delivery path uses — pointing
# only one of them at the fixture would make the positive control below
# meaningless, since every path would then be "outside the allowlist".
ENV = {**os.environ, "CLAUDE_CONFIG_DIR": str(WS / ".claude-sutando"),
       "SUTANDO_TEST_MODE": "1", "SUTANDO_WORKSPACE": str(WS)}
ENV.pop("SUTANDO_SKIP_FILE_MARKER_GUARD", None)


REPO = str(Path(__file__).resolve().parent.parent)


def run(payload, env=None, repo=REPO, argv=True):
    cmd = [sys.executable, HOOK] + (["--repo", repo] if argv and repo else [])
    p = subprocess.run(cmd, input=json.dumps(payload),
                       capture_output=True, text=True, env=env or ENV)
    assert p.returncode == 0, f"hook must always exit 0, got {p.returncode}: {p.stderr}"
    if not p.stdout.strip():
        return None
    return json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"]


def write_call(path, content, tool="Write"):
    key = {"Write": "content", "Edit": "new_string"}[tool]
    ti = {"file_path": str(path), key: content}
    if tool == "Edit":
        ti["old_string"] = "x"
    return {"tool_name": tool, "tool_input": ti}


RESULT = RESULTS / "task-123.txt"
BAD_BODY = f"Here it is.\n\n[file: {UNSENDABLE}]\n"
GOOD_BODY = f"Here it is.\n\n[file: {SENDABLE}]\n"

# 1. THE INCIDENT — an existing file outside the allowlist must be DENIED.
assert run(write_call(RESULT, BAD_BODY)) == "deny", \
    "an existing-but-unsendable attachment in a result body must be denied"

# 2. Positive control on the SAME body: the identical marker pointing into
#    results/ must pass. Without this, #1 could be denying for any reason.
assert run(write_call(RESULT, GOOD_BODY)) is None, \
    "a marker inside the allowlist must pass untouched"

# 3. Every marker spelling is gated (the bridge honours all three).
for kw in ("file", "send", "attach"):
    assert run(write_call(RESULT, f"x\n[{kw}: {UNSENDABLE}]\n")) == "deny", \
        f"[{kw}:] marker must be gated"

# 4. A missing path is denied too — the bridge cannot send it either.
assert run(write_call(RESULT, f"x\n[file: {OUTSIDE / 'ghost.mp4'}]\n")) == "deny", \
    "a marker naming a non-existent file must be denied"

# 5. SCOPE: the same bad body written OUTSIDE results/ is not this hook's
#    business — notes, drafts and prose quoting a path must pass.
assert run(write_call(WS / "notes" / "draft.md", BAD_BODY)) is None, \
    "only result bodies are gated"

# 6. A result with no marker is never inspected.
assert run(write_call(RESULT, "Just a text answer, no attachment.")) is None, \
    "a result without a marker must pass"

# 7. Edit-shaped calls are gated too — a result body can be produced by Edit.
assert run(write_call(RESULT, BAD_BODY, tool="Edit")) == "deny", \
    "Edit into a result body must be gated the same as Write"

# 8. Non-edit tools are untouched.
assert run({"tool_name": "Bash", "tool_input": {"command": f"echo [file: {UNSENDABLE}]"}}) is None, \
    "non-edit tools must pass"

# 9. Escape hatch works.
assert run(write_call(RESULT, BAD_BODY),
           env={**ENV, "SUTANDO_SKIP_FILE_MARKER_GUARD": "1"}) is None, \
    "SUTANDO_SKIP_FILE_MARKER_GUARD=1 must disable the guard"

# 10. Fail-OPEN: malformed stdin must allow, never wedge the core.
p = subprocess.run([sys.executable, HOOK], input="not json",
                   capture_output=True, text=True, env=ENV)
assert p.returncode == 0 and not p.stdout.strip(), \
    "malformed input must fail open (exit 0, no deny)"

# 11. The denial must be ACTIONABLE — naming the file and the fix. A deny the
#     author can't act on just moves the babysitting one step earlier.
out = subprocess.run([sys.executable, HOOK, "--repo", REPO],
                     input=json.dumps(write_call(RESULT, BAD_BODY)),
                     capture_output=True, text=True, env=ENV).stdout
reason = json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]
assert UNSENDABLE.name in reason, "the denial must name the offending file"
assert "results/" in reason, "the denial must point at the fix (stage into an allowed root)"

# ---------------------------------------------------------------- regressions
# 12. THE DEPLOYED-COPY BLOCKER (bassilkhilo-ag2 + qingyun-wu, PR #2596).
#     v1 discovered the repo by walking up from __file__, so once copied to
#     ~/.claude/hooks/ it resolved to the deploy dir, found no src/, and exited
#     0 on EVERY write — inert in its own documented deployment, silently.
#     Run the hook from a copy OUTSIDE any repo layout, with no --repo and no
#     $SUTANDO_REPO_ROOT, and assert it says so on stderr instead of passing mute.
import shutil
fake_home = Path(tempfile.mkdtemp()) / ".claude" / "hooks"
fake_home.mkdir(parents=True)
copied = fake_home / "result-file-marker-guard.py"
shutil.copy(HOOK, copied)
env_nore = {k: v for k, v in ENV.items() if k != "SUTANDO_REPO_ROOT"}
p = subprocess.run([sys.executable, str(copied)], input=json.dumps(write_call(RESULT, BAD_BODY)),
                   capture_output=True, text=True, env=env_nore)
assert p.returncode == 0 and not p.stdout.strip(), "unresolvable repo must fail open"
assert "INERT" in p.stderr, \
    f"an unresolvable repo root must be LOUD, not silent — stderr was {p.stderr!r}"

# 13. The same copied hook, given --repo, must WORK. Without this, #12 could be
#     passing because the copy is broken in some other way.
p = subprocess.run([sys.executable, str(copied), "--repo", REPO],
                   input=json.dumps(write_call(RESULT, BAD_BODY)),
                   capture_output=True, text=True, env=env_nore)
assert json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny", \
    "a copied hook with --repo must enforce"

# 14. $SUTANDO_REPO_ROOT is the other configured route.
p = subprocess.run([sys.executable, str(copied)], input=json.dumps(write_call(RESULT, BAD_BODY)),
                   capture_output=True, text=True, env={**env_nore, "SUTANDO_REPO_ROOT": REPO})
assert json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny", \
    "$SUTANDO_REPO_ROOT must configure the repo root"

# 15. SLACK ADAPTER ROOT (qingyun-wu, PR #2596). Slack extends the canonical
#     allowlist with <workspace>/slack-inbox/ so an uploaded file can be echoed
#     back. Judging a Slack result against the Discord/Telegram policy denies a
#     currently-supported reply path. The destination comes from the task's
#     `source:` field, so this must be allowed for slack and DENIED for discord —
#     the pair is the point: a global slack-inbox root would pass both.
inbox = WS / "slack-inbox"; inbox.mkdir(parents=True, exist_ok=True)
upload = inbox / "echo-me.png"; upload.write_bytes(b"\x00" * 16)
tasks = WS / "tasks"; tasks.mkdir(parents=True, exist_ok=True)
(tasks / "task-slack1.txt").write_text("id: task-slack1\nsource: slack\n")
(tasks / "task-disc1.txt").write_text("id: task-disc1\nsource: discord\n")
slack_body = f"echo\n\n[file: {upload}]\n"
assert run(write_call(RESULTS / "task-slack1.txt", slack_body)) is None, \
    "a slack-inbox upload must be sendable for a SLACK-sourced result"
assert run(write_call(RESULTS / "task-disc1.txt", slack_body)) == "deny", \
    "the same slack-inbox path must NOT be sendable for a DISCORD-sourced result"

# 16. Unknown destination falls back to the UNION, never the strictest — a false
#     deny for a destination nobody can name is unsatisfiable for the author.
assert run(write_call(RESULTS / "task-nosuchtask.txt", slack_body)) is None, \
    "an unresolvable destination must use the union of adapter roots"

print("PASS: result-file-marker-guard — denies an EXISTING file outside the allowlist "
      "(the 2026-08-04 incident) with a positive control on the same body, gates all three "
      "marker spellings + Edit, scopes to results/ only, honours the escape hatch, "
      "fails open on bad input, emits an actionable reason, is LOUD (not silent) when the "
      "repo root is unconfigured in a deployed copy, enforces once given --repo or "
      "$SUTANDO_REPO_ROOT, and applies the SLACK adapter's extra root only to "
      "slack-sourced results")
