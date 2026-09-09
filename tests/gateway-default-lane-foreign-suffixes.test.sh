#!/usr/bin/env bash
# The default gateway lane must not claim unaddressed proactives whose peek
# room sits on a named lane's homeserver suffix — its gateway 403s them and
# the failure policy parks deliverable work as undeliverable (#3427 adds the
# bridge-side fence, read from GATEWAY_FOREIGN_SUFFIXES). This test pins the
# LAUNCHER half: the tracked config derives that env for the default lane
# from the configured named instances, so a deploy cannot forget it.
#
# Run: bash tests/gateway-default-lane-foreign-suffixes.test.sh
# Exit: 0 = all pass, 1 = failure
set -uo pipefail

REPO="${REPO_UNDER_TEST:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
fails=0

ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s — %s\n' "$1" "${2:-}"; fails=$((fails + 1)); }

echo "default-lane GATEWAY_FOREIGN_SUFFIXES derivation:"

# Each case runs the SHIPPED function in a clean subshell so ambient
# AG2_REMOTE_TOKEN_* / GATEWAY_FOREIGN_SUFFIXES on the invoking host cannot
# leak into the derivation under test. GATEWAY_CHANNELS_DIR always points at a
# fixture so the host's real channel .envs never reach the derivation.
FIXTURES="$(mktemp -d)"
trap 'rm -rf "$FIXTURES"' EXIT
mkdir -p "$FIXTURES/empty"
derive() {
  env -i HOME="$HOME" PATH="$PATH" GATEWAY_CHANNELS_DIR="$FIXTURES/empty" "$@" bash -c \
    "source '$REPO/src/startup-runtime.sh' >/dev/null 2>&1; derive_foreign_suffixes"
}

# --- no named instances → empty (fence stays inert) -------------------------
got="$(derive)"
if [ "$got" = "" ]; then
  ok "no named instances derives empty"
else
  bad "no named instances derives empty" "got '$got'"
fi

# --- one named instance → its conventional suffix ---------------------------
got="$(derive AG2_REMOTE_TOKEN_DEV=x)"
if [ "$got" = ":dev.ag2.space" ]; then
  ok "AG2_REMOTE_TOKEN_DEV derives :dev.ag2.space"
else
  bad "AG2_REMOTE_TOKEN_DEV derives :dev.ag2.space" "got '$got'"
fi

# --- two named instances → comma-joined, both present -----------------------
got="$(derive AG2_REMOTE_TOKEN_DEV=x AG2_REMOTE_TOKEN_STAGING=y)"
case "$got" in
  *":dev.ag2.space"*) dev_in=1 ;; *) dev_in=0 ;;
esac
case "$got" in
  *":staging.ag2.space"*) stg_in=1 ;; *) stg_in=0 ;;
esac
if [ "$dev_in" = 1 ] && [ "$stg_in" = 1 ] && [ "$(printf '%s' "$got" | tr -cd ',' | wc -c | tr -d ' ')" = 1 ]; then
  ok "two instances derive both suffixes, comma-joined"
else
  bad "two instances derive both suffixes, comma-joined" "got '$got'"
fi

# --- operator override wins verbatim ----------------------------------------
got="$(derive AG2_REMOTE_TOKEN_DEV=x GATEWAY_FOREIGN_SUFFIXES=":custom.example")"
if [ "$got" = ":custom.example" ]; then
  ok "operator GATEWAY_FOREIGN_SUFFIXES wins verbatim over derivation"
else
  bad "operator GATEWAY_FOREIGN_SUFFIXES wins verbatim over derivation" "got '$got'"
fi

# --- the lane's identity domain outranks the name convention ----------------
# The instance name is a label; the homeserver is whatever the lane's
# AGENT_MXID says. A local homeserver named "localhost" must fence
# ":localhost" — ":local.ag2.space" fences a suffix that exists nowhere.
mkdir -p "$FIXTURES/mxid/local-ag2space" "$FIXTURES/mxid/dev-ag2space"
printf 'AGENT_MXID=@qingyun-local.agent:localhost\n' \
  > "$FIXTURES/mxid/local-ag2space/.env"
printf 'REMOTE_TASK_PROVIDER=dev-ag2space\n' \
  > "$FIXTURES/mxid/dev-ag2space/.env"
got="$(derive GATEWAY_CHANNELS_DIR="$FIXTURES/mxid" AG2_REMOTE_TOKEN_LOCAL=x)"
if [ "$got" = ":localhost" ]; then
  ok "AGENT_MXID domain outranks the name convention (:localhost)"
