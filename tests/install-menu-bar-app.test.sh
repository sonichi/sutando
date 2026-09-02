#!/usr/bin/env bash
# shellcheck disable=SC2016  # stub bodies are written verbatim and expand when the
# stub RUNS, not when this file writes it; single quotes are required throughout.
# The installer's two dangerous paths, exercised against the real script.
#
# 1. Toolchain preflight. `command -v swiftc` passes against the Xcode-CLT stub
#    on a clean Mac (REVIEW.md lesson 7), so a bare swiftc then raises the system
#    install dialog instead of a diagnostic. The gate must be `xcode-select -p`,
#    the one probe that does not prompt, and it must run BEFORE swiftc.
#
# 2. Replacement scope. The Electron desktop app shares the executable NAME with
#    this menu-bar binary (#2038), so a name-scoped kill takes out the user's UI
#    and then reports success. Replacement must be scoped to this bundle's path.
#
# Runs the production script with a stubbed toolchain on PATH — not a copy of
# its logic — so the assertions bind to what actually ships.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$REPO/scripts/install-menu-bar-app.sh"
fail=0
pass() { echo "PASS: $1"; }
flunk() { echo "FAIL: $1"; fail=1; }

[ "$(uname -s)" = "Darwin" ] || { echo "SKIP: macOS-only installer"; exit 0; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"; [ -n "${DECOY:-}" ] && kill "$DECOY" 2>/dev/null; exit' EXIT

mkstub() {  # dir name body
    mkdir -p "$1"
    printf '#!/bin/sh\n%s\n' "$3" > "$1/$2"
    chmod +x "$1/$2"
}

# ---- 1. preflight: no developer tools -> refuse, and never reach swiftc ------
STUB="$WORK/stub-noclt"
mkstub "$STUB" xcode-select 'exit 1'
mkstub "$STUB" swiftc "touch '$WORK/SWIFTC_WAS_INVOKED'; exit 0"
out="$(PATH="$STUB:$PATH" bash "$SCRIPT" 2>&1)"; rc=$?

if [ "$rc" -ne 0 ]; then pass "refuses when xcode-select -p fails"
else flunk "refuses when xcode-select -p fails (exited $rc)"; fi

case "$out" in
    *"xcode-select --install"*) pass "names the actual remedy" ;;
    *) flunk "names the actual remedy (got: $out)" ;;
esac

# The whole point: the stub must never be invoked, because invoking it is what
# raises the install dialog on a clean Mac.
if [ -e "$WORK/SWIFTC_WAS_INVOKED" ]; then
    flunk "does NOT invoke swiftc when the toolchain is absent"
else
    pass "does NOT invoke swiftc when the toolchain is absent"
fi

# ---- 2. preflight: tools present but swiftc not runnable --------------------
STUB2="$WORK/stub-badswift"
mkstub "$STUB2" xcode-select 'exit 0'
mkstub "$STUB2" swiftc 'exit 1'
out2="$(PATH="$STUB2:$PATH" bash "$SCRIPT" 2>&1)"; rc2=$?
if [ "$rc2" -ne 0 ]; then pass "refuses when swiftc is present but not runnable"
else flunk "refuses when swiftc is present but not runnable (exited $rc2: $out2)"; fi

# ---- 3. replacement is path-scoped: a foreign 'Sutando' must SURVIVE --------
# Stand in for the Electron desktop app: same executable name, different bundle.
FOREIGN="$WORK/Applications/Sutando.app/Contents/MacOS"
mkdir -p "$FOREIGN"
# A COPY of a system binary is SIGKILLed by macOS (its signature no longer
# matches), so the stand-in has to be a script we own.
printf '#!/bin/sh\nexec sleep 600\n' > "$FOREIGN/Sutando"
chmod +x "$FOREIGN/Sutando"
"$FOREIGN/Sutando" &
DECOY=$!
sleep 0.3

if ! kill -0 "$DECOY" 2>/dev/null; then
    flunk "decoy setup (foreign Sutando did not start)"
else
    STUB3="$WORK/stub-build"
    mkstub "$STUB3" xcode-select 'exit 0'
    mkstub "$STUB3" swiftc 'while [ $# -gt 0 ]; do [ "$1" = "-o" ] && { shift; touch "$1"; }; shift; done; exit 0'
    mkstub "$STUB3" codesign 'exit 0'
    mkstub "$STUB3" open "touch '$WORK/OPEN_CALLED'; exit 0"
    PATH="$STUB3:$PATH" bash "$SCRIPT" --launch >/dev/null 2>&1

    if kill -0 "$DECOY" 2>/dev/null; then
        pass "a foreign Sutando process SURVIVES --launch (not name-scoped)"
    else
        flunk "a foreign Sutando process SURVIVES --launch (it was killed)"
    fi
    kill "$DECOY" 2>/dev/null
fi

