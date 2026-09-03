#!/usr/bin/env python3
"""release-target-guard — PreToolUse hook that DENIES `gh release create|edit`
whose ``--target`` is an abbreviated commit SHA.

GitHub rejects those with ``Release.target_commitish is invalid`` and creates
nothing, so the cut silently does not happen at the moment it looks finished.

Why a hook rather than a note: this failed twice in fourteen hours on one host,
with the correction written into the build log between the two occurrences. The
rule is easy to know and useless to know, because the value is not chosen — it is
pasted from whatever printed last, and every tool prints the abbreviated form
(``gh pr view``, ``.sha[0:12]``, a previous log line). A lesson cannot intercept a
paste; the shell can.

Allowed: a branch or tag name, and a full 40-character hex SHA. Denied: a hex run
of 7 or more that is not exactly 40 — git's default abbreviation is 7, so a
shorter hex-looking value is a name, not a paste.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys

FULL_SHA = re.compile(r"\A[0-9a-f]{40}\Z")
HEX_RUN = re.compile(r"\A[0-9a-f]{7,}\Z")
# A separator ends the gh command; anything after it belongs to another tool.
SEPARATORS = (";", "&&", "||", "|", "&", "(", ")")


def _deny(reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}))
    sys.exit(0)


def _is_release_cut(rest) -> bool:
    """`release create|edit` anywhere in this gh command, not at a fixed offset.

    A global flag between them (`-R`, `--repo`, `--hostname`) is ordinary, and
    keying on the two words right after `gh` let it disarm the guard entirely.
    """
    for j, w in enumerate(rest):
        if w in SEPARATORS:
            return False
        if w == "release" and rest[j + 1:j + 2] in (["create"], ["edit"]):
            return True
    return False


def targets(command: str):
    """Every --target value belonging to a `gh release create|edit` in `command`.

    `punctuation_chars` tokenizes `;` `&&` `|` as their own words while leaving
    quoted text intact, so a separator ends the gh command instead of gluing to
    the sha before it — and another tool's `--target` after one is not read.
    """
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        words = list(lex)
    except ValueError:
        return []
    out, i, armed = [], 0, False
    while i < len(words):
        w = words[i]
        if w in SEPARATORS:
            armed = False
        if os.path.basename(w) == "gh":
            armed = _is_release_cut(words[i + 1:])
        if armed and w == "--target" and i + 1 < len(words):
            out.append(words[i + 1])
        elif armed and w.startswith("--target="):
            out.append(w.split("=", 1)[1])
        i += 1
    return out


def offenders(command: str):
    """The --target values GitHub will reject. Full SHAs and names are fine."""
    return [t for t in targets(command)
            if HEX_RUN.match(t) and not FULL_SHA.match(t)]


def main(argv):
    if os.environ.get("SUTANDO_SKIP_RELEASE_TARGET_GUARD") == "1":
        sys.exit(0)
    payload = json.load(sys.stdin)
    if (payload.get("tool_name") or "") != "Bash":
        sys.exit(0)
    command = (payload.get("tool_input") or {}).get("command") or ""
    bad = offenders(command)
    if not bad:
        sys.exit(0)
    _deny(
        f"RELEASE TARGET GUARD: --target {', '.join(bad)} is an abbreviated SHA. "
        "GitHub answers 'Release.target_commitish is invalid' and creates nothing, "
        "so the release looks cut and is not.\n\n"
        "Use the branch, or resolve the full SHA in the same command so it is "
        "never typed:\n"
        "    --target main\n"
        "    FULL=$(gh api repos/<owner>/<repo>/commits/main -q '.sha')\n"
        "    gh release edit <tag> --target \"$FULL\"\n\n"
        "[release-target-guard]"
    )


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except SystemExit:
        raise
    except Exception as e:  # fail-open: never wedge the core on a guard bug
        print(f"release-target-guard: non-fatal error, allowing: {e}", file=sys.stderr)
        sys.exit(0)