else
  bad "AGENT_MXID domain outranks the name convention (:localhost)" "got '$got'"
fi

# A lane .env WITHOUT an AGENT_MXID keeps the conventional suffix (pre-identity
# lanes must fence exactly as before this change).
got="$(derive GATEWAY_CHANNELS_DIR="$FIXTURES/mxid" AG2_REMOTE_TOKEN_DEV=x)"
if [ "$got" = ":dev.ag2.space" ]; then
  ok "lane .env without AGENT_MXID falls back to the convention"
else
  bad "lane .env without AGENT_MXID falls back to the convention" "got '$got'"
fi

# Mixed fleet: one identity-bearing lane + one conventional lane, both fenced.
got="$(derive GATEWAY_CHANNELS_DIR="$FIXTURES/mxid" AG2_REMOTE_TOKEN_DEV=x AG2_REMOTE_TOKEN_LOCAL=y)"
case "$got" in
  *":dev.ag2.space"*":localhost"*|*":localhost"*":dev.ag2.space"*)
    ok "mixed fleet derives both the mxid and conventional suffixes" ;;
  *)
    bad "mixed fleet derives both the mxid and conventional suffixes" "got '$got'" ;;
esac

# A malformed AGENT_MXID (no colon) cannot produce an empty suffix.
mkdir -p "$FIXTURES/bad/dev-ag2space"
printf 'AGENT_MXID=not-an-mxid\n' > "$FIXTURES/bad/dev-ag2space/.env"
got="$(derive GATEWAY_CHANNELS_DIR="$FIXTURES/bad" AG2_REMOTE_TOKEN_DEV=x)"
if [ "$got" = ":dev.ag2.space" ]; then
  ok "malformed AGENT_MXID falls back to the convention"
else
  bad "malformed AGENT_MXID falls back to the convention" "got '$got'"
fi

# --- wiring: BOTH launch paths carry the derived value ----------------------

# Assert the claim, not a file-wide count: an `== 1` over the whole file is
# stronger than the intent and fails on any second legitimate injection site.
named_lane_block="$(awk '/GATEWAY_INSTANCE="\$_gw_inst"/,/remote-gateway-bridge\.py/' \
  "$REPO/src/startup-runtime.sh")"
if printf '%s' "$named_lane_block" | grep -q 'GATEWAY_FOREIGN_SUFFIXES'; then
  bad "the named-lane spawn does not inject the foreign list" "found it in the lane block"
else
  ok "the named-lane spawn does not inject the foreign list"
fi

if grep -q 'GATEWAY_FOREIGN_SUFFIXES="\$(derive_foreign_suffixes)"' "$REPO/src/startup-runtime.sh"; then
  ok "the bare default-lane spawn injects the derived value"
else
  bad "the bare default-lane spawn injects the derived value" "no injection site found"
fi

# The supervised path is the PREFERRED one, so a fence only on the fallback
# leaves the same host behaving two ways depending on launchd health.
wrapper="$REPO/src/launchd/gateway-bridge-wrapper.sh"
if grep -q 'GATEWAY_FOREIGN_SUFFIXES="\$(derive_foreign_suffixes)"' "$wrapper" \
   && grep -q 'export GATEWAY_FOREIGN_SUFFIXES' "$wrapper"; then
  ok "the launchd wrapper derives and exports the value (supervised path)"
else
  bad "the launchd wrapper derives and exports the value (supervised path)" "not wired"
fi

# One derivation, not two copies that can drift.
defs="$(grep -rl 'derive_foreign_suffixes() {' "$REPO/src" | wc -l | tr -d ' ')"
if [ "$defs" = 1 ]; then
  ok "exactly one definition of derive_foreign_suffixes in src/"
else
  bad "exactly one definition of derive_foreign_suffixes in src/" "found $defs"
fi

# --- negative control: the harness can fail ---------------------------------
got="$(derive AG2_REMOTE_TOKEN_DEV=x)"
if [ "$got" = ":wrong.example" ]; then
  bad "negative control" "harness cannot fail — ':wrong.example' matched"
else
  ok "negative control: a wrong expectation would be caught"
fi

echo
if [ "$fails" -eq 0 ]; then
  echo "ALL PASS"
  exit 0
else
  echo "$fails FAILURE(S)"
  exit 1
fi
