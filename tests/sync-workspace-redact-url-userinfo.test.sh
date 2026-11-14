#!/usr/bin/env bash
# A remote URL carrying userinfo (a token or user:pass) is never printed
# verbatim: not to stderr, not to --status output, not to the sync log.

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

TMP="$(mktemp -d -t sutando-redact-url.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

SECRET="ghp_SENTINELtoken0123456789"
PASS="hunter2SENTINELpass"
TOKEN_URL="https://x-access-token:${SECRET}@example.invalid/org/vault.git"
WSID="ab12cd"

echo "_redact_url (unit):"

# The helper is exercised as the script defines it, not as a copy in this test.
helper="$(awk '/^_redact_url(_text)?\(\) \{/,/^\}/' "$SYNC")"
if [ -z "$helper" ]; then
    bad "sync-workspace.sh defines _redact_url" "no definition found in $SYNC"
else
    ok "sync-workspace.sh defines _redact_url"
    eval "$helper"
    check() {
        local got
        got="$(_redact_url "$1")"
        if [ "$got" = "$2" ]; then ok "$3"; else bad "$3" "in=$1 want=$2 got=$got"; fi
    }
    check "$TOKEN_URL" "https://***@example.invalid/org/vault.git" \
        "https token userinfo is redacted"
    check "https://alice:${PASS}@git.example.invalid:8443/r.git" \
        "https://***@git.example.invalid:8443/r.git" \
        "user:pass userinfo is redacted, port and path kept"
    check "https://${SECRET}@example.invalid/r.git" "https://***@example.invalid/r.git" \
        "a bare token as the user is redacted"
    check "https://github.com/org/repo.git" "https://github.com/org/repo.git" \
        "a URL with no userinfo is unchanged"
    check "https://example.invalid/r.git?ref=a@b" "https://example.invalid/r.git?ref=a@b" \
        "an @ after the authority is not mistaken for userinfo"
    check "git@github.com:org/repo.git" "git@github.com:org/repo.git" \
        "scp-style git@host:path is unchanged"
    check "/tmp/some/vault.git" "/tmp/some/vault.git" "a local path is unchanged"
    check "" "" "an empty value stays empty"
fi

echo "print paths (end-to-end):"

mk_skel() {
    local skel="$TMP/$1" url="${2:-}"
    mkdir -p "$skel/scripts" "$skel/workspace/.sutando-vault"
    printf '%s\n' "$WSID" > "$skel/workspace/.sutando-vault/ws-id"
    cp "$SYNC" "$skel/scripts/sync-workspace.sh"
    cat > "$skel/scripts/sutando-config.sh" << STUB
#!/usr/bin/env bash
case "\${1:-}" in
    workspace) echo "\$(cd "\$(dirname "\$0")/.." && pwd)/workspace" ;;
    vault-url) printf '%s' '$url' ;;
    *) : ;;
esac
STUB
    chmod +x "$skel/scripts/sutando-config.sh"
    printf '%s' "$skel"
}

run_sync() {
    local skel="$1"; shift
    env -u SUTANDO_MEMORY_REPO \
        SUTANDO_REPO_DIR="$skel" \
        SUTANDO_SYNC_LOCK_DIR="$skel/lock.d" \
        SYNC_WORKSPACE_LOG="$skel/sync.log" \
        bash "$skel/scripts/sync-workspace.sh" "$@" 2>&1
}

no_secret() {
    local name="$1" text="$2" anchor="$3"
    # Anchor first: a run that printed nothing would pass the absence check vacuously.
    if ! printf '%s' "$text" | grep -qF "$anchor"; then
        bad "$name" "expected line ($anchor) not printed; got: $text"
    elif printf '%s' "$text" | grep -qE "$SECRET|$PASS"; then
        bad "$name" "secret printed: $(printf '%s' "$text" | grep -E "$SECRET|$PASS")"
    else
        ok "$name"
    fi
}

# 1: configured token URL, --status prints VAULT_URL.
skel="$(mk_skel status-configured "$TOKEN_URL")"
out="$(run_sync "$skel" --status)"
no_secret "--status VAULT_URL line hides a configured token" "$out" \
    'VAULT_URL:     https://***@example.invalid/org/vault.git'

# 2: token-bearing workspace origin that cannot be reached: the resolution
# notice and the --status "candidate NOT adopted" line both print it.
skel="$(mk_skel status-declined)"
git -C "$skel/workspace" init -q
git -C "$skel/workspace" remote add origin "$TOKEN_URL"
out="$(run_sync "$skel" --status)"
no_secret "the unreachable-origin notice hides the token" "$out" \
    "could not reach the workspace repo's origin (https://***@example.invalid"
no_secret "--status 'candidate NOT adopted' hides the token" "$out" \
    'candidate NOT adopted: https://***@example.invalid/org/vault.git'

# 3: --init --dry-run prints the origin it would set.
skel="$(mk_skel init-dry-run "$TOKEN_URL")"
out="$(run_sync "$skel" --init --dry-run)"
no_secret "--init --dry-run hides the token in 'would set git remote origin'" "$out" \
    'would set git remote origin = https://***@example.invalid/org/vault.git'

# 4: real --init adds the remote; stderr and the sync log both record it, and
# the remote git actually uses keeps the full credential.
skel="$(mk_skel init-real "$TOKEN_URL")"
out="$(run_sync "$skel" --init)"
no_secret "--init 'added remote origin' hides the token" "$out" \
    'added remote origin https://***@example.invalid/org/vault.git'
log_text="$(cat "$skel/sync.log" 2>/dev/null || true)"
no_secret "the sync log hides the token" "$log_text" \
    'added remote origin https://***@example.invalid/org/vault.git'
if [ "$(git -C "$skel/workspace" remote get-url origin 2>/dev/null)" = "$TOKEN_URL" ]; then
    ok "the remote git uses keeps the real credential"
else
    bad "the remote git uses keeps the real credential" \
        "origin is: $(git -C "$skel/workspace" remote get-url origin 2>&1)"
fi

# 5: --init over an existing, different token origin prints both old and new.
skel="$(mk_skel init-update "$TOKEN_URL")"
git -C "$skel/workspace" init -q
git -C "$skel/workspace" remote add origin "https://alice:${PASS}@old.example.invalid/v.git"
out="$(run_sync "$skel" --init)"
no_secret "--init 'updating remote origin' hides both old and new credentials" "$out" \
    'updating remote origin from https://***@old.example.invalid/v.git to https://***@example.invalid/org/vault.git'

if [ "$fail" = "0" ]; then
    echo "ALL TESTS PASS"
    exit 0
fi
echo "TESTS FAILED"
exit 1
