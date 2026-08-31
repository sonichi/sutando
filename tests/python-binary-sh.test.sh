#!/usr/bin/env bash
# Contract test for scripts/python-binary.sh — the shell twin of
# src/python-binary.ts and src/git_binary.py.
#
# The rule being pinned: NEVER execute a candidate to decide whether it is
# usable. On a Mac without the Xcode Command Line Tools, /usr/bin/python3 is
# Apple's stub (one inode hardlinked across 78 names); executing it raises a
# modal install dialog BEFORE it can fail, so a probe like
#
#     "$candidate" -c "pass"
#
# is itself the bug. Only `xcode-select -p` is safe — /usr/bin/xcode-select is a
# real binary, so asking it never prompts.
#
# Run: bash tests/python-binary-sh.test.sh
# Exit: 0 = all pass, 1 = failure
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
pass=0; fail=0
ok()   { printf "  ok   %s\n" "$1"; pass=$((pass+1)); }
bad()  { printf "  FAIL %s — %s\n" "$1" "${2:-}"; fail=$((fail+1)); }
check(){ if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "expected [$3], got [$2]"; fi; }

# A sandbox whose PATH contains ONLY a fake system bin, so `command -v python3`
# finds our stand-in for the stub and nothing else.
mklab() {
  d=$(mktemp -d)
  mkdir -p "$d/bin"
  printf '#!/bin/sh\necho "STUB RAN" >> %s/stub-ran\nexit 1\n' "$d" > "$d/bin/python3"
  chmod +x "$d/bin/python3"
  printf '%s' "$d"
}

# --- 1. no developer tools -> refuses the system interpreter -----------------
lab=$(mklab)
printf '#!/bin/sh\nexit 2\n' > "$lab/bin/xcode-select"; chmod +x "$lab/bin/xcode-select"
# Make the fake python3 look like it lives in the system bin dir by pointing the
# resolver's directory comparison at our sandbox is not possible without editing
# it, so instead assert the REAL contract on the real system path below (test 4)
# and here assert the safe-probe behaviour with a non-system dir: a non-stub
# location must be accepted even with no toolchain.
out=$(PATH="$lab/bin:/bin" /bin/bash -c ". '$REPO/scripts/python-binary.sh'; resolve_python '$REPO'")
check "a NON-system python is used even without developer tools" "$out" "$lab/bin/python3"

# --- 2. it never EXECUTED the candidate to decide ----------------------------
if [ -f "$lab/stub-ran" ]; then
  bad "resolver must not execute a candidate to probe it" "$(cat "$lab/stub-ran")"
else
  ok "resolver never executed the candidate (the #1789 probe shape would have)"
fi

# --- 3. $SUTANDO_PY wins, and only when executable ---------------------------
lab2=$(mklab)
printf '#!/bin/sh\nexit 0\n' > "$lab2/explicit"; chmod +x "$lab2/explicit"
out=$(SUTANDO_PY="$lab2/explicit" PATH="$lab2/bin:/bin" /bin/bash -c ". '$REPO/scripts/python-binary.sh'; resolve_python '$REPO'")
check "\$SUTANDO_PY takes precedence" "$out" "$lab2/explicit"
out=$(SUTANDO_PY="$lab2/does-not-exist" PATH="$lab2/bin:/bin" /bin/bash -c ". '$REPO/scripts/python-binary.sh'; resolve_python '$REPO'")
check "a non-executable \$SUTANDO_PY is ignored" "$out" "$lab2/bin/python3"

# --- 4. the real contract: system dir + no CLT -> EMPTY ----------------------
# Uses the genuine system path, with only xcode-select faked to fail. This is
# the case the whole change exists for.
#
# OSTYPE is PINNED to darwin because this asserts a macOS-only contract. Without
# the pin the case ran against the HOST platform and failed on the Linux CI
# runner -- "expected [], got [/usr/bin/python3]" -- where returning the system
# interpreter is correct. A platform-specific assertion has to fix the platform,
# exactly like the code it tests.
lab3=$(mktemp -d)
printf '#!/bin/sh\nexit 2\n' > "$lab3/xcode-select"; chmod +x "$lab3/xcode-select"
out=$(OSTYPE=darwin25 PATH="$lab3:/usr/bin:/bin" /bin/bash -c ". '$REPO/scripts/python-binary.sh'; resolve_python '$REPO'")
check "macOS + system python + NO developer tools -> refuses (empty)" "$out" ""

