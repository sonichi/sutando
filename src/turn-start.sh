#!/bin/bash
# UserPromptSubmit hook: a new turn is starting, so re-arm the Stop reminder.
# Fails open and silent — it runs before every prompt and must never block one.
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# A bare `python3` fallback re-enters the very CLT stub the resolver refused,
# so a refusal means skip the reset rather than run an unvalidated interpreter.
if ! PYBIN="$(bash "$REPO_DIR/scripts/sutando-config.sh" python-bin 2>/dev/null)" \
   || [ -z "$PYBIN" ] || [ ! -x "$PYBIN" ]; then
  exit 0
fi
# No --session here: turn_ledger.py reads $CLAUDE_CODE_SESSION_ID itself (same
# established idiom as scripts/skill-read-receipt.py's _session_id()), which
# Claude Code sets on every subprocess it spawns, hooks included — see
# turn_ledger.py's SESSION SCOPING note. Absent that env var (a non-Claude-Code
# context), behavior is exactly the original shared-file default.
"$PYBIN" "$REPO_DIR/src/turn_ledger.py" turn-start >/dev/null 2>&1
exit 0
