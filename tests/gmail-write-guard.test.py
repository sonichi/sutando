#!/usr/bin/env python3
"""PreToolUse gmail-write-guard: Gmail MCP connector WRITE tools must be denied
with an actionable reason; reads and non-Gmail tools pass (hooks/gmail-write-guard.py).

Behavioral: drives the real hook via stdin exactly the way Claude Code invokes
it, asserting on the emitted decision JSON + exit code.

Run:  python3 tests/gmail-write-guard.test.py
"""
import json
import os
import subprocess
import sys
from pathlib import Path

HOOK = str(Path(__file__).resolve().parent.parent / "hooks" / "gmail-write-guard.py")

failures = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL ") + name + ((" — " + detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


def run(payload, env_extra=None):
    stdin = json.dumps(payload) if not isinstance(payload, str) else payload
    env = dict(os.environ)
    env.pop("SUTANDO_ALLOW_GMAIL_CONNECTOR_WRITES", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run([sys.executable, HOOK], input=stdin,
                          capture_output=True, text=True, timeout=20, env=env)


def decision(r):
    try:
        return json.loads(r.stdout).get("hookSpecificOutput", {}).get("permissionDecision")
    except Exception:
        return None


# ── The 8 write tools from field report 05cb849a are all denied ───────────────
WRITE_TOOLS = [
    "mcp__claude_ai_Gmail__create_draft",
    "mcp__claude_ai_Gmail__label_thread",
    "mcp__claude_ai_Gmail__unlabel_thread",
    "mcp__claude_ai_Gmail__create_label",
    "mcp__claude_ai_Gmail__apply_sensitive_content_label",
    "mcp__claude_ai_Gmail__apply_sensitive_topics_label",
    "mcp__claude_ai_Gmail__archive_thread",
    "mcp__claude_ai_Gmail__trash_message",
]
for t in WRITE_TOOLS:
    r = run({"tool_name": t, "tool_input": {}})
    check(f"deny: {t.rsplit('__', 1)[-1]}",
          r.returncode == 0 and decision(r) == "deny", r.stdout[:120])

# Reason must be actionable: name the IMAP/SMTP path + the escape hatch.
r = run({"tool_name": "mcp__claude_ai_Gmail__create_draft", "tool_input": {}})
reason = json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
check("reason points at the IMAP/SMTP path", "imaplib/smtplib" in reason)
check("reason names the escape hatch", "SUTANDO_ALLOW_GMAIL_CONNECTOR_WRITES" in reason)

# ── Read tools pass through (they work fine and must keep working) ────────────
READ_TOOLS = [
    "mcp__claude_ai_Gmail__search_threads",
    "mcp__claude_ai_Gmail__get_thread",
    "mcp__claude_ai_Gmail__list_labels",      # token "labels" != write token "label"
    "mcp__claude_ai_Gmail__get_message",
]
for t in READ_TOOLS:
    r = run({"tool_name": t, "tool_input": {}})
    check(f"allow: {t.rsplit('__', 1)[-1]}",
          r.returncode == 0 and decision(r) is None and not r.stdout.strip(), r.stdout[:120])

# ── Naming variants: other server spellings still match ──────────────────────
for t in ["mcp__gmail__send_message", "mcp__composio__GMAIL_CREATE_DRAFT"]:
    r = run({"tool_name": t, "tool_input": {}})
    check(f"deny variant spelling: {t}", decision(r) == "deny")

# ── Non-Gmail tools are untouched (safe under a broad matcher) ────────────────
for t in ["Bash", "Read", "mcp__claude_ai_Google_Drive__create_file",
          "mcp__sutando-station__composio_exec"]:
    r = run({"tool_name": t, "tool_input": {}})
    check(f"allow non-gmail: {t}",
          r.returncode == 0 and decision(r) is None and not r.stdout.strip())

# ── Escape hatch: env var lifts the guard ─────────────────────────────────────
r = run({"tool_name": "mcp__claude_ai_Gmail__create_draft", "tool_input": {}},
        env_extra={"SUTANDO_ALLOW_GMAIL_CONNECTOR_WRITES": "1"})
check("SUTANDO_ALLOW_GMAIL_CONNECTOR_WRITES=1 lifts the guard",
      r.returncode == 0 and not r.stdout.strip())

# ── Malformed stdin: fail-OPEN (exit 0, no deny) — never wedge the core ───────
r = run("this is not json")
check("malformed stdin fails open", r.returncode == 0 and decision(r) is None)

if failures:
    print(f"\nFAIL — {len(failures)}: {failures}")
    sys.exit(1)
print("\nPASS — gmail-write-guard tests")