# --- 5. ...and with the tools present it IS returned -------------------------
# Both halves faked (OSTYPE + a SUCCEEDING xcode-select) so this runs
# everywhere. It used to gate on the host's real xcode-select and printed
# "skip" on the Linux runner, meaning the tools-present branch — half the
# contract — was never exercised in CI. A skip that only ever fires on the
# machine that matters is not coverage.
lab5a=$(mktemp -d)
printf '#!/bin/sh\nexit 0\n' > "$lab5a/xcode-select"; chmod +x "$lab5a/xcode-select"
out=$(OSTYPE=darwin25 PATH="$lab5a:/usr/bin:/bin" /bin/bash -c ". '$REPO/scripts/python-binary.sh'; resolve_python '$REPO'")
if [ -n "$out" ]; then ok "macOS + system python + developer tools -> returned"
else bad "macOS + system python + developer tools -> returned" "got empty"; fi

# --- 5b. NON-Darwin: the stub rule must not apply --------------------------
# The rule is a macOS artifact. On Linux /usr/bin/python3 is an ordinary
# interpreter and xcode-select does not exist, so applying it everywhere
# returned EMPTY and every caller broke with
#   sutando-config.sh: line 56: : command not found
# This suite only ever ran on macOS, so CI caught it and the tests did not.
lab5=$(mktemp -d)
printf '#!/bin/sh\nexit 2\n' > "$lab5/xcode-select"; chmod +x "$lab5/xcode-select"
# Drive the platform through $OSTYPE, which is what the resolver now reads —
# see case 5c for why a stubbed `uname` must NOT be able to decide this.
out=$(OSTYPE=linux-gnu PATH="$lab5:/usr/bin:/bin" /bin/bash -c ". '$REPO/scripts/python-binary.sh'; resolve_python '$REPO'")
if [ -n "$out" ]; then ok "non-Darwin: system python is used (stub rule is macOS-only)"
else bad "non-Darwin: system python is used (stub rule is macOS-only)" "got empty — callers break"; fi

# --- 5c. platform identity must not come from a PATH lookup -----------------
# tests/codex-core-launcher.test.py:89 stubs `uname` to print "Darwin" for the
# launcher's own macOS branch. A uname-based platform check therefore took the
# macOS path on the Linux CI runner, found no xcode-select, and refused a real
# /usr/bin/python3 -- 21 failures in that suite. $OSTYPE is set by the shell,
# so a PATH stub cannot reach it.
lab6=$(mktemp -d)
printf '#!/bin/bash\nprintf "Darwin\\n"\n' > "$lab6/uname"; chmod +x "$lab6/uname"
printf '#!/bin/sh\nexit 2\n' > "$lab6/xcode-select"; chmod +x "$lab6/xcode-select"
out=$(OSTYPE=linux-gnu PATH="$lab6:/usr/bin:/bin" /bin/bash -c ". '$REPO/scripts/python-binary.sh'; resolve_python '$REPO'")
if [ -n "$out" ]; then ok "a stubbed uname cannot flip the platform (non-mac stays non-mac)"
else bad "a stubbed uname cannot flip the platform" "got empty — PATH spoofed the platform"; fi

out=$(OSTYPE=darwin25 PATH="$lab6:/usr/bin:/bin" /bin/bash -c ". '$REPO/scripts/python-binary.sh'; resolve_python '$REPO'")
check "control: a real Mac with no CLT still refuses the stub" "$out" ""

# --- 6. bundled runtime beats PATH ------------------------------------------
lab4=$(mklab)
mkdir -p "$lab4/engine/../runtime/python/bin"
printf '#!/bin/sh\nexit 0\n' > "$lab4/runtime/python/bin/python3"
chmod +x "$lab4/runtime/python/bin/python3"
mkdir -p "$lab4/engine"
out=$(PATH="$lab4/bin:/bin" /bin/bash -c ". '$REPO/scripts/python-binary.sh'; resolve_python '$lab4/engine'")
check "bundled <engine>/../runtime/python wins over PATH" "$out" "$lab4/engine/../runtime/python/bin/python3"

# --- 7. every caller that used to fall through to the bare name is routed ----
for f in src/startup.sh scripts/sutando-config.sh src/agent/claude/cli/start-cli.sh; do
  if grep -qE '^\s*PY="python3"\s*$' "$REPO/$f"; then
    bad "$f has no bare-name fallthrough" 'PY="python3" still present'
  else
    ok "$f has no bare-name fallthrough"
  fi
