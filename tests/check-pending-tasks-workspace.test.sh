#!/bin/bash
# The Stop hook must watch the WORKSPACE queue, not <repo>/tasks/.
#
# THE DEFECT. src/check-pending-tasks.sh resolved its queue as
# `$(dirname "$0")/../tasks` — the repo root. Every producer (voice, Discord,
# Telegram, Slack, chat) and every consumer writes <workspace>/tasks/. On a
# default install those are different directories, so the hook read an empty
# one, emitted `{}` on every Stop, and never blocked on anything. It is the
# fail-silent shape: a broken hook and an empty queue emit the same `{}`, and
# the queue is empty most of the time anyone looks.
#
# ISOLATION (@john-the-dev's blocker on d1767d43). The first version of this
# test resolved the workspace from the host config and wrote its probe into the
# caller's REAL queue — the directory `watch-tasks-stream.sh` is concurrently
# watching. The watcher could claim and execute the probe as a live owner task
# before the EXIT trap removed it, and cleanup cannot retract a reply already
# delivered. A synthetic filename lowers collision odds; it does not isolate a
# live consumer.
#
# So this test builds its own workspace and pins it: `SUTANDO_TEST_MODE=1` plus
# `SUTANDO_WORKSPACE` is the repo's supported escape hatch (src/sutando_config.py
# ~line 336 — the env var alone is ignored post-v0.8/#1440). Passing a temp dir
# is NOT isolation on its own, so the resolved path is ASSERTED before anything
# is written: if the hatch ever regresses, this aborts instead of quietly
# writing to the live queue.
#
# WHY BOTH DIRECTIONS ARE PINNED. "Blocks when a task exists" is satisfied by a
# hook that watches EITHER directory, and "stays quiet when empty" is satisfied
# by the broken version. The case that separates them is the legacy one: a file
# in <repo>/tasks/ must NOT block, because reading that directory is the bug.
# Without it, a fix watching both paths passes — and reintroduces the defect in
# the other direction, since a stale legacy file would then block forever.
#
# Run: bash tests/check-pending-tasks-workspace.test.sh
set -u

REPO="$(cd "$(dirname "$0")/.." && pwd)"
HOOK="$REPO/src/check-pending-tasks.sh"

# Fixture setup needs a REAL git, not whatever bare `git` resolves to here --
# go through the same resolver the hook uses, or the suite risks its own failure mode.
. "$REPO/scripts/git-binary.sh"
TEST_GIT="$(resolve_git)"
if [ -z "$TEST_GIT" ]; then
  echo "FAIL: no usable git found to build test fixtures with."
  exit 1
fi

# Same reasoning, same fix, for python3 -- a bare `|| echo python3` fallback is
# exactly the CLT-stub risk this suite exists to catch, just in its own setup.
. "$REPO/scripts/python-binary.sh"
TEST_PY="$(resolve_python "$REPO")"
if [ -z "$TEST_PY" ]; then
  echo "FAIL: no usable python3 found to build test fixtures with."
  exit 1
fi

# set -u doesn't catch a command failure -- a silent init/commit failure would
# leave a non-repo directory and every case below would pass for the wrong reason.
_git_fixture_repo() {
  "$TEST_GIT" -C "$1" init -q \
    && "$TEST_GIT" -C "$1" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init \
    || { echo "FAIL: fixture git init/commit failed in $1 -- aborting rather than run a vacuous suite"; exit 1; }
}

# --- Build and PIN an isolated workspace before resolving anything ----------
TMPWS="$(mktemp -d "${TMPDIR:-/tmp}/sutando-hooktest.XXXXXX")"

# The hook now also refuses a turn that ends with no message and no recorded
# no-send. These cases assert the TASK gate, so satisfy the turn gate first or
# they measure the wrong refusal.
record_delivery() {
  "$TEST_PY" \
    "$REPO/src/turn_ledger.py" --workspace "$TMPWS" no-send "hook unit test" >/dev/null 2>&1 || true
}

