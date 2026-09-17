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
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _shell_scan  # noqa: E402  (sibling module; path set above)

BODY_FLAGS = ("--body", "--notes", "--title")
# `"$(cat f)"` as the WHOLE value is the intended content, not prose the shell
# mangled — the standard file-passing idiom, and denying it cries wolf.
WHOLE_SUBSTITUTION = re.compile(r'\A"\$\((?:[^()]|\([^()]*\))*\)"\Z')
EQUALS_FORM = re.compile(r"(--body|--notes|--title)=")


def _deny(reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}))
    sys.exit(0)


def offenders(command: str):
    """Flags whose inline double-quoted value the shell would rewrite."""
    # `--body="x"` never lexes as one token: a quote only opens a string at a
    # token boundary, so normalise it to the space form before scanning.
    out = []
    for seg in _shell_scan.segments(EQUALS_FORM.sub(r"\1 ", command)):
        if not any(w.basename_is("gh") for w in seg):
            continue
        for i, w in enumerate(seg):
            if w.text in BODY_FLAGS and i + 1 < len(seg):
                val = seg[i + 1]
                if (val.quoted == '"' and val.expands
                        and not WHOLE_SUBSTITUTION.match(val.raw)):
                    out.append(w.text)
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
