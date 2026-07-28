#!/bin/bash
# Startup must start voice for a managed-only install.
#
# Regression test for the gap on #2197: the PR taught resolveCredential() to load
# a managed voice key, but `configure_startup_runtime` still gated on the BYO env
# vars alone. A provisioned managed user with no GEMINI_* env therefore restarted
# into SKIP_VOICE=1 and voice silently stayed offline — the new tier was
# unreachable through the only path that actually boots the product.
#
# Isolation: the gate resolves its workspace through "$REPO"/scripts/sutando-config.sh,
# so pointing REPO at a stub repo redirects the lookup at a temp workspace without
# touching the developer's real one. $SUTANDO_WORKSPACE is deliberately NOT used —
# it stopped being honored for workspace resolution in v0.8 (#1440), so a test
# built on it would pass while proving nothing.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

STUB="$TMP/stub-repo"
WS="$TMP/workspace"
mkdir -p "$STUB/scripts" "$WS/state/auth"
cat > "$STUB/scripts/sutando-config.sh" <<STUBEOF
#!/bin/bash
[ "\${1:-}" = "workspace" ] && printf '%s\n' "$WS"
STUBEOF
chmod +x "$STUB/scripts/sutando-config.sh"

# Run the REAL gate with the workspace lookup redirected at the stub.
run_gate() {
  env -i PATH="/usr/bin:/bin" REPO="$STUB" \
    GEMINI_API_KEY="${1:-}" GEMINI_VOICE_API_KEY="${2:-}" \
    bash -c 'cd "$1"; source "$2/src/startup-runtime.sh"; configure_startup_runtime; printf "SKIP_VOICE=%s\n" "${SKIP_VOICE:-0}"' \
    _ "$TMP" "$REPO"
}

write_managed() { printf '%s\n' "$1" > "$WS/state/auth/managed-credentials.json"; }
clear_managed() { rm -f "$WS/state/auth/managed-credentials.json"; }

fail() { echo "FAIL: $1" >&2; exit 1; }

# 1. Managed-only install (no env keys at all) MUST start voice. This is the
#    assertion that fails on the unpatched gate.
write_managed '{"capabilities":{"gemini-voice":{"key":"managed-voice-key"}}}'
out="$(run_gate)"
grep -q 'SKIP_VOICE=0' <<<"$out" || fail "managed-only voice key did not start voice: $out"
grep -q 'managed credentials' <<<"$out" || fail "enabled via managed tier but did not say so: $out"

# 2. Voice falls back to the gemini-text slot, matching
#    CAPABILITY_FALLBACKS['gemini-voice'] = ['gemini-voice','gemini-text'].
write_managed '{"capabilities":{"gemini-text":{"key":"managed-text-key"}}}'
grep -q 'SKIP_VOICE=0' <<<"$(run_gate)" || fail "gemini-text slot did not satisfy the voice capability"

# 3. A genuinely credential-free install MUST stay disabled — the control. If this
#    passes unconditionally the suite proves nothing about tier detection.
clear_managed
out="$(run_gate)"
grep -q 'SKIP_VOICE=1' <<<"$out" || fail "credential-free install was not disabled: $out"
grep -q 'managed-credentials.json' <<<"$out" || fail "disabled without naming the managed path: $out"
grep -q 'GEMINI_VOICE_API_KEY' <<<"$out" || fail "disabled without naming the BYO escape hatch: $out"

# 4. Malformed / empty / wrong-shape managed files skip the tier rather than
#    throwing, mirroring readManaged()'s try/catch contract. Each must land
#    disabled, not crash the gate under `set -e`.
for bad in 'not json at all' '{}' '{"capabilities":[]}' '{"capabilities":{"gemini-voice":{}}}' \
           '{"capabilities":{"gemini-voice":{"key":""}}}' '{"capabilities":{"gemini-voice":{"key":123}}}'; do
  write_managed "$bad"
  got="$(run_gate)"
  grep -q 'SKIP_VOICE=1' <<<"$got" || fail "malformed managed file was treated as a credential: $bad -> $got"
done

# 5. BYO env still works with no managed file — the pre-existing path must not
#    regress, including the dedicated voice var.
clear_managed
grep -q 'SKIP_VOICE=0' <<<"$(run_gate byo-text-key)"  || fail "GEMINI_API_KEY regressed"
grep -q 'SKIP_VOICE=0' <<<"$(run_gate '' byo-voice-key)" || fail "GEMINI_VOICE_API_KEY regressed"

# 6. An unreadable managed file must not wedge startup (permissions can be wrong
#    on a half-provisioned install).
write_managed '{"capabilities":{"gemini-voice":{"key":"k"}}}'
chmod 000 "$WS/state/auth/managed-credentials.json"
if [ "$(id -u)" -ne 0 ]; then  # root ignores the mode bits
  grep -q 'SKIP_VOICE=1' <<<"$(run_gate)" || fail "unreadable managed file did not fail closed"
fi
chmod 644 "$WS/state/auth/managed-credentials.json"

echo "PASS: startup voice gate recognizes the managed tier"
