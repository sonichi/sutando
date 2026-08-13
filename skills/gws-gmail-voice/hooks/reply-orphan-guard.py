#!/usr/bin/env python3
"""gmail-reply-orphan-guard — PreToolUse(Bash) hook that BLOCKS a `gws gmail +send`
whose subject is a reply, because +send has no thread flag and orphans it.

`gws gmail +send` cannot set In-Reply-To/References, so a reply sent through it
starts a NEW thread — observable as `threadId == id`. Only `+reply --message-id`
threads correctly. Detection after the fact does not help; the send is the damage.

Fail-OPEN on any error: a crashing hook must never wedge the core.
"""
import json
import re
import sys

REPLY_SUBJECT = re.compile(r'(?i)(?:^|["\'\s:])(?:re|fwd|fw)\s*:', re.M)
SEND_CALL     = re.compile(r'gws\s+gmail\s+\+send\b')
HAS_THREADING = re.compile(r'--message-id\b|\+reply\b|in-?reply-?to', re.I)

def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if payload.get("tool_name") != "Bash":
        return 0
    cmd = (payload.get("tool_input") or {}).get("command") or ""
    if not SEND_CALL.search(cmd):
        return 0
    if HAS_THREADING.search(cmd):
        return 0
    if not REPLY_SUBJECT.search(cmd):
        return 0
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": (
            "`gws gmail +send` has no thread flag, so a Re:/Fwd: subject sent this way starts a NEW "
            "thread (threadId == id) and the recipient sees an orphan. Use "
            "`gws gmail +reply --message-id <their-message-id>` instead, which sets "
            "In-Reply-To/References. Get the id from the message you are replying to."),
    }}))
    return 0

if __name__ == "__main__":
    sys.exit(main())