export SUTANDO_TEST_MODE=1
export SUTANDO_WORKSPACE="$TMPWS"
# The suite models the CORE: the launcher's marked session. An unmarked session
# is a guest and is not gated at all (case 7).
export SUTANDO_CORE_SESSION=1
# This suite is about the queue and guest gates; the watcher-coverage gate (a
# temp inbox nobody watches would block every case) has its own suite.
export SUTANDO_STOP_HOOK_WATCHER_GATE=0

LIVE_WS="$(env -u SUTANDO_TEST_MODE -u SUTANDO_WORKSPACE \
             bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)"
WS="$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)"

# Compare resolved paths, not the strings we passed in — mktemp may hand back a
# symlinked prefix (/tmp -> /private/tmp on macOS).
_real() { (cd "$1" 2>/dev/null && pwd -P) || echo "$1"; }
if [ "$(_real "$WS")" != "$(_real "$TMPWS")" ]; then
  echo "FAIL: workspace did not resolve to the test dir — refusing to run."
  echo "      wanted: $TMPWS"
  echo "      got:    $WS"
  echo "      The SUTANDO_TEST_MODE escape hatch (src/sutando_config.py) has changed."
  rm -rf "$TMPWS"
  exit 1
fi
if [ -n "$LIVE_WS" ] && [ "$(_real "$WS")" = "$(_real "$LIVE_WS")" ]; then
  echo "FAIL: test workspace is the live workspace — refusing to run."
  rm -rf "$TMPWS"
  exit 1
fi

# The legacy probe (case 4) must go where the BUG reads, so it is the one write
# outside the temp tree. It is safe: watch-tasks-stream.sh resolves its
# TASKS_DIR from `sutando-config.sh workspace` (line 38), so <repo>/tasks/ has
# no consumer — and the guard below refuses if the two are ever the same dir.
PROBE="task-zz-hooktest-$$.txt"
LEGACY_DIR="$REPO/tasks"

cleanup() { rm -rf "$TMPWS"; rm -f "$LEGACY_DIR/$PROBE"; }
trap cleanup EXIT

FAILED=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n     %s\n' "$1" "$2"; FAILED=1; }

mkdir -p "$WS/tasks" "$WS/results"

# 1. A task in the workspace queue must block. FAILS against the old path.
printf 'id: probe\ntask: probe\n' > "$WS/tasks/$PROBE"
OUT="$(bash "$HOOK" 2>&1)"
case "$OUT" in
  *'"decision":"block"'*) ok "workspace task blocks" ;;
  *) bad "workspace task blocks" "got: ${OUT:0:120}" ;;
esac

# 2. ...and the payload must name the task, not just block generically.
case "$OUT" in
  *"$PROBE"*) ok "block payload names the task" ;;
  *) bad "block payload names the task" "payload omits $PROBE" ;;
esac

# 3. A task with a matching result is already handled — must not block.
printf 'done\n' > "$WS/results/$PROBE"
OUT="$(bash "$HOOK" 2>&1)"
case "$OUT" in
  '{}') ok "result present suppresses the block" ;;
  *) bad "result present suppresses the block" "got: ${OUT:0:120}" ;;
esac
rm -f "$WS/tasks/$PROBE" "$WS/results/$PROBE"

# 4. THE DIRECTION CASE. A file in the legacy <repo>/tasks/ must NOT block.
if [ "$(_real "$LEGACY_DIR")" = "$(_real "$WS/tasks")" ]; then
  printf '  skip legacy dir IS the resolved queue here; direction is not decidable\n'
else
  mkdir -p "$LEGACY_DIR"
  printf 'id: probe\ntask: legacy\n' > "$LEGACY_DIR/$PROBE"
  record_delivery
  OUT="$(bash "$HOOK" 2>&1)"
  case "$OUT" in
    '{}') ok "legacy <repo>/tasks/ does not block" ;;
    *) bad "legacy <repo>/tasks/ does not block" "hook still reads the repo: ${OUT:0:120}" ;;
  esac
  rm -f "$LEGACY_DIR/$PROBE"
fi

