#!/bin/bash
# The harness exists to certify that a restart really happened, so every gate it
# claims must be pinned here — a green artifact from a restart that never occurred
# is worse than no artifact.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
H="$REPO/scripts/restart-test-harness.sh"

pass=0; fail=0
ok()  { echo "ok   $1"; pass=$((pass+1)); }
bad() { echo "FAIL $1"; fail=$((fail+1)); }

TMPD="$(mktemp -d -t restart-harness.XXXXXX)"
trap 'rm -rf "$TMPD"' EXIT

# An identity that changes on every call — a real restart's pid/start-time.
CHANGING="$TMPD/changing.sh"
cat > "$CHANGING" <<EOF
#!/bin/bash
n=\$(cat "$TMPD/counter" 2>/dev/null || echo 0)
n=\$((n+1)); echo "\$n" > "$TMPD/counter"; echo "pid-\$n"
EOF
chmod +x "$CHANGING"

expect() { # expect <desc> <want-rc> <args...>
    local desc="$1" want="$2"; shift 2
    local out rc
    out="$("$@" 2>&1)"; rc=$?
    if [ "$rc" -eq "$want" ]; then ok "$desc (rc=$rc)"
    else bad "$desc — want rc=$want got rc=$rc"; echo "$out" | tail -3 | sed 's/^/      /'; fi
}

# --- the gate this whole PR turns on ------------------------------------------
# A restart command can return 0 while the old process keeps answering; the
# probes then pass and the run would certify a restart that never happened.
expect "no-op restart (identity unchanged) fails closed" 1 \
    bash "$H" --start true --restart true --probe true --identity "echo same" --settle 0

expect "real restart (identity changes) passes" 0 \
    bash "$H" --start true --restart true --probe true --identity "$CHANGING" --settle 0

# --- identity must actually yield a witness -----------------------------------
expect "identity command failing pre-restart fails closed" 1 \
    bash "$H" --start true --restart true --probe true --identity false --settle 0

expect "identity printing nothing fails closed" 1 \
    bash "$H" --start true --restart true --probe true --identity "true" --settle 0

# --- every stage gate ---------------------------------------------------------
expect "nonzero start fails closed" 1 \
    bash "$H" --start false --restart true --probe true --identity "$CHANGING" --settle 0

expect "failing baseline probe fails closed" 1 \
    bash "$H" --start true --restart true --probe false --identity "$CHANGING" --settle 0

expect "nonzero restart fails closed" 1 \
    bash "$H" --start true --restart false --probe true --identity "$CHANGING" --settle 0

# Probe healthy before, broken after — the post-restart regression this catches.
PROBE_ONCE="$TMPD/probe-once.sh"
cat > "$PROBE_ONCE" <<EOF
#!/bin/bash
if [ -f "$TMPD/probed" ]; then exit 1; fi
touch "$TMPD/probed"; exit 0
EOF
chmod +x "$PROBE_ONCE"
expect "probe that breaks after restart fails closed" 1 \
    bash "$H" --start true --restart true --probe "$PROBE_ONCE" --identity "$CHANGING" --settle 0

expect "readiness timeout fails closed" 1 \
    bash "$H" --start true --restart true --probe true --identity "$CHANGING" \
         --ready false --ready-timeout 2 --settle 0

# --- argument contract --------------------------------------------------------
expect "missing --identity is a usage error" 2 \
    bash "$H" --start true --restart true --probe true --settle 0

expect "unknown argument is a usage error" 2 \
    bash "$H" --start true --restart true --probe true --identity "echo x" --bogus y

expect "bad --target fails closed" 1 \
    bash "$H" --start true --restart true --probe true --identity "echo x" \
         --target nonsense --settle 0

# --- ssh option construction (stub ssh; never touches a network) --------------
STUB="$TMPD/bin"; mkdir -p "$STUB"
cat > "$STUB/ssh" <<EOF
#!/bin/bash
echo "\$@" >> "$TMPD/ssh-args"
exit 0
EOF
chmod +x "$STUB/ssh"

: > "$TMPD/ssh-args"
PATH="$STUB:$PATH" bash "$H" --start true --restart true --probe true \
    --identity "echo x" --target ssh:user@host --known-hosts "$TMPD/kh" --settle 0 >/dev/null 2>&1
if grep -q -- "StrictHostKeyChecking=accept-new" "$TMPD/ssh-args" \
   && grep -q -- "UserKnownHostsFile=$TMPD/kh" "$TMPD/ssh-args"; then
    ok "--known-hosts pins a disposable file with accept-new"
else
    bad "--known-hosts did not produce the pinned ssh options"
fi

: > "$TMPD/ssh-args"
PATH="$STUB:$PATH" bash "$H" --start true --restart true --probe true \
    --identity "echo x" --target ssh:user@host --settle 0 >/dev/null 2>&1
if grep -q -- "StrictHostKeyChecking" "$TMPD/ssh-args"; then
    bad "without --known-hosts the harness overrode host verification"
else
    ok "without --known-hosts host verification is left to ssh's own config"
fi
# Host verification must never be turned off, with or without --known-hosts.
if grep -qE "StrictHostKeyChecking=(no|off)" "$TMPD/ssh-args"; then
    bad "host key checking was disabled"
else
    ok "host key checking is never disabled"
fi

echo
echo "passed=$pass failed=$fail"
[ "$fail" -eq 0 ]