# ---- 4. success is verified, not assumed -----------------------------------
# `open` succeeded but nothing is running, so the script must NOT claim launch.
STUB4="$WORK/stub-noproc"
mkstub "$STUB4" xcode-select 'exit 0'
mkstub "$STUB4" swiftc 'while [ $# -gt 0 ]; do [ "$1" = "-o" ] && { shift; touch "$1"; }; shift; done; exit 0'
mkstub "$STUB4" codesign 'exit 0'
mkstub "$STUB4" open 'exit 0'
out4="$(PATH="$STUB4:$PATH" bash "$SCRIPT" --launch 2>&1)"; rc4=$?
case "$out4" in
    *"✓ launched"*) flunk "never prints '✓ launched' when no process is running" ;;
    *) pass "never prints '✓ launched' when no process is running" ;;
esac
if [ "$rc4" -ne 0 ]; then pass "exits nonzero when the launch cannot be confirmed"
else flunk "exits nonzero when the launch cannot be confirmed (exited $rc4)"; fi

# ---- 4b. --supervise must not install a supervisor over a live unmanaged copy --
# launchd's copy would exit under the singleton guard, and KeepAlive does not
# restart a clean exit — so the supervisor supervises nothing.
# NOTE: do NOT drive --supervise here. It reaches the real
# src/install-sutando-app-launchd.sh, which writes a live LaunchAgent plist
# pointing at this worktree's temp paths. Asserted structurally instead.
# Structural: BOTH dispositions must call it, and the definition must exist once.
defs="$(grep -c '^stop_unmanaged()' "$SCRIPT")"
calls="$(grep -c '^  stop_unmanaged ||' "$SCRIPT")"
if [ "$defs" = "1" ] && [ "$calls" = "2" ]; then
    pass "one stop_unmanaged definition, called by both --supervise and --launch"
else
    flunk "one stop_unmanaged definition, called by both (defs=$defs calls=$calls)"
fi

# ---- 4bis. a FAILING codesign must not print a signed checkmark -------------
# Both signing arms used to end in `|| true` under an unconditional "✓ signed".
STUB_CS="$WORK/stub-codesign-fails"
mkstub "$STUB_CS" xcode-select 'exit 0'
mkstub "$STUB_CS" swiftc 'while [ $# -gt 0 ]; do [ "$1" = "-o" ] && { shift; touch "$1"; }; shift; done; exit 0'
mkstub "$STUB_CS" security 'exit 1'          # no "Sutando Dev" identity
mkstub "$STUB_CS" codesign 'exit 1'          # every signing attempt fails
mkstub "$STUB_CS" open 'exit 0'
out_cs="$(PATH="$STUB_CS:$PATH" bash "$SCRIPT" 2>&1)"; rc_cs=$?
case "$out_cs" in
    *"✓ signed"*) flunk "prints '✓ signed' when every codesign attempt failed" ;;
    *)            pass "never prints '✓ signed' when every codesign attempt failed" ;;
esac
case "$out_cs" in
    *UNSIGNED*) pass "names the bundle as UNSIGNED so the state is reported, not hidden" ;;
    *)          flunk "failing codesign did not report the unsigned state (out: ${out_cs%%$'\n'*})" ;;
esac
if [ "$rc_cs" -ne 0 ]; then
    pass "exits nonzero when the bundle could not be signed"
else
    flunk "exited 0 with an unsigned bundle (rc=$rc_cs)"
fi
# Control: with codesign succeeding, the SAME path must still report success —
# or the assertions above would pass simply by the script being broken.
STUB_OK="$WORK/stub-codesign-ok"
mkstub "$STUB_OK" xcode-select 'exit 0'
mkstub "$STUB_OK" swiftc 'while [ $# -gt 0 ]; do [ "$1" = "-o" ] && { shift; touch "$1"; }; shift; done; exit 0'
mkstub "$STUB_OK" security 'exit 1'
mkstub "$STUB_OK" codesign 'exit 0'
mkstub "$STUB_OK" open 'exit 0'
out_ok="$(PATH="$STUB_OK:$PATH" bash "$SCRIPT" 2>&1)"; rc_ok=$?
if [ "$rc_ok" -eq 0 ] && [ "${out_ok#*✓ signed}" != "$out_ok" ]; then
    pass "control: a SUCCEEDING codesign still reports signed and exits 0"
else
    flunk "control failed: succeeding codesign did not report signed (rc=$rc_ok)"
fi

