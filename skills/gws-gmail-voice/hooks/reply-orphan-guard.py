#!/usr/bin/env python3
"""PreToolUse(Bash) hook: block a `gws gmail +send` carrying a reply subject.

+send cannot set In-Reply-To/References, so the reply starts a new thread.
"""
from __future__ import annotations

import json
import re
import shlex
import sys

REPLY_SUBJECT = re.compile(r'(?i)(?:^|["\'\s:])(?:re|fwd|fw)\s*:', re.M)
SEND_CALL = re.compile(r'gws\s+gmail\s+\+send\b')


def _gmail_calls(tokens: list[str]) -> list[tuple[str, list[str]]]:
    """(subcommand, following tokens) per `gws gmail …` call in the command."""
    return [(tokens[i + 2], tokens[i + 3:]) for i, tok in enumerate(tokens)
            if tok == "gws" and tokens[i + 1:i + 2] == ["gmail"] and len(tokens) > i + 2]


def _flag(tokens: list[str], name: str) -> str | None:
    for i, tok in enumerate(tokens):
        if tok == name:
            return tokens[i + 1] if i + 1 < len(tokens) else ""
        if tok.startswith(name + "="):
            return tok.split("=", 1)[1]
    return None


def decide(cmd: str) -> bool:
    """True when this command must be denied."""
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        # Unparseable quoting fails CLOSED — never let untokenizable text
        # be read as evidence that the call is a safe one.
        return bool(SEND_CALL.search(cmd) and REPLY_SUBJECT.search(cmd))
    for sub, rest in _gmail_calls(tokens):
        if sub != "+send":
            continue  # +reply is the only threading form; nothing else sends.
        subject = _flag(rest, "--subject")
        if REPLY_SUBJECT.search(subject if subject is not None else cmd):
            return True
    return False


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
