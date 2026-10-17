#!/usr/bin/env bash
# SessionStart(compact) hook — re-inject PERSONAL_CLAUDE.md after context
# compaction.
#
# CLAUDE.md and the memory index are part of the system prompt, so they
# survive a /compact or a long-session summary. PERSONAL_CLAUDE.md is NOT:
# it only enters context via an explicit Read at session start, and that
# Read lives in the conversation — which is exactly what compaction
# summarizes away. On long sessions the agent silently loses its per-user
# rules and re-makes mistakes the file explicitly documents.
#
# This hook closes the gap: registered under the SessionStart "compact"
# matcher (see scripts/install-personal-claude-hook.sh), it emits the
# resolved PERSONAL_CLAUDE.md as additionalContext so the rules re-enter
# the fresh post-compaction window. Startup/resume are NOT matched — the
# session-start Read (CLAUDE.md "Personal overrides") already covers them.
#
# Resolution delegates to src/util_paths.py:personal_path() — the same
# per-host-first order every other reader uses (hosts/<host>/ → legacy
# memory-dir → workspace root). No inline `hostname | sed` here (#1745).
#
# Token control (opt-in): if the file contains the marker line
#   <!-- COMPACT-CORE-END -->
# only the content ABOVE the marker is injected, plus a one-line pointer to
# the full file. This lets a large PERSONAL_CLAUDE.md (the reporting install
# is ~12k tokens) keep a small always-on core without splitting into two
# files. No marker → the whole file is injected.
#
# Best-effort: any failure exits 0 so the hook never blocks a session start.

set -euo pipefail

# SCOPE GATE (mirrors schedule-crons-session-hint.sh): only the long-lived
# sutando-core session gets the injection. The core launcher
# (src/agent/claude/cli/start-cli.sh) sets SUTANDO_CORE_SESSION=1; ad-hoc
# sessions in the same checkout (PR-review agents, codex delegates, a plain
# `claude` in the repo) do not — injecting the owner's personal rules into a
# sandboxed reviewer's context would be both a token cost and behavioral
# contamination for a session that is deliberately NOT operating as the
# owner's agent. When the marker is absent we stay silent (exit 0).
if [ "${SUTANDO_CORE_SESSION:-}" != "1" ]; then
  exit 0
fi

REPO="$(cd "$(dirname "$0")/.." && pwd)"

python3 - "$REPO" <<'PYEOF' || exit 0
import hashlib
import json
import os
import sys

repo = sys.argv[1]
# Insert BOTH repo and repo/src: util_paths internally does a flat
# `from workspace_default import resolve_workspace`, which needs repo/src on
# sys.path. Without it, _workspace_root() falls back to cwd-dependent
# `git rev-parse` — and Claude Code runs hooks from the session cwd, which is
# not guaranteed to be a git checkout (caught in the PR's live test: from a
# non-repo cwd the hook silently resolved to ~/sutando-workspace and no-op'd).
sys.path.insert(0, repo + "/src")
sys.path.insert(0, repo)

try:
    from src.util_paths import personal_path
except ImportError:
    sys.exit(0)

MARKER = "<!-- COMPACT-CORE-END -->"

# Opt-in (0 = whole file): the platform's preview cap is undocumented, so a
# default bound would truncate files it already delivers intact.
try:
    CORE_BYTES = int(os.environ.get("SUTANDO_COMPACT_CORE_BYTES", "0"))
except ValueError:
    CORE_BYTES = 0


def _bounded_head(text, limit):
    """Head of text within `limit` bytes, trimmed to the last full line."""
    raw = text.encode("utf-8", "replace")
    if limit <= 0 or len(raw) <= limit:
        return text, False
    head = raw[:limit].decode("utf-8", "ignore")
    nl = head.rfind("\n")
    if nl > 0:
        head = head[:nl]
    return head.rstrip(), True


p = personal_path("PERSONAL_CLAUDE.md")
if not p.exists():
    sys.exit(0)  # no personal overrides on this install — stay silent

try:
    content = p.read_text()
except OSError:
    sys.exit(0)

if MARKER in content:
    core = content.split(MARKER, 1)[0].rstrip()
    body = (
        f"{core}\n\n"
        f"(Injected: always-on core of {p}. The reference tail below the "
        f"{MARKER} marker was omitted to save tokens — Read the file when "
        f"you need it.)"
    )
else:
    core, trimmed = _bounded_head(content, CORE_BYTES)
    if trimmed:
        shown = core.count("\n") + 1
        total = content.count("\n") + 1
        body = (
            f"{core}\n\n"
            f"(Injected: first {shown} of {total} lines of {p}, bounded to stay "
            f"within the post-compaction preview so these top rules survive intact. "
            f"Read the full file for the rest — set a `{MARKER}` marker to choose "
            f"the boundary yourself.)"
        )
    else:
        body = content

# A top preview is otherwise indistinguishable from a full load, so the header
# names an EOF sentinel only the very bottom carries: absent => truncated.
full = content.encode("utf-8", "replace")
sha = hashlib.sha256(full).hexdigest()
eof = f"<<<PERSONAL_CLAUDE_EOF sha256={sha[:16]}>>>"

context = (
    "PERSONAL_CLAUDE.md (re-injected after context compaction — these "
    "per-user rules override/extend CLAUDE.md and were lost from the "
    f"compacted context; source: {p}).\n"
    f"INTEGRITY: file is {len(full)} bytes, sha256 {sha}. This injection MUST "
    f"end with the exact line `{eof}`. If you do NOT see that line at the end "
    "of this block, your context received a TRUNCATED preview and the rules "
    f"below are INCOMPLETE — you MUST `Read {p}` in FULL before any substantive "
    "decision. A preview is indistinguishable from a full load EXCEPT by the "
    "presence of that end line. Seeing it proves only that this BLOCK arrived "
    "intact, not that it carries the whole file: if the block states it was "
    f"bounded to the first N lines, `Read {p}` in FULL for the "
    f"rest.\n\n{body}\n\n{eof}"
)

print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": context,
    }
}))
PYEOF
