#!/bin/bash
# The wrapper's interpreter probe must measure the INTERPRETER, not the network.
# A network-based probe reports "no usable Python interpreter" on an outage and
# exit 1s on a fast loop -- the launchd deferral state the wrapper exists to avoid.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
WRAPPER="$REPO/src/launchd/channel-bridge-wrapper.sh"
[ -f "$WRAPPER" ] || { echo "SKIP: wrapper not found"; exit 0; }
command -v python3 >/dev/null 2>&1 || { echo "SKIP: python3 not found"; exit 0; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
fails=0
say() { if [ "$1" = ok ]; then echo "  ok: $2"; else echo "  FAIL: $2"; fails=1; fi; }

# A successful probe lets the wrapper go resident, so every run needs a bound.
# BSD/macOS has neither timeout(1) nor gtimeout(1) by default; fall back to a
# watchdog so this suite runs for a developer, not only on the CI runner.
_bound() { # _bound <secs> <outfile> <cmd...>
  local secs="$1" out="$2"; shift 2
  if command -v timeout >/dev/null 2>&1; then timeout "$secs" "$@" > "$out" 2>&1; return $?
  elif command -v gtimeout >/dev/null 2>&1; then gtimeout "$secs" "$@" > "$out" 2>&1; return $?
  fi
  "$@" > "$out" 2>&1 & local p=$!
  ( sleep "$secs"; kill -TERM "$p" 2>/dev/null ) >/dev/null 2>&1 & local k=$!
  wait "$p" 2>/dev/null; local rc=$?
  kill "$k" 2>/dev/null; wait "$k" 2>/dev/null
  return "$rc"
}

# A shadow python3 that records argv and decides by what it was asked to run.
# mode=all-fail        -> every invocation fails (probe must be seen to gate)
# mode=no-network      -> plain imports succeed; anything touching the network fails
make_stub() {
  mkdir -p "$TMP/bin"
  cat > "$TMP/bin/python3" <<STUB
#!/bin/bash
printf '%s\n' "\$*" >> "$TMP/argv.log"
case "\$MODE" in
  all-fail) exit 1 ;;
  no-network)
    case "\$*" in
      *urlopen*|*http*) exit 1 ;;
      *) exit 0 ;;
    esac ;;
esac
exit 0
STUB
  chmod +x "$TMP/bin/python3"
}
make_stub

cd "$REPO" || exit 1

echo "1. probe is load-bearing: an interpreter that fails every check must gate"
: > "$TMP/argv.log"
MODE=all-fail TELEGRAM_BOT_TOKEN=x PATH="$TMP/bin:$PATH" \
  _bound 20 "$TMP/out1" bash "$WRAPPER" telegram; rc=$?
if grep -q 'no usable Python interpreter' "$TMP/out1" && [ "$rc" = 1 ]; then
  say ok "wrapper exits 1 with the interpreter diagnosis when python3 truly fails"
else
  say FAIL "expected exit 1 + 'no usable Python interpreter', got rc=$rc: $(tail -1 "$TMP/out1")"
fi

echo "2. REGRESSION: a network outage must NOT be reported as a missing interpreter"
: > "$TMP/argv.log"
MODE=no-network TELEGRAM_BOT_TOKEN=x PATH="$TMP/bin:$PATH" \
  _bound 20 "$TMP/out2" bash "$WRAPPER" telegram; rc2=$?
if grep -q 'no usable Python interpreter' "$TMP/out2"; then
  say FAIL "network-only failure was reported as 'no usable Python interpreter' (rc=$rc2) — probe is measuring the network"
else
  say ok "network-only failure does not produce the interpreter diagnosis"
fi

echo "3. the probe invocation itself names a module, not a URL"
probe="$(head -1 "$TMP/argv.log" 2>/dev/null || true)"
if [ -z "$probe" ]; then
  say FAIL "no python3 invocation recorded — the probe never ran"
elif printf '%s' "$probe" | grep -qE 'urlopen|https?://'; then
  say FAIL "probe reaches the network: $probe"
elif printf '%s' "$probe" | grep -q 'import '; then
  say ok "probe is a local import: $probe"
else
  say FAIL "unrecognised probe form: $probe"
fi