done


# --- 8. CALLER-LEVEL: no caller may degrade into `"" -c ...` ----------------
# Resolver unit coverage is not enough (CR #2599, @john-the-dev): the advertised
# "returns empty, callers skip" contract has to hold at the ACTIVATED entry
# points. Before this, sutando-config.sh exited 127 with the shell's opaque
#   scripts/sutando-config.sh: line 56: : command not found
# once per call site, and startup.sh promised services "will be skipped" while
# actually aborting.
noclt=$(mktemp -d)
printf '#!/bin/sh\nexit 2\n' > "$noclt/xcode-select"; chmod +x "$noclt/xcode-select"

out=$(env -u SUTANDO_PY PATH="$noclt:/usr/bin:/bin" /bin/bash "$REPO/scripts/sutando-config.sh" workspace 2>&1)
rc=$?
if [ "$rc" -eq 0 ]; then
  ok "sutando-config.sh: succeeds when a python IS resolvable"
else
  case "$out" in
    *"command not found"*) bad "sutando-config.sh fails ACTIONABLY, not with ': command not found'" "$out" ;;
    *"no runnable python3"*) ok "sutando-config.sh fails once, actionably (exit $rc)" ;;
    *) bad "sutando-config.sh fails actionably" "unexpected: $out" ;;
  esac
fi

# The startup message must not promise a skip the script does not perform.
# NOTE: the first version of this block asked `grep -q "skipped (no runnable
# python3)"` ONCE and then `break`, so a single skip branch anywhere satisfied
# it for every service in the list — vacuous. The guards below replace it
# with checks that are per-site and per-service.

# ── 10c. ACTIVATED: what a clean Mac actually gets from `bash src/startup.sh`.
# Every guard in this file above reads source. None of them drove startup
# through its own config seams, which is how the contradiction below survived
# three review rounds (CR #2599, @qingyun-wu):
#
#   startup.sh:57  _APP_NODE_DIR="$(bash scripts/sutando-config.sh app-node-dir)"
#   startup.sh:586 WORKSPACE="$(bash scripts/sutando-config.sh workspace)"
#
# Under `set -e` either one exiting non-zero terminates startup — at :57, right
# after it printed that services "will be skipped". So run the real script under
# a no-interpreter fixture and pin the OUTCOME, not the source.
#
# The fixture puts a fake `xcode-select` (exit 2 = no developer tools) ahead of
# a real /usr/bin on PATH, so `command -v python3` finds the genuine system
# python3 and the resolver must refuse it as the stub. `$HOME` is redirected so
# the run cannot touch the developer's real state.
startup_home=$(mktemp -d)
startup_out=$(cd "$REPO" && env -u SUTANDO_PY -u VIRTUAL_ENV -u CONDA_PREFIX \
  PATH="$noclt:/usr/bin:/bin" OSTYPE=darwin24 HOME="$startup_home" \
  bash src/startup.sh 2>&1); startup_rc=$?

if [ "$startup_rc" -ne 0 ]; then
  ok "startup exits non-zero when no interpreter resolves (rc=$startup_rc)"
else
  bad "startup exits non-zero when no interpreter resolves" "rc=0 — it claimed success"
fi

# It must not die with the shell's opaque error, nor claim it will continue.
case "$startup_out" in
  *"command not found"*)
    bad "startup fails with an actionable message" "opaque: $startup_out" ;;
  *"will be skipped"*)
    bad "startup does not promise to continue" "still promises a skip it cannot perform" ;;
  *"no runnable python3"*)
    ok "startup fails with an actionable message, not the shell's opaque error" ;;
  *)
    bad "startup fails with an actionable message" "unexpected: ${startup_out:-<empty>}" ;;
esac

# It must fail ONCE. The eager `require_python` fired this message per config
# lookup; the point of failing at the top is that the operator sees one line.
msg_count=$(printf '%s\n' "$startup_out" | grep -c "no runnable python3" || true)
if [ "$msg_count" -eq 1 ]; then
  ok "startup reports the missing interpreter exactly once"
else
  bad "startup reports the missing interpreter exactly once" "saw it $msg_count times"
fi