# 5. Empty queue stays quiet — the control that proves case 1 measured something.
record_delivery
OUT="$(bash "$HOOK" 2>&1)"
case "$OUT" in
  '{}') ok "empty queue emits {}" ;;
  *) bad "empty queue emits {}" "got: ${OUT:0:120}" ;;
esac

# 6. THE REJECTION PATH. A refused interpreter must not fall back to bare
# `python3`. A recording `git` stub witnesses resolve_git actually ran.
REJ="$(mktemp -d)"
mkdir -p "$REJ/scripts" "$REJ/src" "$REJ/workspace/tasks" "$REJ/workspace/results"
printf '#!/bin/bash\n[ "$1" = "workspace" ] && { echo "%s/workspace"; exit 0; }\nexit 1\n' "$REJ" \
  > "$REJ/scripts/sutando-config.sh"
chmod +x "$REJ/scripts/sutando-config.sh"
cp "$HOOK" "$REJ/src/"
cp "$REPO/scripts/git-binary.sh" "$REJ/scripts/"
printf 'id: probe\ntask: rejected-interpreter\n' > "$REJ/workspace/tasks/$PROBE"
# A recording shim: if the hook falls back to PATH python this fires.
printf '#!/bin/bash\necho FALLBACK_INVOKED >&2\nexit 79\n' > "$REJ/python3"
chmod +x "$REJ/python3"
# Boundary-aware recording stub (both probes echo $REJ, falling through below):
# argc + one arg per line, not "$*", which would log 3 args and 1 with a space identically.
printf '#!/bin/bash\n{ printf "GIT_CALLED argc=%%s\\n" "$#"; for a in "$@"; do printf "ARG<%%s>\\n" "$a"; done; } >> %s\necho %s\n' \
  "$REJ/git-calls.log" "$REJ" > "$REJ/git"
chmod +x "$REJ/git"
printf '#!/bin/sh\nexit 2\n' > "$REJ/xcode-select"
chmod +x "$REJ/xcode-select"
# A NON-Git cwd (never this checkout) so the guest fall-through, not a real
# identity or a short-circuiting guest exit, is what reaches the logic below.
REJ_CWD="$(mktemp -d)"
REJ_ERR="$(cd "$REJ_CWD" && OSTYPE=darwin25 PATH="$REJ:$PATH" bash "$REJ/src/$(basename "$HOOK")" 2>&1 >/dev/null)"
rm -f "$REJ/git-calls.log"
REJ_OUT="$(cd "$REJ_CWD" && OSTYPE=darwin25 PATH="$REJ:$PATH" bash "$REJ/src/$(basename "$HOOK")" 2>/dev/null)"
case "$REJ_ERR" in
  *FALLBACK_INVOKED*) bad "a refused interpreter is not worked around" "the bare python3 fallback ran" ;;
  *) ok "a refused interpreter is not worked around" ;;
esac
# EXACT equality, not a substring -- extra noise alongside the good message
# (git-binary.sh failing to source, a stray command-not-found) must not pass.
if [ "$REJ_ERR" = "check-pending-tasks: no usable interpreter; queue not reported" ]; then
  ok "stderr is exactly the one deliberate rejection message, nothing else"
else
  bad "stderr is exactly the one deliberate rejection message, nothing else" "got: ${REJ_ERR:0:160}"
fi
case "$REJ_OUT" in
  '{}') ok "a refused interpreter still emits valid JSON" ;;
  *) bad "a refused interpreter still emits valid JSON" "got: ${REJ_OUT:0:120}" ;;
esac
# Identity comes from the launcher's marker now, never from a git probe: the
# recording stub must stay silent (a bare `git` here would raise the CLT dialog
# this path exists to avoid).
if [ ! -f "$REJ/git-calls.log" ]; then
  ok "the hook never invokes git (identity is the launcher's marker, not the cwd's repo)"
else
  bad "the hook never invokes git (identity is the launcher's marker, not the cwd's repo)" \
    "git ran: $(cat "$REJ/git-calls.log")"
