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

[ "$fails" = 0 ] && echo "ALL PASSED" || echo "FAILED: $fails"
exit "$fails"
