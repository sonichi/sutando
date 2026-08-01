#!/usr/bin/env bash
# Tests REVIEW.md lesson 7's machine-readable half: the Xcode-CLT stub patterns
# in `checks.hardcoded-paths.flag_exact`.
#
# On macOS /usr/bin/{git,python3,swift,swiftc,clang,gcc,make} are one inode
# hardlinked as the CLT stub. Invoking one on a host without developer tools
# raises a modal install dialog and returns nothing, and the absolute path
# cannot be shadowed by a real install on PATH. #2469 and #2473 fixed live
# instances; this pins the gate that stops them coming back.
#
# Runs the REAL scripts/review-checks.sh against the REAL REVIEW.md, so it
# covers the whole chain — guide parsing, flag list, scanner — not just the
# scanner in isolation. Three properties:
#   1. every stub path is caught in executable code,
#   2. a comment that merely MENTIONS a stub path is not (this repo discusses
#      these paths constantly in prose — a check that flagged comments would be
#      turned off within a week),
#   3. genuinely-real /usr/bin binaries are never flagged — including
#      prefix-family siblings like swift-inspect, which a substring rule
#      wrongly rejected (#2474 review).
#
# Run: bash tests/review-checks-clt-shims.test.sh
set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNNER="$REPO/scripts/review-checks.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

pass=0
fail=0

ok() {
    if [ "$2" = "yes" ]; then
        echo "ok   $1"
        pass=$((pass + 1))
    else
        echo "FAIL $1"
        fail=$((fail + 1))
    fi
}

# scan <diff-body> -> writes scanner stdout to $OUT, sets $RC
scan() {
    {
        echo "diff --git a/src/probe.py b/src/probe.py"
        echo "--- a/src/probe.py"
        echo "+++ b/src/probe.py"
        echo "@@ -1 +1,40 @@"
        printf '%s\n' "$1"
    } > "$TMP/in.diff"
    # 2>&1: the runner reports violations on stderr and only the summary on
    # stdout, so a stdout-only capture looks empty on a real hit.
    OUT="$(bash "$RUNNER" --diff "$TMP/in.diff" 2>&1)"
    RC=$?
}

# --- 1. every stub path is caught in executable code -------------------------
# Each stub is named exactly under flag_exact — swiftc no longer rides on a
# '/usr/bin/swift' prefix, because that prefix also rejected the real
# swift-inspect binary (see 2b).
for stub in git python3 swift swiftc clang gcc make; do
    scan "+    subprocess.run([\"/usr/bin/$stub\", \"--version\"])"
    if [ "$RC" -eq 1 ] && printf '%s' "$OUT" | grep -q "/usr/bin/$stub"; then
        ok "flags /usr/bin/$stub in executable code" yes
    else
        ok "flags /usr/bin/$stub in executable code" "no (rc=$RC out=$OUT)"
    fi
done

# --- 2. prose mentions are not flagged ---------------------------------------
scan "+    # startup.sh launches this via bare python3, which is /usr/bin/python3 (3.9)
+    // where neither /usr/bin/python3 nor a brewed python had PyYAML"
if [ "$RC" -eq 0 ]; then
    ok "comment mentions of a stub path are not flagged" yes
else
    ok "comment mentions of a stub path are not flagged" "no (rc=$RC out=$OUT)"
fi

# --- 2b. prefix-family siblings are NOT flagged ------------------------------
# The stub entries are whole-token matches (`flag_exact`), not substrings.
# /usr/bin/swift-inspect is a REAL separate binary — its own inode, link count
# 1 — while /usr/bin/swift and /usr/bin/swiftc share the stub inode with 76
# other names. A substring rule rejected the real tool too, which is how a
# mandatory gate gets disabled rather than fixed (#2474 review, john-the-dev).
# makeinfo is the same shape against /usr/bin/make; it is absent on the current
# host, so it is pinned here as a synthetic control rather than left to chance.
for sibling in swift-inspect swift-frontend makeinfo gcc-14; do
    scan "+    p = \"/usr/bin/$sibling\""
    if [ "$RC" -eq 0 ]; then
        ok "prefix-family sibling /usr/bin/$sibling is not flagged" yes
    else
        ok "prefix-family sibling /usr/bin/$sibling is not flagged" "no (rc=$RC out=$OUT)"
    fi
done

# --- 3. real /usr/bin binaries stay usable -----------------------------------
# These are NOT stubs (separate inodes, link count 1) and the codebase addresses
# them absolutely on purpose — main.swift spawns pgrep/lsof/osascript that way.
# Flagging them would make the gate unusable.
scan "+    proc.executableURL = URL(fileURLWithPath: \"/usr/bin/pgrep\")
+    proc.executableURL = URL(fileURLWithPath: \"/usr/bin/lsof\")
+    task.launchPath = \"/usr/bin/osascript\"
+    proc.executableURL = URL(fileURLWithPath: \"/usr/bin/xcode-select\")
+    task.launchPath = \"/usr/bin/open\"
+    p = \"/usr/bin/id\"
+    q = \"/usr/bin/pmset\""
if [ "$RC" -eq 0 ]; then
    ok "non-stub /usr/bin binaries are not flagged" yes
else
    ok "non-stub /usr/bin binaries are not flagged" "no (rc=$RC out=$OUT)"
fi

# --- 4. the guide actually carries the patterns -------------------------------
# Guards against someone deleting the flag entries while leaving lesson 7's
# prose in place — the prose alone is not enforcement.
missing=""
for pat in '/usr/bin/git' '/usr/bin/python3' '/usr/bin/swift' '/usr/bin/swiftc' '/usr/bin/clang' '/usr/bin/gcc' '/usr/bin/make'; do
    grep -q "'$pat'" "$REPO/REVIEW.md" || missing="$missing $pat"
done
if [ -z "$missing" ]; then
    ok "REVIEW.md checks: block lists every stub pattern" yes
else
    ok "REVIEW.md checks: block lists every stub pattern" "no (missing:$missing)"
fi

# The stubs must live under flag_exact, not flag — under `flag` they are
# substrings again and the prefix-family controls above regress silently.
if awk '/^[[:space:]]*flag_exact:/{f=1} f && /usr\/bin\/swiftc/{found=1} END{exit !found}' "$REPO/REVIEW.md"; then
    ok "stub patterns live under flag_exact (whole-token), not flag" yes
else
    ok "stub patterns live under flag_exact (whole-token), not flag" "no"
fi

echo
echo "passed=$pass failed=$fail"
[ "$fail" -eq 0 ] || exit 1