fi
# Isolated positive control, separate whole-file controls: the fixture's own
# args never contain a space, and a shared-log line-range slice can be fooled.
_argv_lab="$(mktemp -d)"
# $1 is the log path, consumed via shift BEFORE argc/$@ are recorded -- it must
# never itself be counted as one of the args under test.
printf '#!/bin/bash\n_log="$1"; shift\n{ printf "GIT_CALLED argc=%%s\\n" "$#"; for a in "$@"; do printf "ARG<%%s>\\n" "$a"; done; } >> "$_log"\n' \
  > "$_argv_lab/git"
chmod +x "$_argv_lab/git"
"$_argv_lab/git" "$_argv_lab/log-a" -C /a b c >/dev/null      # 3 separate args
"$_argv_lab/git" "$_argv_lab/log-b" -C "/a b c" >/dev/null    # 1 quoted arg, joins to the same string
GCL_A="$(cat "$_argv_lab/log-a" 2>/dev/null)"
GCL_B="$(cat "$_argv_lab/log-b" 2>/dev/null)"
if [ -z "$GCL_A" ] || [ -z "$GCL_B" ]; then
  bad "the stub's own recording distinguishes 3 args from 1 quoted arg joining to the same string" \
    "one or both invocations produced no log at all -- A=[$GCL_A] B=[$GCL_B]"
elif [ "$GCL_A" = "$GCL_B" ]; then
  bad "the stub's own recording distinguishes 3 args from 1 quoted arg joining to the same string" \
    "both logged identically: A=[$GCL_A] B=[$GCL_B]"
else
  ok "the stub's own recording distinguishes 3 args from 1 quoted arg joining to the same string"
fi
rm -rf "$_argv_lab"

# BEHAVIORAL, not source text (a regex passes on a dead/commented guard) --
# `bash -x` traces an empty PYBIN as one-or-more `+` (a command substitution nests one deeper).
EMPTY_EXEC_RE="^\+{1,} '' "
# Real xtrace capture positive control -- a fabricated string only proves the
# regex; drives a known-bad script through the real -x/PS4/redirect instead.
_pc_lab="$(mktemp -d)"
cat > "$_pc_lab/probe.sh" <<'EOF'
BAD_PYBIN=""
"$BAD_PYBIN" /tmp/direct-site-probe.py --x
OUT="$("$BAD_PYBIN" /tmp/nested-site-probe.py --y)"
EOF
(cd "$_pc_lab" && PS4='+ ' bash -x probe.sh) >/dev/null 2>"$_pc_lab/trace.log"
_pc_direct=$(grep -cE '^\+ '"'"''"'"' /tmp/direct-site-probe\.py' "$_pc_lab/trace.log")
_pc_nested=$(grep -cE '^\+\+ '"'"''"'"' /tmp/nested-site-probe\.py' "$_pc_lab/trace.log")
if [ -s "$_pc_lab/trace.log" ] && grep -qE "$EMPTY_EXEC_RE" "$_pc_lab/trace.log" \
   && [ "$_pc_direct" -eq 1 ] && [ "$_pc_nested" -eq 1 ]; then
  ok "a real bash -x/PS4/redirect capture of a known-bad script produces a trace the detector matches, at both nesting depths"
else
  bad "a real bash -x/PS4/redirect capture of a known-bad script produces a trace the detector matches, at both nesting depths" \
    "trace bytes=$(wc -c < "$_pc_lab/trace.log" 2>/dev/null || echo 0), direct-hits=$_pc_direct, nested-hits=$_pc_nested"
fi
rm -rf "$_pc_lab"

