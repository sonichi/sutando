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

# Resolve the interpreter the way startup.sh does (`startup.sh:32`), NOT a
# hardcoded /usr/bin/python3. On a Mac without Command Line Tools that path is
# the developer-tool STUB — verified on this host, where it shares an inode and
# link count with /usr/bin/git — and executing it raises a modal no exit-code
# check can suppress. A test that exists to forbid the bare interpreter must not
# invoke the stub itself. (@john-the-dev on #2638: "the green scanner is not
# evidence that the added absolute tool path is safe.")
. "$REPO/scripts/python-binary.sh"
PY="${PY:-$(resolve_python "$REPO")}"
if [ -z "$PY" ]; then
  echo "  SKIP no safe interpreter resolved — install CLT or set PY=<path>" >&2
  exit 0
fi
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

# --- the REAL gateway predicate (src/startup-runtime.sh) --------------------
# keweichen on #3338: the gate hardcoded channels/ag2space/.env, ran a bare
# python3, and collapsed every resolver failure into "not configured". Drive
# gateway_lane_configured() itself with a stub resolver and a lane file.
GW="$TMP/gw"; mkdir -p "$GW/src" "$GW/scripts"
cp "$REPO/src/startup-runtime.sh" "$REPO/src/repo_root.sh" "$REPO/src/watcher_sentinel.sh" "$GW/src/"
printf '#!/bin/bash\necho "%s/lane.env"\n' "$GW" > "$GW/scripts/sutando-config.sh"
gw_gate() {  # $1 = stub resolver rc, $2 = lane file contents ('' = absent), rest = env
  local _rc="$1" _lane="$2"; shift 2
  rm -f "$GW/lane.env"; [ -n "$_lane" ] && printf '%s\n' "$_lane" > "$GW/lane.env"
  printf 'import sys; sys.exit(%s)\n' "$_rc" > "$GW/src/channel_token.py"
  ( export REPO="$GW" PY="$PY" REMOTE_TASK_TOKEN= AG2_REMOTE_TOKEN= "$@"
    # shellcheck disable=SC1090
    . "$GW/src/startup-runtime.sh"; gateway_lane_configured ) 2>/dev/null
}
check "$(gw_gate 0 '' && echo 0 || echo 1)" \
      "gateway: resolver says usable (device-only host, no lane file) -> configured"
check "$(gw_gate 3 'REMOTE_TASK_TOKEN=x' && echo 1 || echo 0)" \
      "gateway: resolver says definitively absent -> NOT configured, no grep fallback"
check "$(gw_gate 1 'REMOTE_TASK_TOKEN=usable' && echo 0 || echo 1)" \
      "gateway: broken resolver + usable lane file -> configured (degrades to the file)"
check "$(gw_gate 1 '' && echo 1 || echo 0)" \
      "gateway: broken resolver + no lane file -> NOT configured"
check "$(gw_gate 1 'REMOTE_TASK_TOKEN=' && echo 1 || echo 0)" \
      "gateway: broken resolver + empty lane value -> NOT configured"
check "$( ( export REPO="$GW" PY= REMOTE_TASK_TOKEN= AG2_REMOTE_TOKEN=
            printf 'REMOTE_TASK_TOKEN=usable\n' > "$GW/lane.env"
            . "$GW/src/startup-runtime.sh"; gateway_lane_configured ) 2>/dev/null && echo 0 || echo 1)" \
      "gateway: no resolved \$PY never runs a bare python3 — falls to the lane file"
check "$( ( export REPO="$GW" PY="$PY" REMOTE_TASK_CHANNEL_DIR=dev
            . "$GW/src/startup-runtime.sh"; gateway_lane_env_file >/dev/null; echo 0 ) )" \
      "gateway: the lane file is named through the resolver helper, not a literal"
SR="$REPO/src/startup-runtime.sh"
check "$(grep -q 'claude-home-path channels/ag2space/.env' "$SR" && echo 1 || echo 0)" \
      "startup-runtime.sh no longer resolves a hardcoded channels/ag2space/.env"
check "$(grep -qE '"\$\{PY:-python3\}"' "$SR" && echo 1 || echo 0)" \
      "startup-runtime.sh has no bare-python3 fallback in the gateway gate"
check "$(grep -q 'if gateway_lane_configured' "$SR" && echo 0 || echo 1)" \
      "start_gateway_lanes delegates to gateway_lane_configured"

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

# --- and this file must hold itself to the rule it enforces -----------------
# A guard that forbids the bare/stub interpreter while invoking it is not a
# guard. Asserted, not commented: the previous revision defaulted PY to
# /usr/bin/python3, which on a CLT-less Mac is the stub that raises the modal.
check "$(grep -qE ':-[/]usr/bin/python3' "${BASH_SOURCE[0]}" && echo 1 || echo 0)" \
      "PY does not DEFAULT to the hardcoded interpreter path (the reviewed defect)"
if [ -e /usr/bin/git ] && [ -e /usr/bin/python3 ]; then
  _i1="$(stat -f %i /usr/bin/python3 2>/dev/null || stat -c %i /usr/bin/python3 2>/dev/null)"
  _i2="$(stat -f %i /usr/bin/git 2>/dev/null || stat -c %i /usr/bin/git 2>/dev/null)"
  _ipy="$(stat -f %i "$PY" 2>/dev/null || stat -c %i "$PY" 2>/dev/null)"
  if [ "$_i1" = "$_i2" ]; then
    check "$([ "$_ipy" != "$_i1" ] && echo 0 || echo 1)" \
          "the resolved \$PY is not the CLT shim (shares an inode with /usr/bin/git here)" \
          "PY=$PY resolves to the shim"
  else
    ok "/usr/bin/python3 is not a shim on this host (nothing to avoid)"
  fi
fi

echo
if [ "$fails" -ne 0 ]; then
  echo "FAILED ($fails)"
  exit 1
fi
echo "startup channel-token gate: all checks passed"
