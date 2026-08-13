#!/usr/bin/env python3
"""PreToolUse(Bash) hook: block a `gws gmail +send` carrying a reply subject.

+send cannot set In-Reply-To/References, so the reply starts a new thread.
"""
import json
import re
import sys

REPLY_SUBJECT = re.compile(r'(?i)(?:^|["\'\s:])(?:re|fwd|fw)\s*:', re.M)
SEND_CALL = re.compile(r'gws\s+gmail\s+\+send\b')
# +reply is the only gws form that threads. --message-id does NOT rescue +send:
# the flag is not wired to a header there, so honouring it reopens the orphan.
SAFE_FORM = re.compile(r'\+reply\b|in-?reply-?to', re.I)


def decide(cmd: str) -> bool:
    """True when this command must be denied."""
    return bool(SEND_CALL.search(cmd)
                and not SAFE_FORM.search(cmd)
                and REPLY_SUBJECT.search(cmd))


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(payload, dict) or payload.get("tool_name") != "Bash":
        return 0
    # tool_input has been a str and a list in real payloads; .get() on those
    # raises and the hook then emits no decision at all.
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0
    cmd = tool_input.get("command")
    if not isinstance(cmd, str) or not decide(cmd):
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