# And it must not have claimed ANY service started.
if printf '%s' "$startup_out" | grep -q '✓'; then
  bad "startup claims no service started" "printed: $(printf '%s' "$startup_out" | grep '✓' | tr '\n' ' ')"
else
  ok "startup claims no service started"
fi

# NOTE — a counting-the-execs case was drafted here and deliberately dropped: it
# is unfalsifiable on a machine that HAS the developer tools. To make the
# resolver see the system stub the fixture must put a real /usr/bin ahead of the
# shim on PATH, which shadows the shim, so it can never be reached and the case
# could only ever report zero. The exec invariant is already covered where it
# CAN fail — the resolver cases above run against `mklab`'s logging stand-in and
# have a working positive control ("a NON-system python is used even without
# developer tools" proves that lab python does get executed when it should).

# ── 11. A guarded background launch must not be followed by an unconditional ✓.
# CR #2599 (@qingyun-wu) P1: four sites read
#
#     [ -n "$PY" ] && "$PY" .../core_heartbeat.py > ... 2>&1 &
#     echo "  ✓ core heartbeat"
#
# The guard suppresses the launch; the echo runs regardless. On a no-CLT Mac
# startup then reports the heartbeat, services-status emitter, screen capture
# and gateway bridge as started with no process behind any of them — and the
# heartbeat/gateway pair are exactly the liveness + remote-control surfaces
# other hosts trust. Enumerate the shape rather than the four known lines, so a
# fifth site added later cannot reintroduce it silently.
bg_guards=$(grep -nE '^\s*\[ -n "\$PY" \] &&.*&\s*$' "$REPO/src/startup.sh" || true)
if [ -z "$bg_guards" ]; then
  ok "no backgrounded launch uses the '&& cmd &' guard (all use if/else)"
else
  bad "no backgrounded launch uses the '&& cmd &' guard" \
      "still present at lines: $(printf '%s' "$bg_guards" | cut -d: -f1 | tr '\n' ' ')"
fi

# ── 12. Every Python-backed service the reviewer named owns an explicit skip
# branch. Counted per service, so covering one does not vouch for the rest.
# "gateway bridge"'s skip line lives in start_gateway_lanes() in
# src/startup-runtime.sh (startup.sh's own inline gateway block moved there so
# it can also run standalone via scripts/restart-gateway-lanes.sh) — search
# both files rather than only startup.sh, since startup.sh still calls it.
missing=""
for svc in "core heartbeat" "services-status emitter" "screen capture" "gateway bridge"; do
  grep -qF "⊘ $svc skipped — no runnable python3" "$REPO/src/startup.sh" "$REPO/src/startup-runtime.sh" \
    || missing="${missing}[$svc] "
done
if [ -z "$missing" ]; then
  ok "each Python-backed service prints an explicit ⊘ skip when \$PY is empty"
else
  bad "each Python-backed service prints an explicit ⊘ skip" "no skip branch for: $missing"
fi

# ── 12b. The named secondary gateways, on the ACTIVATED path.
# CR #2599 (@qingyun-wu) found a fifth false-success site that guards 11 and 12
# both waved through, each for its own reason:
#   - guard 11 only rejects the old `&& cmd &` shape, and this loop had already
#     been converted to `if [ -n "$PY" ]; then … fi` — with the ✓ left OUTSIDE
#     the `fi`, which is the same defect wearing the fixed shape;
#   - guard 12 greps per service NAME, and every named instance's label starts
#     "gateway bridge", so the PRIMARY gateway's skip branch vouched for all of
#     them.
# A name-based scan cannot distinguish per-instance output, so run the loop
# instead of reading it. The block is lifted out of start_gateway_lanes() in
# src/startup-runtime.sh at test time (moved out of startup.sh's inline body
# so it can also run standalone via scripts/restart-gateway-lanes.sh) rather
# than copied here, so the test cannot drift from the source it pins.
gw_block=$(awk '/for _gw_var in /{f=1} f{print} f&&/^[[:space:]]*done[[:space:]]*$/{exit}' "$REPO/src/startup-runtime.sh")
if [ -z "$gw_block" ]; then
  bad "named-gateway loop is extractable from startup-runtime.sh" "no 'for _gw_var in' block found"
