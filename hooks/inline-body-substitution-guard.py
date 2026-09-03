#!/usr/bin/env python3
"""inline-body-substitution-guard — PreToolUse hook that DENIES a `gh` command
passing a `--body`/`--notes` inline in DOUBLE quotes when the text contains a
backtick or `$(`.

The shell substitutes both before gh ever sees them, so a published comment
arrives with holes where its code spans were, and nothing reports an error: the
command succeeds, the URL comes back, and only re-reading the comment shows it.

Single quotes are safe and allowed; `--body-file` is the fix and is what this
steers to. Scoped to `gh` because that is where the loss is silent AND public.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys

BODY_FLAGS = ("--body", "--notes", "--title")
# What the shell eats inside double quotes. `$VAR` is deliberately absent: it is
# a normal, usually-intended interpolation, unlike a code span or a subshell.
SUBSTITUTES = re.compile(r"`|\$\(")
EQUALS_FORM = re.compile(r"(--body|--notes|--title)=")


def _deny(reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}))
    sys.exit(0)


def _newline_separators(command: str) -> str:
    """A newline ends the command unless it sits inside a quoted argument,
    where it is ordinary body text — so quote state decides, not str.split."""
    out, quote, esc = [], None, False
    for ch in command:
        if esc:
            out.append(ch); esc = False; continue
        if ch == "\\" and quote != "'":
            out.append(ch); esc = True; continue
        if quote is None and ch in ("'", '"'):
            quote = ch
        elif ch == quote:
            quote = None
        out.append(";" if (ch == "\n" and quote is None) else ch)
    return "".join(out)


def offenders(command: str):
    """Flags whose inline double-quoted value the shell would rewrite.

    posix=False keeps the quote characters on each token, which is the whole
    point — after posix parsing a single- and a double-quoted body look the same.
    """
    # `--body="x"` never lexes as one token: a quote only opens a string at a
    # token boundary, so normalise it to the space form before splitting.
    command = EQUALS_FORM.sub(r"\1 ", command)
    command = _newline_separators(command)
    try:
        lex = shlex.shlex(command, posix=False, punctuation_chars=True)
        lex.whitespace_split = True
        words = list(lex)
    except ValueError:
        return []
    out, i, armed = [], 0, False
    while i < len(words):
        w = words[i]
        if w in (";", "&&", "||", "|", "&", "(", ")"):
            armed = False
        if os.path.basename(w) == "gh":
            armed = True
        if armed and w in BODY_FLAGS and i + 1 < len(words):
            val = words[i + 1]
            if val.startswith('"') and SUBSTITUTES.search(val):
                out.append(w)
        i += 1
    return out


def main(argv):
    if os.environ.get("SUTANDO_SKIP_INLINE_BODY_GUARD") == "1":
        sys.exit(0)
    payload = json.load(sys.stdin)
    if (payload.get("tool_name") or "") != "Bash":
        sys.exit(0)
    command = (payload.get("tool_input") or {}).get("command") or ""
    bad = offenders(command)
    if not bad:
        sys.exit(0)
    _deny(
        f"INLINE BODY GUARD: {', '.join(sorted(set(bad)))} is passed inline in double "
        "quotes and contains a backtick or $( . The shell substitutes those before gh "
        "runs, so the comment publishes with holes where its code spans were — and the "
        "command still succeeds and still returns a URL.\n\n"
        "Write the body to a file and pass it:\n"
        "    cat > /tmp/body.md <<'EOF'\n    ...\n    EOF\n"
        "    gh pr comment <n> --repo <o/r> --body-file /tmp/body.md\n\n"
        "Single quotes are safe if the body has no single quote of its own.\n"
        "[inline-body-substitution-guard]"
    )


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except SystemExit:
        raise
    except Exception as e:  # fail-open: never wedge the core on a guard bug
        print(f"inline-body-substitution-guard: non-fatal error, allowing: {e}", file=sys.stderr)
        sys.exit(0)