# The vault token gate runs BEFORE the bridge launches, so it must use the SAME
# interpreter decision -- never a bare `command -v python3`. An unvalidated PATH
# python3 can be the Xcode Command Line Tools stub, which prompts an install
# dialog when executed; the gate must not be what discovers that.
mkdir -p "$TMP/ovr"
cat > "$TMP/ovr/python3" <<OVR
#!/bin/bash
printf '%s\n' "\$*" >> "$TMP/ovr.log"
exit 3   # 3 = token definitively absent, the resolver's documented "no" answer
OVR
chmod +x "$TMP/ovr/python3"

echo "4. explicit override with a BROKEN PATH python3: the gate uses the override"
: > "$TMP/argv.log"; : > "$TMP/ovr.log"
MODE=all-fail SUTANDO_CHANNEL_BRIDGE_PYTHON="$TMP/ovr/python3" PATH="$TMP/bin:$PATH" \
  _bound 20 "$TMP/out4" bash "$WRAPPER" telegram >/dev/null 2>&1
if ! grep -q 'channel_token.py' "$TMP/ovr.log" 2>/dev/null; then
  say FAIL "the override was never used for the token gate"
elif grep -q 'channel_token.py' "$TMP/argv.log" 2>/dev/null; then
  say FAIL "the BROKEN PATH python3 was invoked as the vault gate despite an override"
else
  say ok "gate ran on the override; broken PATH python3 never invoked"
fi

echo "5. no override + UNVALIDATED PATH python3: it must not be invoked as the gate"
: > "$TMP/argv.log"; : > "$TMP/ovr.log"
MODE=all-fail PATH="$TMP/bin:$PATH" \
  _bound 20 "$TMP/out5" bash "$WRAPPER" telegram >/dev/null 2>&1
# Order matters: check the DEFECT first. On the pre-fix wrapper the gate ran and
# the wrapper then parked without ever reaching the probe, so an import-first
# assertion reports "probe never ran" and misnames the cause it just caught.
if grep -q 'channel_token.py' "$TMP/argv.log" 2>/dev/null; then
  say FAIL "a python3 that FAILS the module probe was invoked as the vault gate"
elif ! grep -q 'import ' "$TMP/argv.log" 2>/dev/null; then
  say FAIL "the module probe never ran — cannot tell whether the gate was ordered after it"
else
  say ok "probe rejected PATH python3 and the gate never invoked it"
fi

echo "6. REGRESSION: the real macOS CLT-stub location must never be invoked (probe OR gate) when developer tools are absent"
# Unlike the shadow-python3 cases above (an arbitrary PATH entry standing in for
# "PATH has a python3"), this drives resolve_python() through its ACTUAL
# stub-rejection rule: python3 resolved at the real /usr/bin, with only
# `xcode-select` faked to report "not installed". That is the shape qingyun-wu's
# review flagged as still unhandled -- a `command -v python3` probe accepts
# /usr/bin/python3 unconditionally, which on a clean Mac IS the CLT stub, and
# merely running it (even just for the module import probe) raises the install
# dialog before it can fail.
if [ -x /usr/bin/python3 ] && [ "$(uname -s)" = Darwin ]; then
  fakexcode="$TMP/fakexcode"; mkdir -p "$fakexcode"
  printf '#!/bin/sh\nexit 2\n' > "$fakexcode/xcode-select"; chmod +x "$fakexcode/xcode-select"
  : > "$TMP/argv.log"
  TELEGRAM_BOT_TOKEN=x OSTYPE=darwin24 PATH="$fakexcode:/usr/bin:/bin" \
    _bound 20 "$TMP/out6" bash "$WRAPPER" telegram; rc6=$?
  if [ -s "$TMP/argv.log" ]; then
    say FAIL "something was logged to the shadow-python argv log during case 6 (should be untouched)"
  elif grep -q 'no usable Python interpreter' "$TMP/out6" && [ "$rc6" = 1 ]; then
    say ok "resolve_python() refused the real /usr/bin/python3 with no CLT; wrapper never invoked it for probe or gate"
  else
    say FAIL "expected exit 1 + 'no usable Python interpreter' with a faked no-CLT signal, got rc=$rc6: $(tail -1 "$TMP/out6")"
  fi
else
  say ok "SKIP: no real /usr/bin/python3 on this platform — case 6 is macOS-only"
fi

[ "$fails" = 0 ] && echo "ALL PASSED" || echo "FAILED: $fails"
exit "$fails"