else
  gw_out=$(env -i PATH="/usr/bin:/bin" AG2_REMOTE_TOKEN_DEV=tok-dev \
    bash -c 'PY=""; REPO="'"$REPO"'"; LOGS_DIR="$(mktemp -d)"
'"$gw_block"'' 2>&1)
  # It must NOT claim a start, and it MUST name the instance it skipped.
  if printf '%s' "$gw_out" | grep -q '✓ gateway bridge (dev'; then
    bad "named gateway does not print ✓ when \$PY is empty" "printed: $gw_out"
  else
    ok "named gateway does not print ✓ when \$PY is empty"
  fi
  if printf '%s' "$gw_out" | grep -q '⊘ gateway bridge (dev'; then
    ok "named gateway names the instance it skipped"
  else
    bad "named gateway names the instance it skipped" "got: ${gw_out:-<no output>}"
  fi
fi

# ── 12c. General form of the same defect, so a SIXTH site cannot hide behind
# the corrected shape: a `[ -n "$PY" ]` guard that wraps a "$PY" launch must not
# be immediately followed by an unconditional success line. Depth-tracked so
# nested ifs inside the guard do not close it early.
stray=$(awk '
  /^[[:space:]]*if \[ -n "\$PY" \][[:space:]]*;[[:space:]]*then/ && !ing { ing=1; d=1; py=0; next }
  ing {
    if ($0 ~ /^[[:space:]]*if /) d++
    else if ($0 ~ /^[[:space:]]*fi[[:space:]]*$/) {
      d--
      if (d == 0) { ing=0; if (py) waiting=NR; next }
    }
    if ($0 ~ /"\$PY"/) py=1
    next
  }
  waiting && $0 ~ /^[[:space:]]*$/ { next }
  waiting && $0 ~ /^[[:space:]]*#/ { next }
  waiting {
    if ($0 ~ /echo .*✓/) print waiting ":" $0
    waiting=0
  }
' "$REPO/src/startup.sh")
if [ -z "$stray" ]; then
  ok "no \$PY guard is followed by an unconditional ✓ (general form)"
else
  bad "no \$PY guard is followed by an unconditional ✓" "after fi at line(s): $(printf '%s' "$stray" | cut -d: -f1 | tr '\n' ' ')"
fi

# ── 13. `${PY:-<fallback>}` may only fall back to a command that is a safe
# no-op. CR #2599 P2: `${PY:-cat} -c "import sys,json…"` ran `cat -c`, which is
# not a valid invocation of cat on BSD or GNU. It fails, `|| echo ""` swallows
# the error, and the operator gets a live ngrok tunnel with a stale
# WEBHOOK_BASE_URL — Twilio keeps posting to the previous session's URL. Only
# `false` is acceptable: it accepts any argv and exits non-zero.
# Comment-aware and token-wise: the first draft scanned raw lines and flagged
# the comment that *documents* the bug, and a line-wise filter would let a bad
# fallback hide on the same line as a good one.
badfb=$(awk '!/^[[:space:]]*#/ {
  line = $0
  while (match(line, /\$\{PY:-[^}]*\}/)) {
    tok = substr(line, RSTART, RLENGTH)
    if (tok != "${PY:-false}") print FNR ":" tok
    line = substr(line, RSTART + RLENGTH)
  }
}' "$REPO/src/startup.sh")
if [ -z "$badfb" ]; then
  ok "every \${PY:-…} fallback is 'false' (accepts any argv, exits non-zero)"
else
  bad "every \${PY:-…} fallback is 'false'" "unsafe fallback(s): $(printf '%s' "$badfb" | tr '\n' ' ')"
fi

# --- 9. every fixture that materialises the REAL config script must also -----
#        materialise the helper it sources.
# This failure mode has recurred three times (codex-core-launcher,
# startup-voice-managed-gate, daily-insight-stand-attribution), each time via a
# copy idiom the previous grep missed: a quoted list entry, `cp` in shell, then
# shutil.copy2. So this scans for the OUTCOME, not the idiom.
#
# COUNTS, not presence: the first draft of this guard asked "does the file
# mention python-binary.sh anywhere?" — and sync-workspace.test.sh has SIX
# config copies, so deleting one helper copy still left five and the guard
# passed. Caught by its own negative control, not by review.
miss=0
for tf in $(grep -rlE '(cp|copy2).*sutando-config\.sh' "$REPO/tests" 2>/dev/null); do
  n_cfg=$(grep -cE '(cp|copy2).*sutando-config\.sh' "$tf" 2>/dev/null || echo 0)
  n_help=$(grep -cE '(cp|copy2).*python-binary\.sh' "$tf" 2>/dev/null || echo 0)
  if [ "$n_help" -lt "$n_cfg" ]; then
    bad "fixture copies the real config script without the helper: $(basename "$tf")" \
        "config copies=$n_cfg helper copies=$n_help"
    miss=$((miss+1))
  fi
done
[ "$miss" -eq 0 ] && ok "every fixture copying the real config script also copies the helper"

# --- 10. the empty-$PY contract at the ACTIVATED startup sites ---------------
# Three P1s from CR #2599 (@qingyun-wu), each a different way the guard was
# wrong at the seam rather than in the resolver.

# 10a. A config call must not abort a startup that just promised to skip.
#      `core_runtime="$(bash sutando-config.sh core-runtime)"` under `set -e`
#      killed startup.sh outright, because sutando-config.sh now exits 1
#      without python.
if grep -qE 'core_runtime="\$\(bash "\$REPO/scripts/sutando-config\.sh" core-runtime\)"' "$REPO/src/startup.sh"; then
  bad "startup.sh tolerates a failing core-runtime lookup" "bare command substitution still aborts under set -e"
else
  ok "startup.sh tolerates a failing core-runtime lookup"
fi

# 10b. A guard must not swallow the command's environment.
#      `VAR=1 [ -n "$PY" ] && cmd` applies VAR to `[`, and runs cmd with NONE
#      of it — the named gateway lost GATEWAY_INSTANCE / its own
#      REMOTE_TASK_TOKEN / REMOTE_PROACTIVE_ROOM=.
env_swallow=$(bash -c 'PY=/bin/echo; A=1 B=2 \
  [ -n "$PY" ] && "$PY" "A=${A:-unset} B=${B:-unset}"' 2>/dev/null)
case "$env_swallow" in
  *"A=unset"*) ok "control: 'VAR=1 [ test ] && cmd' really does drop VAR (the bug shape)" ;;
  *) bad "control: the env-swallow shape" "expected A=unset, got: $env_swallow" ;;
