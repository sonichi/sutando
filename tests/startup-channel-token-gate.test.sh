#!/usr/bin/env bash
# The startup gate itself, not just the helper it calls.
#
# @john-the-dev on #2638: "The focused suite only calls ct.main() directly, so
# its passing empty-value control never exercises the surrounding `|| grep`."
# That was exactly right — the helper refused an empty token while the shell
# around it said yes. So this file drives the BOOLEAN, and the two startup
# hazards that only exist in shell:
#
#   * a `. "$file"` on a path that need not exist any more, under `set -e`,
#     aborts ALL of startup to launch an OPTIONAL bridge
#   * a bare `python3` can be the Xcode-CLT stub, which raises a modal that no
#     exit-code check can suppress — startup resolves `$PY` for this reason
#
# Run: bash tests/startup-channel-token-gate.test.sh
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PY:-/usr/bin/python3}"
fails=0

ok()   { printf '  ok   %s\n' "$1"; }
bad()  { printf '  FAIL %s %s\n' "$1" "${2:-}"; fails=$((fails+1)); }
check(){ if [ "$1" = "0" ]; then ok "$2"; else bad "$2" "${3:-}"; fi }

echo "startup channel-token gate:"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# The gate shape, verbatim in behaviour: 0 = start, 3 = definitive no (NO
# fallback), anything else = resolver could not run, fall back to the old grep.
gate() {  # $1 = var, $2 = env file, $3 = resolver path
  local _rc=0
  "$PY" "$3" --has "$1" --env-file "$2" 2>/dev/null || _rc=$?
  if   [ "$_rc" -eq 0 ]; then return 0
  elif [ "$_rc" -eq 3 ]; then return 1
  else [ -f "$2" ] && grep -q "$1=" "$2" 2>/dev/null; fi
}

RESOLVER="$REPO/src/channel_token.py"

# --- the regression john reproduced: empty value must NOT start a bridge -----
printf 'FAKE_TOKEN_XYZ=\n' > "$TMP/empty.env"
if gate FAKE_TOKEN_XYZ "$TMP/empty.env" "$RESOLVER"; then
  bad "an empty \`VAR=\` does not open the gate" "GATE_PASSES_EMPTY_TOKEN"
else
  ok "an empty \`VAR=\` does not open the gate"
fi

printf 'FAKE_TOKEN_XYZ=real-looking-value\n' > "$TMP/good.env"
if gate FAKE_TOKEN_XYZ "$TMP/good.env" "$RESOLVER"; then
  ok "POSITIVE CONTROL — a real value opens the gate"
else
  bad "POSITIVE CONTROL — a real value opens the gate" "gate refused a usable token"
fi

# --- a BROKEN resolver must degrade, not refuse every bridge ----------------
cp "$RESOLVER" "$TMP/broken.py"; printf '\ndef f(:\n' >> "$TMP/broken.py"
if gate FAKE_TOKEN_XYZ "$TMP/good.env" "$TMP/broken.py"; then
  ok "a broken resolver falls back to the old grep (host keeps working)"
else
  bad "a broken resolver falls back to the old grep" "all bridges would be skipped"
fi

# --- the `set -e` abort: sourcing a file the gate no longer requires --------
# A vault-only token opens the gate with no .env present. If the source is
# unguarded, startup dies here and every LATER service dies with it.
cat > "$TMP/unguarded.sh" <<'EOS'
set -e
_SL_ENV="/nonexistent/slack.env"
set -a; . "$_SL_ENV"; set +a
echo REACHED_END
EOS
out="$(bash "$TMP/unguarded.sh" 2>/dev/null || true)"
check "$([ "$out" != "REACHED_END" ] && echo 0 || echo 1)" \
      "CONTROL — the unguarded source really does abort under set -e" \
      "expected abort, got: $out"

cat > "$TMP/guarded.sh" <<'EOS'
set -e
_SL_ENV="/nonexistent/slack.env"
if [ -f "$_SL_ENV" ]; then set -a; . "$_SL_ENV"; set +a; fi
echo REACHED_END
EOS
out="$(bash "$TMP/guarded.sh" 2>/dev/null || true)"
check "$([ "$out" = "REACHED_END" ] && echo 0 || echo 1)" \
      "the guarded form survives a missing .env (vault-only Slack)" \
      "startup would have aborted; got: $out"

# --- the real startup.sh, asserted structurally -----------------------------
S="$REPO/src/startup.sh"
check "$(grep -c 'if \[ -f "\$_SL_ENV" \]; then set -a' "$S" >/dev/null && echo 0 || echo 1)" \
      "startup.sh guards the Slack .env source"
n_bare="$(grep -c '; python3 "\$REPO/src/channel_token.py"' "$S" || true)"
check "$([ "${n_bare:-0}" -eq 0 ] && echo 0 || echo 1)" \
      "no gate invokes a bare \`python3\` (Xcode-CLT stub raises a modal)" \
      "found $n_bare"
n_py="$(grep -c '"\$PY" "\$REPO/src/channel_token.py"' "$S" || true)"
check "$([ "${n_py:-0}" -ge 3 ] && echo 0 || echo 1)" \
      "all three gates use the resolved \$PY" "found $n_py"
check "$(grep -q -- '--has SLACK_APP_TOKEN' "$S" && echo 0 || echo 1)" \
      "the Slack gate requires the APP token too (bot token alone cannot connect)"

echo
if [ "$fails" -ne 0 ]; then
  echo "FAILED ($fails)"
  exit 1
fi
echo "startup channel-token gate: all checks passed"