# 6b. THE READINESS-CHECK GAP (first PYBIN site) -- a task WITH a result exits
# earlier and never reaches the turn-ledger site, isolating this site from that one.
rm -f "$REJ/workspace/tasks/$PROBE"
RESULT_PROBE="task-zz-hooktest-readiness-$$.txt"
printf 'id: probe\ntask: readiness-gap-probe\n' > "$REJ/workspace/tasks/$RESULT_PROBE"
printf 'a real, well-formed reply\n' > "$REJ/workspace/results/$RESULT_PROBE"
RG_ERR="$(cd "$REJ_CWD" && OSTYPE=darwin25 PATH="$REJ:$PATH" bash "$REJ/src/$(basename "$HOOK")" 2>&1 >/dev/null)"
RG_OUT="$(cd "$REJ_CWD" && OSTYPE=darwin25 PATH="$REJ:$PATH" bash "$REJ/src/$(basename "$HOOK")" 2>/dev/null)"
case "$RG_ERR" in
  *"command not found"*|*": : "*)
    bad "a truly result-only queue with no interpreter produces no stray command-not-found noise" "got: ${RG_ERR:0:160}" ;;
  *) ok "a truly result-only queue with no interpreter produces no stray command-not-found noise" ;;
esac
case "$RG_OUT" in
  '{}') ok "a truly result-only queue with no interpreter still emits valid JSON" ;;
  *) bad "a truly result-only queue with no interpreter still emits valid JSON" "got: ${RG_OUT:0:120}" ;;
esac
(cd "$REJ_CWD" && OSTYPE=darwin25 PATH="$REJ:$PATH" PS4='+ ' bash -x "$REJ/src/$(basename "$HOOK")") >/dev/null 2>"$REJ/xtrace-site1.log"
# TRACE-IS-ALIVE sentinel, checked before trusting an absence below -- a
# missing -x, unset PS4, or broken redirect all produce the same empty file.
if ! grep -qF "TASKS_DIR=" "$REJ/xtrace-site1.log"; then
  bad "site-1 trace capture is alive (not silently empty/broken)" \
    "no TASKS_DIR= line -- trace bytes=$(wc -c < "$REJ/xtrace-site1.log")"
elif grep -qE "$EMPTY_EXEC_RE" "$REJ/xtrace-site1.log"; then
  bad "readiness-check site (result-present queue) never execs an empty command" \
    "$(grep -E "$EMPTY_EXEC_RE" "$REJ/xtrace-site1.log" | head -1)"
else
  ok "readiness-check site (result-present queue) never execs an empty command (trace confirmed alive)"
fi
rm -f "$REJ/workspace/tasks/$RESULT_PROBE" "$REJ/workspace/results/$RESULT_PROBE" "$REJ/xtrace-site1.log"

# 6b2. THE TURN-LEDGER SITE (second PYBIN site) -- only a queue with ZERO
# task files reaches it; any task (with or without a result) exits earlier.
TG_OUT="$(cd "$REJ_CWD" && OSTYPE=darwin25 PATH="$REJ:$PATH" bash "$REJ/src/$(basename "$HOOK")" 2>/dev/null)"
case "$TG_OUT" in
  '{}') ok "an empty queue with no interpreter still emits valid JSON" ;;
  *) bad "an empty queue with no interpreter still emits valid JSON" "got: ${TG_OUT:0:120}" ;;
esac
(cd "$REJ_CWD" && OSTYPE=darwin25 PATH="$REJ:$PATH" PS4='+ ' bash -x "$REJ/src/$(basename "$HOOK")") >/dev/null 2>"$REJ/xtrace-site2.log"
# TRACE-IS-ALIVE sentinel -- see the matching comment at site 1 above.
if ! grep -qF "TASKS_DIR=" "$REJ/xtrace-site2.log"; then
  bad "site-2 trace capture is alive (not silently empty/broken)" \
    "no TASKS_DIR= line -- trace bytes=$(wc -c < "$REJ/xtrace-site2.log")"
elif grep -qE "$EMPTY_EXEC_RE" "$REJ/xtrace-site2.log"; then
  bad "turn-ledger site (empty queue) never execs an empty command" \
    "$(grep -E "$EMPTY_EXEC_RE" "$REJ/xtrace-site2.log" | head -1)"
else
  ok "turn-ledger site (empty queue) never execs an empty command (trace confirmed alive)"
fi
rm -f "$REJ/xtrace-site2.log"