# ---- 4b-ii. an unsigned bundle must not be launched OR supervised -----------
# The SIGNED report alone is not enough: the gate has to precede the side effect,
# or the app is already open / the LaunchAgent already installed when it fires.
for disp in --launch --supervise; do
    STUB_D="$WORK/stub-unsigned${disp}"
    mkstub "$STUB_D" xcode-select 'exit 0'
    mkstub "$STUB_D" swiftc 'while [ $# -gt 0 ]; do [ "$1" = "-o" ] && { shift; touch "$1"; }; shift; done; exit 0'
    mkstub "$STUB_D" security 'echo "  1) ABC \"Other\""; exit 0'
    mkstub "$STUB_D" codesign 'exit 1'
    mkstub "$STUB_D" open "touch '$WORK/OPENED$disp'; exit 0"
    mkstub "$STUB_D" launchctl 'exit 0'
    rm -f "$WORK/OPENED$disp"
    out_u="$(PATH="$STUB_D:$PATH" HOME="$WORK/home-unsigned" bash "$SCRIPT" "$disp" 2>&1)"; rc_u=$?
    if [ "$rc_u" -ne 0 ]; then
        pass "$disp with a failed codesign exits nonzero"
    else
        flunk "$disp with a failed codesign exited 0"
    fi
    if [ -f "$WORK/OPENED$disp" ]; then
        flunk "$disp OPENED an unsigned bundle before failing"
    else
        pass "$disp never reached the side effect on an unsigned bundle"
    fi
    case "$out_u" in
        *UNSIGNED*) pass "$disp names the unsigned state" ;;
        *)          flunk "$disp did not name the unsigned state" ;;
    esac
done
# Control: the SAME dispositions must still reach the side effect when signing works.
STUB_S="$WORK/stub-signed-launch"
mkstub "$STUB_S" xcode-select 'exit 0'
mkstub "$STUB_S" swiftc 'while [ $# -gt 0 ]; do [ "$1" = "-o" ] && { shift; touch "$1"; }; shift; done; exit 0'
mkstub "$STUB_S" security 'echo "  1) ABC \"Other\""; exit 0'
mkstub "$STUB_S" codesign 'exit 0'
mkstub "$STUB_S" open "touch '$WORK/OPENED_SIGNED'; exit 0"
rm -f "$WORK/OPENED_SIGNED"
PATH="$STUB_S:$PATH" bash "$SCRIPT" --launch >/dev/null 2>&1
if [ -f "$WORK/OPENED_SIGNED" ]; then
    pass "control: a SIGNED bundle still reaches --launch (the gate does not over-fire)"
else
    flunk "control failed: a signed bundle no longer reaches --launch"
fi

# ---- 4c. the probe must be LITERAL, not a regex over the checkout path ------
# `pgrep -f` matches an ERE, so a checkout path holding regex metacharacters
# makes the probe miss a live process and stop_unmanaged report "nothing to do".
METADIR="$WORK/ex[re]po.d"
mkdir -p "$METADIR"
ln -s /bin/sleep "$METADIR/Sutando" 2>/dev/null
"$METADIR/Sutando" 400 & META_PID=$!
sleep 0.5
if ! kill -0 "$META_PID" 2>/dev/null; then
    flunk "metacharacter-path fixture did not stay alive (fixture broken, not the probe)"
else
    APP_BIN="$METADIR/Sutando 400"
    # Drive the PRODUCTION function body, not a copy of its recipe.
    eval "$(sed -n '/^app_pids() {/,/^}/p' "$SCRIPT")"
    found="$(app_pids | tr -d '[:space:]')"
    pgrep -f "^$APP_BIN$" >/dev/null 2>&1; pg_rc=$?
    if [ "$found" = "$META_PID" ]; then
        pass "the probe finds a live process whose path holds regex metacharacters"
    else
        flunk "probe missed a live metacharacter-path process (got '$found', want $META_PID)"
    fi
    # Control: the old regex probe must actually FAIL here, or this proves nothing.
    if [ "$pg_rc" -ne 0 ]; then
        pass "control: the regex probe does miss it, so the literal compare is load-bearing"
    else
        flunk "control failed: pgrep -f also matched, so this fixture cannot detect the bug"
    fi
fi
kill "$META_PID" 2>/dev/null

# The guard must precede the installer call, or it cannot prevent anything.
gi="$(grep -n '^  stop_unmanaged ||' "$SCRIPT" | head -1 | cut -d: -f1)"
ii="$(grep -n 'bash "\$REPO/src/install-sutando-app-launchd.sh"' "$SCRIPT" | head -1 | cut -d: -f1)"
if [ -n "$gi" ] && [ -n "$ii" ] && [ "$gi" -lt "$ii" ]; then
    pass "the stop guard runs BEFORE the launchd installer"
else
    flunk "the stop guard runs BEFORE the launchd installer (guard=$gi installer=$ii)"
fi

# ---- 5. the retired probes must not come back -------------------------------
# Strip comments first: the script explains WHY each probe was retired and names
# it, so a whole-file grep matches the explanation and reports the bug it fixed.
code="$(grep -vE '^[[:space:]]*#' "$SCRIPT")"
case "$code" in
    *'pkill -x Sutando'*) flunk "no name-scoped 'pkill -x Sutando' in CODE" ;;
    *) pass "no name-scoped 'pkill -x Sutando' in CODE" ;;
esac
case "$code" in
    *'command -v swiftc'*) flunk "no 'command -v swiftc' in CODE (passes against the CLT stub)" ;;
    *) pass "no 'command -v swiftc' in CODE (passes against the CLT stub)" ;;
esac

if [ "$fail" -ne 0 ]; then
    echo "FAIL: install-menu-bar-app"
    exit 1
fi
echo "PASS: installer refuses without a real toolchain and replaces only its own app."
