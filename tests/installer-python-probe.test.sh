#!/usr/bin/env bash
# The health-check launchd installer must pick an interpreter that can actually
# RUN health-check.py, not merely one that exists.
#
# 2026-08-03, measured on a live host: `resolve_python` returned
# /opt/homebrew/bin/python3, which was 3.14.5 with a pyexpat/libexpat symbol
# mismatch — `import plistlib` died, so health-check could not start at all.
# resolve_python only asks whether an interpreter EXISTS, and the job it
# installs is the OS-level net that catches a wedged or dead session: the one
# component whose failure nothing else watches. It would have failed every 300s
# into a log nobody reads while `--status` still reported it loaded.
#
# `resolve_python_verified` wraps it with a probe. Note the fallback list is
# deliberately NOT extended with /usr/bin/python3: per REVIEW.md lesson 7 that
# is the Xcode-CLT stub, which exists whether or not the tools do.
#
# These cases drive the REAL resolve_python_verified with stub interpreters injected via
# $SUTANDO_PYTHON_CANDIDATES, so they assert the selection logic rather than
# whatever happens to be installed on the machine running the suite.
#
# Run: bash tests/installer-python-probe.test.sh   (exit 0 / 1)
set -u

REPO="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$REPO/src/install-health-check-launchd.sh"
fails=0

check() {
    if [ "$2" = "$3" ]; then
        printf '  ok   %s\n' "$1"
    else
        printf '  FAIL %s — got %s, want %s\n' "$1" "$2" "$3"
        fails=$((fails + 1))
    fi
}

box="$(mktemp -d)"
trap 'rm -rf "$box"' EXIT

# A "working" interpreter: succeeds at whatever it is asked to do.
mk_good() { printf '#!/bin/sh\nexit 0\n' > "$1"; chmod +x "$1"; }
# A "broken" one: exists and is executable, but fails the import probe — the
# exact shape of the real failure (dlopen at import time, not a missing binary).
mk_bad()  { printf '#!/bin/sh\necho "ImportError: pyexpat" >&2\nexit 1\n' > "$1"; chmod +x "$1"; }

mk_bad  "$box/broken1"
mk_bad  "$box/broken2"
mk_good "$box/good1"
mk_good "$box/good2"

# Extract probe_python AND resolve_python_verified and drive them directly.
# Both are needed: the resolver calls the probe, and an undefined probe would
# fail every candidate (127) — which looks exactly like 'every interpreter is
# broken' and would make these cases pass for the wrong reason. The script dispatches on "$1" at
# the bottom (no main-guard), so it cannot simply be sourced.
run_resolve() {
    SUTANDO_PYTHON_CANDIDATES="$1" REPO="$REPO" bash -c "
        REPO='$REPO'
        $(sed -n '/^probe_python()/,/^}$/p' "$SCRIPT")
        $(sed -n '/^probe_python()/,/^}$/p' "$SCRIPT")
    $(sed -n '/^resolve_python_verified()/,/^}$/p' "$SCRIPT")
        resolve_python_verified
    " 2>/dev/null
}

echo "installer python probe:"

# CONTROL FIRST: with a working interpreter at the front, the order is kept.
# Without this, 'skips the broken one' could pass merely because the function
# always returns the last candidate.
check "prefers the FIRST candidate when it works (order preserved)" \
      "$(run_resolve "$box/good1 $box/good2")" "$box/good1"

check "skips a broken candidate and takes the next working one" \
      "$(run_resolve "$box/broken1 $box/good1")" "$box/good1"

check "skips SEVERAL broken candidates" \
      "$(run_resolve "$box/broken1 $box/broken2 $box/good2")" "$box/good2"

# The real host shape: a broken preferred interpreter, a working one after it.
check "a broken preferred interpreter does not win over a working later one" \
      "$(run_resolve "$box/broken1 $box/good1 $box/good2")" "$box/good1"

# All broken: FAIL CLOSED. An earlier version warned and installed with the
# broken interpreter anyway. That is backwards — the caller runs
# bootout_if_loaded AFTER this resolves, so proceeding unloads a possibly-working
# job and replaces it with one proven unable to start, while reporting success.
# Nothing else watches this job, so a successful-looking dead install is the
# worst outcome available. (Review-caught, qingyun-wu on #2582.)
allbroken_status=0
SUTANDO_PYTHON_CANDIDATES="$box/broken1 $box/broken2" bash -c "
    REPO='$REPO'
    $(sed -n '/^probe_python()/,/^}$/p' "$SCRIPT")
    $(sed -n '/^resolve_python_verified()/,/^}$/p' "$SCRIPT")
    resolve_python_verified
