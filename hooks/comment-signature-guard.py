#!/usr/bin/env python3
"""PreToolUse: a published comment or PR body must carry this agent's MXID.

Attribution across the shared `qingyun-wu` login rests on the body signature —
the login cannot tell two agents apart, and neither can the commit email. That
rule was followed by discipline alone, and the discipline drifted: measured
2026-09-03 across three PRs, 39 comments carry the older `Signed: @<mxid>` form
and 2 the newer `— sutando-qingyun-air (@<mxid>)`. Both are fine; an unsigned
body is not, because it is indistinguishable from the peer's.

So the check is on the MXID, never the surrounding prose — matching one literal
is what let the format drift go unnoticed for weeks.
"""
import json
import os
import re
import shlex
import sys

# No default: a node that deploys this without SUTANDO_AGENT_MXID would
# otherwise enforce another agent's identity and deny every comment.
MXID = os.environ.get("SUTANDO_AGENT_MXID", "")
BODY_FLAGS = {"--body", "-b"}
FILE_FLAGS = {"--body-file", "-F"}
# Only subcommands that PUBLISH prose under this login. `gh pr view`, `gh api`
# and friends carry no authored body a reader would attribute.
PUBLISHING = (("pr", "comment"), ("issue", "comment"), ("pr", "create"), ("issue", "create"))
EQUALS_FORM = re.compile(r"(--body|--body-file)=")


def _is_gh(word):
    return word.rsplit("/", 1)[-1] == "gh"


def _publishes(words):
    """`gh <a> <b>` where (a, b) is a publishing pair, allowing global flags
    between them — `gh -R o/r pr comment` publishes exactly as `gh pr comment`."""
    for i, w in enumerate(words):
        if not _is_gh(w):
            continue
        rest = words[i + 1:]
        # Adjacency, not position: a global flag TAKES A VALUE (`-R owner/repo`),
        # so dropping flags alone still leaves the value ahead of the subcommand.
        for j in range(len(rest) - 1):
            for a, b in PUBLISHING:
                if rest[j] == a and rest[j + 1] == b:
                    return f"{a} {b}"
    return None


def unsigned_body(command):
    """The body this command would publish, when it carries no MXID."""
    if not isinstance(command, str) or "gh" not in command:
        return None
    command = EQUALS_FORM.sub(r"\1 ", command)
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        words = list(lex)
    except ValueError:
        return None
    sub = _publishes(words)
    if sub is None:
        return None
    for i, w in enumerate(words):
        if w in BODY_FLAGS and i + 1 < len(words):
            if MXID not in words[i + 1]:
                return (sub, "--body")
        if w in FILE_FLAGS and i + 1 < len(words):
            path = words[i + 1]
            try:
                with open(path, encoding="utf-8", errors="replace") as handle:
                    text = handle.read()
            except OSError:
                return None  # unreadable: the gate cannot answer, so it does not
            if MXID not in text:
                return (sub, path)
    return None


def main(argv):
    if os.environ.get("SUTANDO_ALLOW_UNSIGNED_COMMENT") == "1":
        return 0
    if not MXID:
        print("comment-signature-guard: SUTANDO_AGENT_MXID unset — not enforcing",
              file=sys.stderr)
        return 0
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if payload.get("tool_name") != "Bash":
        return 0
    found = unsigned_body((payload.get("tool_input") or {}).get("command"))
    if not found:
        return 0
    sub, where = found
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": (
            f"BLOCKED: this `gh {sub}` body carries no `{MXID}`, so a reader cannot tell it "
            f"from the peer agent's — both push under the same login and the commit email is "
            f"many-to-one too. Body checked: {where}. Add a signature line containing the MXID "
            f"(either `— sutando-qingyun-air ({MXID})` or the older `Signed: @{MXID}` form; the "
            f"check is on the MXID, not the wording). Override once with "
            f"SUTANDO_ALLOW_UNSIGNED_COMMENT=1. [comment-signature-guard]"),
    }}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