esac
if grep -qE '^\s+REMOTE_PROACTIVE_ROOM= \\$' "$REPO/src/startup.sh" \
   && grep -A1 'REMOTE_PROACTIVE_ROOM= \\' "$REPO/src/startup.sh" | grep -qE '^\s+\[ -n'; then
  bad "named gateway keeps its per-instance credentials" "guard is inside the assignment list"
else
  ok "named gateway keeps its per-instance credentials"
fi

# 10c. No probe may EXECUTE a bare python3 — probing runs it, and on a Mac
#      without developer tools that is the stub.
probes=$(grep -nE '(^|[^-[:alnum:]_/"$])python3 -c ' "$REPO/src/startup.sh" | grep -vE '^\s*[0-9]+:\s*#' || true)
if [ -n "$probes" ]; then
  bad "no startup probe executes a bare python3 (literal)" "$probes"
else
  ok "no startup probe executes a bare python3 (literal)"
fi

# A literal scan is not enough: both interpreter-probe loops iterate a list
# ending in a bare `python3` and then run `"$_p" -c ...`, which executes the
# stub without the literal `python3 -c ` ever appearing. The first version of
# THIS check missed the discord loop for exactly that reason. So: every loop
# offering a bare python3 candidate must neutralise it before probing.
loops=0; unguarded=0
while IFS= read -r ln; do
  loops=$((loops+1))
  n="${ln%%:*}"
  # The substitution must appear before the loop body does anything with $_p.
  # Window is generous (14 lines) because a rationale comment legitimately sits
  # between; the binding check below is what makes that safe.
  if ! sed -n "$((n+1)),$((n+14))p" "$REPO/src/startup.sh" | grep -q '_p="\$PY"'; then
    unguarded=$((unguarded+1))
    bad "probe loop neutralises its bare python3 candidate (line $n)" "no _p=\"\$PY\" substitution"
  fi
done < <(grep -nE 'for _p in .* python3; do' "$REPO/src/startup.sh" || true)
if [ "$loops" -gt 0 ] && [ "$unguarded" -eq 0 ]; then
  ok "all $loops interpreter-probe loop(s) neutralise their bare python3 candidate"
fi

printf "\npassed=%d failed=%d\n" "$pass" "$fail"
[ "$fail" -eq 0 ]
