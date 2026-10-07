#!/usr/bin/env python3
"""PreToolUse: gate a Bash `mv` that lands a dedup-staged result into `results/`
on `skills/proactive-loop/scripts/check-dedup-targets.py`, for ANY caller — not
just proactive-loop step 1's own checklist.

WHY THIS EXISTS. Step 1 stages a grouped `[deduped: X]` reply under
`state/dedup-staging/<file>` and only promotes it with:

  S="$WORKSPACE/state/dedup-staging/<file>"
  python3 skills/proactive-loop/scripts/check-dedup-targets.py "$S" && mv -f "$S" "$WORKSPACE/results/<file>"

`check-dedup-targets.py` refuses (exit 1) a staged file whose dedup target
resolves to nothing — `[no-send]` or absent — because the bridge would then
tell the room "see task X" for an X that delivers nothing. That refusal only
holds if the `&&` chain is actually typed. A bare `mv` of a dedup-staged file
into `results/`, run from any skill or any live session with the chain
dropped, bypasses it completely — same architectural gap as gh-policy-gate.py
and memory-index-guard.py, and the same fix: move enforcement to the action.

WHAT COUNTS AS THE ADDITION. A PreToolUse hook sees `tool_input.command` as
the RAW, unexpanded shell text: `$S` is a literal variable reference, not the
path it will resolve to, so this hook cannot resolve it — building real shell
variable tracking is out of scope (fragile, and unlike a `gh`/`Edit`/`Write`
call, a Bash command carries no resolved value the hook can read directly).
Instead it matches literal substrings the way the skill's own prose documents
the pattern (`state/dedup-staging/` -> `results/`): a command (a) invokes `mv`
in some segment whose arguments mention `results` (the destination `mv -f
"$S" ".../results/<file>"` always writes literally, even when the SOURCE is
hidden behind `$S`), (b) the command as a whole — any segment, not just the
`mv`'s own — mentions `dedup-staging` (usually the earlier `S=...` assignment,
`;`-separated from the `mv`), and (c) NO segment anywhere in the same command
invokes `check-dedup-targets.py`. `&&` splits into a SEPARATE segment from
what precedes it (verified against `_shell_scan.segments()` directly, not
assumed — the checker call and the `mv` it gates are almost always in
different segments, unlike gh-policy-gate's same-segment `gh` matches), so (b)
and (c) are scanned across the WHOLE command rather than per segment; only (a)
stays segment-scoped, since a `results`-mentioning `mv` that has nothing to do
with dedup staging (no `dedup-staging` mention anywhere in the command) is not
this pattern.

FAILS OPEN ON UNCERTAINTY, DENIES ONLY ON A POSITIVE FINDING. A command
mentioning only one of `dedup-staging` / `results`, or no `mv` at all, is
allowed — same contract as gh-policy-gate.py and memory-index-guard.py. This
hook only sees ONE Bash command string at a time: a multi-tool-call sequence
(`S=...` in one call, the check+mv in the next) is outside what any single
PreToolUse invocation can observe, the same scope limit the other two accept.

Tokenizes via the shared `_shell_scan` scanner, same reasoning as
gh-policy-gate.py's own docstring for why `shlex`/lookbehind under-denies.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _shell_scan  # noqa: E402  (sibling module; path set above)

_SOURCE_MARKER = "dedup-staging"
_DEST_MARKER = "results"
_CHECKER_MARKER = "check-dedup-targets.py"


def _mv_segments(command):
    """Each `mv`-invoking segment of `command`, as (words-after-mv) texts."""
    if not isinstance(command, str) or "mv" not in command:
        return []
    out = []
    for seg in _shell_scan.segments(command):
        for i, w in enumerate(seg):
            if w.basename_is("mv"):
                out.append([x.text for x in seg[i + 1:]])
                break
    return out


def _flat_word_texts(command):
    """Every non-operator word's text across the WHOLE command, all segments —
    used for the two checks that must see past the `&&`/`;` that splits the
    staging assignment, the checker call, and the `mv` into separate
    segments (see module docstring)."""
    if not isinstance(command, str):
        return []
    return [w.text for w in _shell_scan.words(command) if not w.is_operator]


def check_dedup_staging_bypass(command):
    """Returns a deny reason string, or None to allow."""
    mv_segs = _mv_segments(command)
    if not mv_segs:
        return None
    dest_segs = [seg for seg in mv_segs if any(_DEST_MARKER in w for w in seg)]
    if not dest_segs:
        return None
    all_words = _flat_word_texts(command)
    if not any(_SOURCE_MARKER in w for w in all_words):
        return None
    if any(_CHECKER_MARKER in w for w in all_words):
        return None
    seg_text = " ".join(dest_segs[0])
    return (f"`mv {seg_text}` moves a dedup-staged file into results/ with no "
            f"check-dedup-targets.py call anywhere in the command")


def main(argv):
    if os.environ.get("SUTANDO_ALLOW_UNGATED_DEDUP_STAGING") == "1":
        return 0
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if payload.get("tool_name") != "Bash":
        return 0
    reason = check_dedup_staging_bypass((payload.get("tool_input") or {}).get("command"))
    if not reason:
        return 0
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": (
            f"BLOCKED: {reason}. Chain "
            f"skills/proactive-loop/scripts/check-dedup-targets.py \"$S\" && mv ... first. "
            f"Override once with SUTANDO_ALLOW_UNGATED_DEDUP_STAGING=1. "
            f"[dedup-staging-guard]"),
    }}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
