#!/usr/bin/env bash
# The watcher may only hand a session task files that are routed to it.
#
# Before this gate the watched directory was dispatched wholesale — including
# `.assigned-<other>` and `.claimed-<other>` — so a session with no acquisition
# step could execute a task concurrently with the worker it was routed to, and
# a crash-then-reclaim could repeat an irreversible side effect.
#
# Sources the REAL gate rather than restating it: a hand-copied rule passes
# while production drifts.
#
# Runs under CI (the shell-standalone-tests step) and manually via
# `bash tests/task-route-gate.test.sh`.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
WATCHER="$REPO/src/watch-tasks-stream.sh"
ROUTER="$REPO/src/task-route.sh"

# Failures are recorded in a FILE, not a variable: the env-isolation cases below
# run in ( subshells ), so a `fail=1` there is discarded at the closing paren and
# the suite exits 0 while printing FAIL.
FAILLOG="$(mktemp)"
trap 'rm -f "$FAILLOG"' EXIT
check() {  # check <label> <expected> <actual>
	if [ "$2" = "$3" ]; then
		echo "  ok   $1"
	else
		echo "  FAIL $1: expected '$2', got '$3'"; echo "$1" >> "$FAILLOG"
	fi
}

# ── wiring: the gate is worthless if the watcher never loads or calls it ──
check "the watcher sources the routing gate" \
	"1" "$(grep -c 'source "\$__SCRIPT_DIR/task-route.sh"' "$WATCHER" || true)"
check "...and initialises it" \
	"1" "$(grep -cE '^task_route_init$' "$WATCHER" || true)"
check "...and the file it sources defines both functions" \
	"2" "$(grep -cE '^(task_route_init|task_routed_here)\(\)' "$ROUTER" || true)"
# There is no `set -e` in the watcher, so a missing function is rc=127 and
# NON-FATAL — the gate would read as "not routed here" and silently drop every
# task. Pin that dispatch consults it, and that it is the first thing it does.
check "dispatch_task consults the gate" \
	"1" "$(grep -c 'if ! task_routed_here "\$filename"; then' "$WATCHER" || true)"

# ── behaviour: source the real gate and drive every row of the table ──
# shellcheck source=../src/task-route.sh
source "$ROUTER"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP" "$FAILLOG"' EXIT
mkdir -p "$TMP/agents"

routed() {  # routed <filename> -> yes|no
	if task_routed_here "$1"; then echo yes; else echo no; fi
}

# ---- a worker: its own assignments and unassigned work, nothing else --------
POOL_WORKER="core-1"; POOL_INSTALLED=1
check "worker takes an unassigned task"        "yes" "$(routed 'task-a.txt')"
check "worker takes its OWN assignment"        "yes" "$(routed 'task-a.assigned-core-1.txt')"
check "worker refuses a sibling's assignment"  "no"  "$(routed 'task-a.assigned-core-2.txt')"
check "worker refuses its own claim"           "no"  "$(routed 'task-a.claimed-core-1.txt')"
check "worker refuses a sibling's claim"       "no"  "$(routed 'task-a.claimed-core-2.txt')"
# A prefix must not be mistaken for the whole id: core-1 is not core-10.
check "worker refuses a longer worker's name"  "no"  "$(routed 'task-a.assigned-core-10.txt')"
# Ids legitimately contain dots (task-<inst>~<id>), so the split must key on
# the LAST .assigned- rather than the first dot.
check "dotted id still routes to its worker"   "yes" "$(routed 'task-dev~a.b.assigned-core-1.txt')"

# ---- no identity, workers installed: the queue is routed, take none of it ----
POOL_WORKER=""; POOL_INSTALLED=1
check "non-member refuses unassigned work while workers exist" \
	"no" "$(routed 'task-a.txt')"
check "non-member refuses an assignment" \
	"no" "$(routed 'task-a.assigned-core-1.txt')"
check "non-member refuses a claim" \
	"no" "$(routed 'task-a.claimed-core-1.txt')"

# ---- no identity, no workers: the single-session case, unchanged ------------
POOL_WORKER=""; POOL_INSTALLED=0
check "single session still takes unassigned work" "yes" "$(routed 'task-a.txt')"
# Even here a pool-owned name is refused: those files only exist if something
# routed them, and this session did not acquire them.
check "single session still refuses an assignment" \
	"no" "$(routed 'task-a.assigned-core-1.txt')"

# ── detection: task_route_init resolves identity and installed-ness ──────────
( unset SUTANDO_CORE_ID SUTANDO_POOL_WORKER
  export SUTANDO_POOL_AGENTS_DIR="$TMP/agents"
  task_route_init
  check "no plists -> workers not installed" "0" "$POOL_INSTALLED"
  check "no env      -> no worker identity"   ""  "$POOL_WORKER" )

( unset SUTANDO_POOL_WORKER
  export SUTANDO_POOL_AGENTS_DIR="$TMP/agents" SUTANDO_CORE_ID=3
  touch "$TMP/agents/com.sutando.core-3.plist"
  task_route_init
  check "plists present -> workers installed" "1" "$POOL_INSTALLED"
  check "SUTANDO_CORE_ID -> worker identity"  "core-3" "$POOL_WORKER" )

# Multi-digit ids are real seats: the installer's glob is core-[0-9]*, so the
# numeric gate must not be a single-digit test.
( unset SUTANDO_POOL_WORKER
  export SUTANDO_POOL_AGENTS_DIR="$TMP/agents" SUTANDO_CORE_ID=12
  task_route_init
  check "multi-digit id is still a worker" "core-12" "$POOL_WORKER" )

# The MAIN core is documented to run with SUTANDO_CORE_ID=legacy. Deriving a
# worker identity from any non-empty value made it 'core-legacy', which passes
# the has-identity branch and takes unassigned work while the lead is assigning
# that same work to a follower -- the duplicate-execution path this gate closes.
# core-status.sh already draws this line numerically; both sites must agree.
( unset SUTANDO_POOL_WORKER
  export SUTANDO_POOL_AGENTS_DIR="$TMP/agents" SUTANDO_CORE_ID=legacy
  task_route_init
  check "main core (CORE_ID=legacy) is NOT a worker" "" "$POOL_WORKER"
  check "...so it refuses unassigned work while workers exist" \
    "no" "$(routed 'task-a.txt')"
  check "...and refuses a follower's assignment" \
    "no" "$(routed 'task-a.assigned-core-1.txt')" )

# An explicit trusted stamp still wins: it is set deliberately, not derived.
( export SUTANDO_POOL_AGENTS_DIR="$TMP/agents" SUTANDO_CORE_ID=legacy SUTANDO_POOL_WORKER=core-7
  task_route_init
  check "explicit SUTANDO_POOL_WORKER overrides the numeric gate" "core-7" "$POOL_WORKER" )

# The narrower glob matters: com.sutando.core-agent.plist is the single-core
# job, not a worker, and must not read as "a pool is installed".
( unset SUTANDO_CORE_ID SUTANDO_POOL_WORKER
  rm -f "$TMP/agents"/*.plist
  touch "$TMP/agents/com.sutando.core-agent.plist"
  export SUTANDO_POOL_AGENTS_DIR="$TMP/agents"
  task_route_init
  check "core-agent.plist is not a worker" "0" "$POOL_INSTALLED" )

fail_count="$(wc -l < "$FAILLOG" | tr -d ' ')"
if [ "$fail_count" -eq 0 ]; then
	echo "PASS: task routing gate"
	exit 0
fi
echo "FAIL: task routing gate ($fail_count assertion(s))"
exit 1