" >/dev/null 2>&1 || allbroken_status=$?
check "all candidates broken -> exits NON-ZERO (installs nothing)" \
      "$([ "$allbroken_status" -ne 0 ] && echo nonzero || echo zero)" "nonzero"

allbroken_out=$(SUTANDO_PYTHON_CANDIDATES="$box/broken1 $box/broken2" bash -c "
    REPO='$REPO'
    $(sed -n '/^probe_python()/,/^}$/p' "$SCRIPT")
    $(sed -n '/^resolve_python_verified()/,/^}$/p' "$SCRIPT")
    resolve_python_verified
" 2>&1 >/dev/null)
check "all-broken failure prints no interpreter on stdout (nothing to install)" \
      "$(SUTANDO_PYTHON_CANDIDATES="$box/broken1 $box/broken2" bash -c "
          REPO='$REPO'
          $(sed -n '/^probe_python()/,/^}$/p' "$SCRIPT")
          $(sed -n '/^resolve_python_verified()/,/^}$/p' "$SCRIPT")
          resolve_python_verified
      " 2>/dev/null | tr -d '[:space:]')" ""

check "all-broken failure names the override so it is not a dead end" \
      "$(printf '%s' "$allbroken_out" | grep -c 'SUTANDO_PYTHON_CANDIDATES' | head -1)" "1"

# Non-existent paths are not candidates at all, and must not be reported as
# broken interpreters — that would send someone debugging a python that is
# simply not installed.
check "a non-existent path is skipped, not treated as broken" \
      "$(run_resolve "$box/does-not-exist $box/good1")" "$box/good1"

noise=$(SUTANDO_PYTHON_CANDIDATES="$box/does-not-exist $box/good1" bash -c "
    REPO='$REPO'
    $(sed -n '/^probe_python()/,/^}$/p' "$SCRIPT")
    $(sed -n '/^resolve_python_verified()/,/^}$/p' "$SCRIPT")
    resolve_python_verified
" 2>&1 >/dev/null | grep -c "does-not-exist")
check "a non-existent path produces no note" "$noise" "0"

# The same interpreter listed twice must be probed once.
dupes=$(SUTANDO_PYTHON_CANDIDATES="$box/broken1 $box/broken1 $box/good1" bash -c "
    REPO='$REPO'
    $(sed -n '/^probe_python()/,/^}$/p' "$SCRIPT")
    $(sed -n '/^resolve_python_verified()/,/^}$/p' "$SCRIPT")
    resolve_python_verified
" 2>&1 >/dev/null | grep -c "broken1")
check "a duplicate candidate is probed once, not twice" "$dupes" "1"

# STRUCTURAL: the interpreter must be resolved BEFORE bootout_if_loaded in the
# install path. That ordering is the whole reason failing closed is safe — it
# preserves any job already installed. A future reorder would silently turn a
# clean refusal back into "unload the working job, then fail".
# Asserted as "no bootout CALL precedes the resolution", not "a bootout follows
# it". The follows-it form is what I wrote first and it cannot fail: inserting a
# bootout above the resolution leaves the later one in place, so the check still
# saw a bootout after and passed. Verified by perturbation — moving bootout above
# the resolution must make this FAIL.
# The `^\s*bootout_if_loaded\s*$` shape matches CALLS only; the definition line
# is `bootout_if_loaded() {` and is excluded.
py_line=$(grep -n 'PYTHON_BIN="$(resolve_python_verified)"' "$SCRIPT" | head -1 | cut -d: -f1)
prior_bootout=$(grep -nE '^[[:space:]]*bootout_if_loaded[[:space:]]*$' "$SCRIPT" \
                | cut -d: -f1 | awk -v s="$py_line" '$1 < s {print; exit}')
check "no bootout_if_loaded runs BEFORE resolution (preserves an existing job)" \
      "$([ -n "$py_line" ] && [ -z "$prior_bootout" ] && echo yes || echo no)" "yes"

echo
if [ "$fails" -ne 0 ]; then
    echo "$fails check(s) FAILED"
    exit 1
fi
echo "all checks passed — the installer picks an interpreter that WORKS, in preference order"