# 6c. EXISTENCE IS NOT READINESS -- an empty result file with no interpreter
# to check it must stay UNPROCESSED, not vanish into a quiet {}.
EMPTY_PROBE="task-zz-hooktest-emptyresult-$$.txt"
printf 'id: probe\ntask: empty-result-probe\n' > "$REJ/workspace/tasks/$EMPTY_PROBE"
: > "$REJ/workspace/results/$EMPTY_PROBE"
ER_ERR="$(cd "$REJ_CWD" && OSTYPE=darwin25 PATH="$REJ:$PATH" bash "$REJ/src/$(basename "$HOOK")" 2>&1 >/dev/null)"
if [ "$ER_ERR" = "check-pending-tasks: no usable interpreter; queue not reported" ]; then
  ok "a genuinely empty result with no interpreter is NOT silently treated as done"
else
  bad "a genuinely empty result with no interpreter is NOT silently treated as done" \
    "expected the no-interpreter warning; got: ${ER_ERR:0:160}"
fi
rm -f "$REJ/workspace/tasks/$EMPTY_PROBE" "$REJ/workspace/results/$EMPTY_PROBE"
rm -f "$REJ/workspace/tasks/$RESULT_PROBE" "$REJ/workspace/results/$RESULT_PROBE"
rm -rf "$REJ_CWD"
# 7. IDENTITY. The core is the session the launcher marked (SUTANDO_CORE_SESSION=1)
# or an enrolled worker (SUTANDO_INSTANCE_ID). Any other session in this checkout
# -- an ad-hoc `claude`, a `claude -p <skill>` one-shot -- is a GUEST and owes
# nothing (user report 2026-09-24: a one-shot skill session was blocked at every
# Stop on the live core's own queue and looped). Identity used to be read from
# the cwd's git repo, which cannot tell a guest in the checkout from the core.
printf 'id: probe\ntask: guest-probe\n' > "$WS/tasks/$PROBE"
# 7a. No marked core alive: the unmarked session may BE the core (a hand launch),
# so it is gated -- the guest exit fails closed (review of #4863).
NOCORE_OUT="$(env -u SUTANDO_CORE_SESSION bash "$HOOK" 2>"$TMPWS/nocore.err")"
case "$NOCORE_OUT" in
  *'"decision":"block"'*) ok "an unmarked session with NO live marked core is gated as the core (fail closed)" ;;
  *) bad "an unmarked session with NO live marked core is gated as the core (fail closed)" "got: ${NOCORE_OUT:0:120}" ;;
esac
grep -q "no live marked core" "$TMPWS/nocore.err" \
  && ok "...and says so on stderr" || bad "...and says so on stderr" "stderr: $(cat "$TMPWS/nocore.err")"
# 7b. A stale heartbeat is no core either. The file is THIS host's, named the way
# core_heartbeat.py names it (util_paths._host_label).
HOST_LABEL="$("$TEST_PY" -c "import sys; sys.path.insert(0, '$REPO/src'); from util_paths import _host_label; print(_host_label())")"
[ -n "$HOST_LABEL" ] || { bad "fixture: host label resolved" "empty"; HOST_LABEL="$(hostname -s)"; }
mkdir -p "$WS/state/cores"
printf '{"pid":1,"socket":"/tmp/x.sock","session":"core"}\n' > "$WS/state/cores/$HOST_LABEL.alive"
touch -t 202001010000 "$WS/state/cores/$HOST_LABEL.alive"
STALE_OUT="$(env -u SUTANDO_CORE_SESSION bash "$HOOK" 2>/dev/null)"
case "$STALE_OUT" in
  *'"decision":"block"'*) ok "a stale state/cores/<host>.alive does not make the unmarked session a guest" ;;
  *) bad "a stale state/cores/<host>.alive does not make the unmarked session a guest" "got: ${STALE_OUT:0:120}" ;;
esac
# 7b2. ANOTHER host's fresh heartbeat (the workspace syncs them) is not this host's core.
printf '{"pid":1,"socket":"/tmp/peer.sock","session":"core"}\n' > "$WS/state/cores/some-other-host.alive"
FOREIGN_OUT="$(env -u SUTANDO_CORE_SESSION bash "$HOOK" 2>/dev/null)"
case "$FOREIGN_OUT" in
  *'"decision":"block"'*) ok "a foreign host's live heartbeat does not make the unmarked session a guest" ;;
  *) bad "a foreign host's live heartbeat does not make the unmarked session a guest" "got: ${FOREIGN_OUT:0:120}" ;;
