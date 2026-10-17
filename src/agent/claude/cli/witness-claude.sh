#!/bin/bash
# Witness core pane wrapper: execs "$@" with the vault's CLAUDE_CODE_OAUTH_TOKEN in its env (--check: only test for it).
# The token moves by pipe and exec alone, never argv, a file or tmux, so no other pane or process sees it.

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
. "$REPO/scripts/python-binary.sh"
PY="$(resolve_python "$REPO")"

witness_vault_token() {
  [ -n "$PY" ] || return 1
  "$PY" - "$REPO/src" 2>/dev/null <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from vault_intercept import get_vault_key
try:
    tok = get_vault_key("CLAUDE_CODE_OAUTH_TOKEN")
except KeyError:
    sys.exit(1)
if not tok:
    sys.exit(1)
sys.stdout.write(tok)
PY
}

if [ "${1:-}" = "--check" ]; then
  witness_vault_token > /dev/null
  exit
fi

if _tok="$(witness_vault_token)" && [ -n "$_tok" ]; then
  export CLAUDE_CODE_OAUTH_TOKEN="$_tok"
fi
unset _tok
exec "$@"
