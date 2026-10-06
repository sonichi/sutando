#!/usr/bin/env bash
# Output of git's own network commands (push, fetch) that embeds a token-bearing
# remote URL reaches the sync log redacted, and the script's exit status is unchanged.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
SYNC="$REPO/scripts/sync-workspace.sh"

fail=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s — %s\n' "$1" "${2:-}"; fail=1; }

export GIT_AUTHOR_NAME="${GIT_AUTHOR_NAME:-sync-ws-redact-test}"
export GIT_AUTHOR_EMAIL="${GIT_AUTHOR_EMAIL:-sync-ws-redact-test@invalid}"
export GIT_COMMITTER_NAME="$GIT_AUTHOR_NAME"
export GIT_COMMITTER_EMAIL="$GIT_AUTHOR_EMAIL"
export GIT_TERMINAL_PROMPT=0

TMP="$(mktemp -d -t sutando-redact-git-out.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

SECRET="ghp_SENTINELgitout0123456789"
WSID="ab12cd"
# Recent git strips userinfo from its own "unable to access" line; a remote helper
# receives the full URL and prints whatever it likes, which is what this shim does.
TOKEN_URL="sutfail://x-access-token:${SECRET}@example.invalid/org/vault.git"
REDACTED="sutfail://***@example.invalid/org/vault.git"
mkdir -p "$TMP/bin"
cat > "$TMP/bin/git-remote-sutfail" << 'HELPER'
#!/bin/sh
echo "fatal: unable to access '$2/': Could not resolve host: example.invalid" >&2
exit 128
HELPER
chmod +x "$TMP/bin/git-remote-sutfail"
export PATH="$TMP/bin:$PATH"

echo "_redact_url_text + _log_cmd (unit):"

helpers="$(awk '/^_redact_url_text\(\) \{/,/^\}/; /^_log_cmd\(\) \{/,/^\}/' "$SYNC")"
if ! printf '%s' "$helpers" | grep -q '^_log_cmd()' \
   || ! printf '%s' "$helpers" | grep -q '^_redact_url_text()'; then
    bad "sync-workspace.sh defines _redact_url_text and _log_cmd" "not found in $SYNC"
else
    ok "sync-workspace.sh defines _redact_url_text and _log_cmd"
    eval "$helpers"
    got="$(printf '%s\n' "fatal: unable to access 'https://x-access-token:${SECRET}@github.com/o/r.git/': 403; also ssh://u:${SECRET}@h:22/x and git@github.com:o/r.git" | _redact_url_text)"
    want="fatal: unable to access 'https://***@github.com/o/r.git/': 403; also ssh://***@h:22/x and git@github.com:o/r.git"
    if [ "$got" = "$want" ]; then ok "every URL's userinfo inside free text is redacted"
    else bad "every URL's userinfo inside free text is redacted" "got=$got"; fi

    LOG="$TMP/unit.log"
    rc=0
    ( set -euo pipefail
      _log_cmd sh -c "echo 'To https://u:${SECRET}@h/x'; echo 'fatal: https://u:${SECRET}@h/x' >&2; exit 7" ) || rc=$?
    if [ "$rc" = "7" ]; then ok "_log_cmd returns the command's own exit status (7)"
    else bad "_log_cmd returns the command's own exit status (7)" "rc=$rc"; fi
    if grep -qF "$SECRET" "$LOG"; then bad "_log_cmd redacts stdout and stderr" "$(cat "$LOG")"
    elif [ "$(grep -cF 'https://***@h/x' "$LOG")" = "2" ]; then ok "_log_cmd redacts stdout and stderr"
    else bad "_log_cmd redacts stdout and stderr" "log: $(cat "$LOG")"; fi
fi

echo "git network output (end-to-end):"

skel="$TMP/skel"
mkdir -p "$skel/scripts" "$skel/workspace/.sutando-vault"
printf '%s\n' "$WSID" > "$skel/workspace/.sutando-vault/ws-id"
cp "$SYNC" "$skel/scripts/sync-workspace.sh"
cat > "$skel/scripts/sutando-config.sh" << STUB
#!/usr/bin/env bash
case "\${1:-}" in
    workspace) echo "\$(cd "\$(dirname "\$0")/.." && pwd)/workspace" ;;
    vault-url) printf '%s' '$TOKEN_URL' ;;
    vault-sync-include) echo "notes/" ;;
    *) : ;;
esac
STUB
chmod +x "$skel/scripts/sutando-config.sh"

run_sync() {
    env -u SUTANDO_MEMORY_REPO \
        SUTANDO_REPO_DIR="$skel" \
        SUTANDO_SYNC_LOCK_DIR="$skel/lock.d" \
        SYNC_WORKSPACE_LOG="$skel/sync.log" \
        bash "$skel/scripts/sync-workspace.sh" "$@" 2>&1
}

check_run() {
    local name="$1" want_rc="$2" rc="$3" out="$4" before="$5" new_log
    new_log="$(tail -c +"$((before + 1))" "$skel/sync.log" 2>/dev/null || true)"
    if [ "$rc" = "$want_rc" ]; then ok "$name exits $want_rc"
    else bad "$name exits $want_rc" "rc=$rc; out: $out"; fi
    # Anchor first: a run whose git output never reached the log would pass vacuously.
    if ! printf '%s' "$new_log" | grep -qF "unable to access '$REDACTED/'"; then
        bad "$name logs git's error with the URL redacted" "log: $new_log"
    else
        ok "$name logs git's error with the URL redacted"
    fi
    if printf '%s\n%s' "$new_log" "$out" | grep -qF "$SECRET"; then
        bad "$name never prints or logs the token" \
            "$(printf '%s\n%s' "$new_log" "$out" | grep -F "$SECRET")"
    else
        ok "$name never prints or logs the token"
    fi
}

log_size() { wc -c < "$skel/sync.log" 2>/dev/null | tr -d ' ' || echo 0; }

# --init: the first push to the host branch fails; init reports it and exits 1.
before=0; rc=0; out="$(run_sync --init)" || rc=$?
check_run "--init (failed first push)" 1 "$rc" "$out" "$before"

# --push-only with a local change: the push fails and the script exits 1.
mkdir -p "$skel/workspace/notes"
echo "change" > "$skel/workspace/notes/redact-test.md"
before="$(log_size)"; rc=0; out="$(run_sync --push-only)" || rc=$?
check_run "--push-only (failed push)" 1 "$rc" "$out" "$before"

# --pull-only: the fetch fails and, as before, errexit ends the run with git's status.
before="$(log_size)"; rc=0; out="$(run_sync --pull-only)" || rc=$?
check_run "--pull-only (failed fetch)" 128 "$rc" "$out" "$before"

if [ "$(git -C "$skel/workspace" remote get-url origin 2>/dev/null)" = "$TOKEN_URL" ]; then
    ok "the remote git uses keeps the real credential"
else
    bad "the remote git uses keeps the real credential" \
        "origin is: $(git -C "$skel/workspace" remote get-url origin 2>&1)"
fi

if [ "$fail" = "0" ]; then
    echo "ALL TESTS PASS"
    exit 0
fi
echo "TESTS FAILED"
exit 1