esac
rm -f "$WS/state/cores/some-other-host.alive"
# 7c. A fresh heartbeat of THIS host: a marked core owns the queue, so the unmarked session is a guest.
touch "$WS/state/cores/$HOST_LABEL.alive"
GUEST_OUT="$(env -u SUTANDO_CORE_SESSION bash "$HOOK" 2>"$TMPWS/guest.err")"
case "$GUEST_OUT" in
  '{}') ok "an unmarked session beside a live marked core is a guest: {} with the queue pending" ;;
  *) bad "an unmarked session beside a live marked core is a guest: {} with the queue pending" "got: ${GUEST_OUT:0:120}" ;;
esac
grep -q "guest session" "$TMPWS/guest.err" \
  && ok "...and says so on stderr" || bad "...and says so on stderr" "stderr: $(cat "$TMPWS/guest.err")"
GUEST_ZERO_OUT="$(SUTANDO_CORE_SESSION=0 bash "$HOOK" 2>/dev/null)"
case "$GUEST_ZERO_OUT" in
  '{}') ok "SUTANDO_CORE_SESSION set to anything but 1 is not the mark" ;;
  *) bad "SUTANDO_CORE_SESSION set to anything but 1 is not the mark" "got: ${GUEST_ZERO_OUT:0:120}" ;;
esac

# 7b. A guest in an unrelated repo's worktree is a guest too (the old carve-out's case).
GUEST_REPO="$(mktemp -d)"
_git_fixture_repo "$GUEST_REPO"
GUEST_WT_OUT="$(cd "$GUEST_REPO" && env -u SUTANDO_CORE_SESSION bash "$HOOK" 2>/dev/null)"
case "$GUEST_WT_OUT" in
  '{}') ok "an unmarked session in an unrelated repo is not blocked by the core's queue" ;;
  *) bad "an unmarked session in an unrelated repo is not blocked by the core's queue" "got: ${GUEST_WT_OUT:0:120}" ;;
esac

# 8. CONTROL FOR 7. The MARKED core is gated wherever it runs: this checkout and
# a foreign cwd alike (SUTANDO_CLAUDE_WORKING_DIR is a supported core config).
MARKED_HERE_OUT="$(bash "$HOOK" 2>&1)"
case "$MARKED_HERE_OUT" in
  *'"decision":"block"'*) ok "the marked core in the checkout still blocks" ;;
  *) bad "the marked core in the checkout still blocks" "got: ${MARKED_HERE_OUT:0:120}" ;;
esac
MARKED_FOREIGN_OUT="$(cd "$GUEST_REPO" && bash "$HOOK" 2>&1)"
case "$MARKED_FOREIGN_OUT" in
  *'"decision":"block"'*) ok "the marked core in a foreign repo still blocks on the pending queue" ;;
  *) bad "the marked core in a foreign repo still blocks on the pending queue" "got: ${MARKED_FOREIGN_OUT:0:160}" ;;
esac
case "$MARKED_FOREIGN_OUT" in
  *"$PROBE"*) ok "the marked-core block payload names the pending task" ;;
  *) bad "the marked-core block payload names the pending task" "payload omits $PROBE" ;;
esac
rm -f "$WS/tasks/$PROBE"

# 8b. An ENROLLED WORKER (SUTANDO_INSTANCE_ID set, no core mark) in a foreign
# worktree is gated on its OWN deliveries -- foreign --cwd is a supported worker config.
WFR_WORKER="worker-foreign-$$"
WFR_PROBE="task-wfr-hooktest-$$"
mkdir -p "$WS/deliveries/$WFR_WORKER"
: > "$WS/deliveries/$WFR_WORKER/$WFR_PROBE.txt"
printf 'id: %s\ntask: worker-foreign-probe\n' "$WFR_PROBE" > "$WS/tasks/$WFR_PROBE.txt"
WFR_OUT="$(cd "$GUEST_REPO" && env -u SUTANDO_CORE_SESSION SUTANDO_INSTANCE_ID="$WFR_WORKER" bash "$HOOK" 2>&1)"
case "$WFR_OUT" in
  *'"decision":"block"'*) ok "an enrolled worker in a foreign worktree still blocks on its own pending delivery" ;;
  *) bad "an enrolled worker in a foreign worktree still blocks on its own pending delivery" "got: ${WFR_OUT:0:160}" ;;
esac
case "$WFR_OUT" in
  *"$WFR_PROBE"*) ok "the foreign-worker block payload names its own task" ;;
  *) bad "the foreign-worker block payload names its own task" "payload omits $WFR_PROBE" ;;
esac

# 8c. ...clears once that same worker's result is ready -- proves 8b is the
# real delivery gate, not a stuck-open one.
printf 'done\n' > "$WS/results/$WFR_PROBE.txt"
record_delivery
WFR_CLEAR_OUT="$(cd "$GUEST_REPO" && env -u SUTANDO_CORE_SESSION SUTANDO_INSTANCE_ID="$WFR_WORKER" bash "$HOOK" 2>&1)"
case "$WFR_CLEAR_OUT" in
  '{}') ok "the foreign-worker block clears once its own result is ready" ;;
  *) bad "the foreign-worker block clears once its own result is ready" "got: ${WFR_CLEAR_OUT:0:160}" ;;
esac
rm -f "$WS/results/$WFR_PROBE.txt" "$WS/deliveries/$WFR_WORKER/$WFR_PROBE.txt" "$WS/tasks/$WFR_PROBE.txt"
rmdir "$WS/deliveries/$WFR_WORKER" 2>/dev/null || true
rm -rf "$GUEST_REPO"

# 9. A PACKAGED BUNDLE has no .git at all; identity does not depend on one.
BUNDLE="$(mktemp -d)"
mkdir -p "$BUNDLE/src" "$BUNDLE/scripts" "$BUNDLE/workspace/tasks" "$BUNDLE/workspace/results"
cp "$REPO/src/check-pending-tasks.sh" "$BUNDLE/src/"
printf '#!/bin/bash\ncase "$1" in\n  workspace) echo "%s/workspace"; exit 0 ;;\n  python-bin) echo "%s"; exit 0 ;;\nesac\nexit 1\n' \
  "$BUNDLE" "$TEST_PY" > "$BUNDLE/scripts/sutando-config.sh"
chmod +x "$BUNDLE/scripts/sutando-config.sh"
printf 'id: probe\ntask: bundle-probe\n' > "$BUNDLE/workspace/tasks/$PROBE"
mkdir -p "$BUNDLE/workspace/state/cores" && printf '{"socket":"/tmp/x.sock"}\n' > "$BUNDLE/workspace/state/cores/$HOST_LABEL.alive"
BUNDLE_CWD="$(mktemp -d)"
B_CORE_OUT="$(cd "$BUNDLE_CWD" && bash "$BUNDLE/src/$(basename "$HOOK")" 2>&1)"
case "$B_CORE_OUT" in
  *'"decision":"block"'*) ok "non-Git bundle + marked core -> blocks on the bundle's queue" ;;
  *) bad "non-Git bundle + marked core -> blocks on the bundle's queue" "got: ${B_CORE_OUT:0:120}" ;;
esac
B_GUEST_OUT="$(cd "$BUNDLE_CWD" && env -u SUTANDO_CORE_SESSION bash "$BUNDLE/src/$(basename "$HOOK")" 2>/dev/null)"
case "$B_GUEST_OUT" in
  '{}') ok "non-Git bundle + unmarked session -> guest ({})" ;;
  *) bad "non-Git bundle + unmarked session -> guest ({})" "got: ${B_GUEST_OUT:0:120}" ;;
esac
rm -rf "$BUNDLE_CWD" "$BUNDLE"

if [ "$FAILED" -eq 0 ]; then echo "PASS"; else echo "FAIL"; fi
exit "$FAILED"
