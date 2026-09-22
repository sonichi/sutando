#!/usr/bin/env bash
# Every TASK_FILE emit must be non-fatal AND non-silent.
#
# The normal-path emits use `|| exit 0` (no consumer -> stop). The shutdown and
# handler-fallback emits must not abort the rest of the drain, so they cannot use
# that — but `|| true` made them non-fatal AND silent, and a lost line then left
# no trace beside the stderr note the preceding `echo` had already printed. That
# is the shape #2934 fails with: stderr carries "optional task handler failed",
# stdout carries no TASK_FILE line, and nothing on disk says whether the write
# failed or succeeded-then-vanished.
#
# Sources the REAL emitters rather than restating them: a hand-copied
# reimplementation passes while production drifts.
#
# Runs under CI (the shell-standalone-tests step) and manually via
# `bash tests/watch-tasks-stream-shutdown-emit.test.sh`.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
WATCHER="$REPO/src/watch-tasks-stream.sh"
EMITTERS="$REPO/src/task-emit.sh"

fail=0
check() {  # check <label> <expected> <actual>
    if [ "$2" = "$3" ]; then
        echo "  ok   $1"
    else
        echo "  FAIL $1: expected '$2', got '$3'"; fail=1
    fi
}

# ── structural: no silent form survives, and every site routes through a helper ──
silent=$(grep -cE "printf 'TASK_FILE: %s\\\\n' \"\\\$filename\"( >&9)? \|\| true" \
    "$WATCHER" "$EMITTERS" 2>/dev/null | awk -F: '{s+=$2} END {print s+0}')
check "no emit still uses the silent \`|| true\` form" "0" "$silent"

# fallback_outstanding_handlers()'s DISPATCH_DIR-queue loop (one of its two
# emit_task_file sites) was retired -- no queue to recover once run_handler_now()
# is synchronous. Its CLAIMS_DIR loop still applies: a SIGTERM can still
# interrupt a claim this watcher owns, and settle_own_claims_on_shutdown()
# (added to fix a real reviewer-found gap -- a dangling unsettled claim) is
# its synchronous-era successor, with its own single emit_task_file site. 1 is
# the correct count now, not 0.
check "no remaining shutdown call site references the retired async recovery path" \
      "1" "$(grep -cE '^\s+emit_task_file "\$[A-Za-z_]+"' "$WATCHER" || true)"
check "the handler-fallback site goes through its own emitter" \
      "1" "$(grep -cE '^\s+emit_fallback_task_file "\$[A-Za-z_]+"' "$WATCHER" || true)"

# The call sites above are worthless if the definitions never load. There is no
# `set -e` here, so a missing function is rc=127 and NON-FATAL: the watcher would
# drain on and silently emit nothing — the exact drop this suite exists to catch.
check "the watcher actually sources the emitters" \
      "1" "$(grep -c 'source "\$__SCRIPT_DIR/task-emit.sh"' "$WATCHER" || true)"
check "...and the file it sources defines both of them" \
      "2" "$(grep -cE '^(emit_task_file|emit_fallback_task_file)\(\)' "$EMITTERS" || true)"

# fd 9 must still be the stable dup of real stdout the shutdown emitter writes to.
grep -q '^exec 9>&1' "$WATCHER" \
    || { echo "  FAIL fd 9 is no longer a dup of stdout"; fail=1; }

# The fallback runs in normal drain on real stdout; borrowing fd 9 here would be
# a behaviour change, not a diagnostic.
fb_body=$(awk '/^emit_fallback_task_file\(\) \{/,/^\}/' "$EMITTERS")
# Non-vacuity: an empty extraction makes the >&9 assertion below pass for the
# wrong reason — absence of a function reads identically to a correct one.
check "the fallback emitter was actually found" \
      "1" "$(printf '%s\n' "$fb_body" | grep -c "printf 'TASK_FILE: " || true)"
check "the fallback emitter writes to stdout, not fd 9" \
      "0" "$(printf '%s\n' "$fb_body" | grep -c '>&9' || true)"

# ── behavioural: SOURCE the real emitters and drive both branches of each ──
TMPDIR_T="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_T"' EXIT

# Shutdown emitter: healthy fd 9, then closed fd 9.
{
    echo 'set -u'
    echo "source \"$EMITTERS\""
    echo 'exec 9>&1'
    echo 'emit_task_file "task-ok.txt"; echo "rc=$?"'
    echo 'exec 9>&-'
    echo 'emit_task_file "task-broken.txt"; echo "rc=$?"'
} > "$TMPDIR_T/shutdown.sh"

out="$(bash "$TMPDIR_T/shutdown.sh" 2>"$TMPDIR_T/err")"
err="$(cat "$TMPDIR_T/err")"

check "healthy fd 9 emits the TASK_FILE line" \
      "1" "$(printf '%s\n' "$out" | grep -c '^TASK_FILE: task-ok.txt$' || true)"
# Two rc=0 lines: the second is only reached if the FAILING call returned
# rather than exiting — that is the non-fatal half, pinned without a extra case.
check "both calls return 0 (non-fatal)" \
      "2" "$(printf '%s\n' "$out" | grep -c '^rc=0$' || true)"
check "a failed shutdown emit names the file on stderr" \
      "1" "$(printf '%s\n' "$err" | grep -c 'FAILED to emit TASK_FILE for task-broken.txt' || true)"
check "a failed shutdown emit puts nothing on stdout" \
      "0" "$(printf '%s\n' "$out" | grep -c 'task-broken.txt' || true)"

# Fallback emitter: healthy stdout, then closed stdout.
{
    echo 'set -u'
    echo "source \"$EMITTERS\""
    echo 'emit_fallback_task_file "task-fb-ok.txt"; echo "rc=$?"'
    echo 'exec 1>&-'
    echo 'emit_fallback_task_file "task-fb-broken.txt"; echo "rc=$?" >&2'
} > "$TMPDIR_T/fallback.sh"

out_fb="$(bash "$TMPDIR_T/fallback.sh" 2>"$TMPDIR_T/err-fb")"
err_fb="$(cat "$TMPDIR_T/err-fb")"

check "healthy stdout emits the fallback TASK_FILE line" \
      "1" "$(printf '%s\n' "$out_fb" | grep -c '^TASK_FILE: task-fb-ok.txt$' || true)"
check "a failed fallback emit names the file on stderr" \
      "1" "$(printf '%s\n' "$err_fb" | grep -c 'FAILED to emit TASK_FILE for task-fb-broken.txt' || true)"
check "a failed fallback emit is non-fatal (caller still runs)" \
      "1" "$(printf '%s\n' "$err_fb" | grep -c '^rc=0$' || true)"

# The two emitters must stay distinguishable in the log: the CI occurrence of
# #2934 fired on the fallback ("failed for"), not on a shutdown emit
# ("interrupted for"), and one shared message would erase that discriminator.
check "the two failure messages name different destinations" \
      "1" "$(printf '%s\n%s\n' "$err" "$err_fb" | grep -c 'on fd 9' || true)"
check "...and the fallback names stdout" \
      "1" "$(printf '%s\n%s\n' "$err" "$err_fb" | grep -c 'on stdout after a handler fallback' || true)"

# ── structural: cleanup() still has its remaining real anchors (#3025 retired) ──
# #3025's ordering concern was specifically about `$DISPATCH_DIR/shutting-down`
# racing the (now-removed) async drain/queue guards that read it (#2934). There
# is no more DISPATCH_DIR, no more async guard, and no more sentinel write to
# order — cleanup() no longer needs one at all, since run_handler_now() has
# nothing in flight for a signal to race against the way the background
# --handler-runner subprocess used to. What's left to check is simpler: the two
# anchors cleanup() still genuinely needs are present, in the order they always
# were (release the watcher's own PID sentinel, then kill the child processes).
cleanup_code=$(awk '/^cleanup\(\) \{/,/^\}/' "$WATCHER" | grep -vE '^[[:space:]]*#')
check "cleanup() body is extractable (else every ordering check below is vacuous)" \
      "yes" "$([ -n "$cleanup_code" ] && echo yes || echo no)"
check "cleanup() no longer references the retired DISPATCH_DIR shutdown sentinel" \
      "0" "$(printf '%s\n' "$cleanup_code" | grep -c 'shutting-down' || true)"

line_of() { printf '%s\n' "$cleanup_code" | grep -n -- "$1" | head -1 | cut -d: -f1; }
rel=$(line_of 'sentinel_release_if_owner')
kil=$(line_of 'kill -TERM')

for pair in "release:$rel" "kill:$kil"; do
    check "cleanup() still contains the ${pair%%:*} anchor" \
          "yes" "$([ -n "${pair#*:}" ] && echo yes || echo no)"
done

if [ -n "$rel" ] && [ -n "$kil" ]; then
    check "sentinel_release_if_owner precedes the first kill" \
          "yes" "$([ "$rel" -lt "$kil" ] && echo yes || echo no)"
fi

# Real shutdown recovery: the structural checks above accept any plain `"$var"`,
# so only an end-to-end TERM proves recovery names the payload, not the sentinel.
RTMP="$(mktemp -d)"
RWS="$RTMP/ws"; RINBOX="$RWS/deliveries/w-test"
mkdir -p "$RWS/tasks" "$RINBOX" "$RWS/results"
RPAYLOAD="$RWS/tasks/task-probe1.txt"
printf 'id: task-probe1\naccess_tier: owner\ntask: resolve me\n' > "$RPAYLOAD"
: > "$RINBOX/task-probe1.txt"          # the sentinel: zero bytes, by design

RRESOLVER="$RTMP/resolver.sh"
printf '#!/bin/sh\nprintf "%%s\\n" "%s"\n' "$RPAYLOAD" > "$RRESOLVER"; chmod +x "$RRESOLVER"

# Accepts the probe, then the real run only sleeps, so the task is still
# RUNNING when TERM lands: that forces the shutdown path, not the failure one.
RHANDLER="$RTMP/handler.sh"
cat > "$RHANDLER" << 'HEOF'
#!/bin/sh
for a in "$@"; do [ "$a" = "--probe" ] && exit 0; done
sleep 30
HEOF
chmod +x "$RHANDLER"

run_shutdown_sweep() {  # $1 = 1 to set SUTANDO_INBOX_RESOLVER, 0 to leave it unset
    local with_resolver="$1" outfile="$RTMP/sweep-$1.out" pid i resolver_env=""
    [ "$with_resolver" -eq 1 ] && resolver_env="$RRESOLVER"
    : > "$outfile"
    set -m
    SUTANDO_INBOX_RESOLVER="$resolver_env" SUTANDO_WORKSPACE_DIR="$RWS" \
      SUTANDO_RESULTS_DIR="$RWS/results" SUTANDO_INSTANCE="w-test-$1" \
      SUTANDO_TASK_EVENT_HANDLER="$RHANDLER" TMPDIR="$RTMP" \
      bash "$WATCHER" "$RINBOX" > "$outfile" 2>"$RTMP/sweep-$1.err" &
    pid=$!
    set +m
    # Wait for the handler to be RUNNING, not merely probed: TERM landing
    # before the marker moves leaves the sweep legitimately nothing to recover.
    for i in $(seq 1 60); do
        [ -n "$(ls "$RTMP"/sutando-task-dispatch.*/running/ 2>/dev/null)" ] && break
        sleep 0.1
    done
    kill -TERM -"$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null
    wait "$pid" 2>/dev/null
    grep 'TASK_FILE:' "$outfile" 2>/dev/null | tail -1
}

line_with_resolver="$(run_shutdown_sweep 1)"
echo "  shutdown recovery, resolver configured: ${line_with_resolver:-<nothing>}"
check "a resolver-configured shutdown recovery names the RESOLVED payload, not the sentinel" \
      "TASK_FILE: $RPAYLOAD" "$line_with_resolver"

# Control: no resolver must announce the bare basename. Without it, asserting
# "not the bare sentinel" would pass even if resolution never engaged.
: > "$RINBOX/task-probe1.txt"
line_no_resolver="$(run_shutdown_sweep 0)"
echo "  shutdown recovery, no resolver:          ${line_no_resolver:-<nothing>}"
check "...and with no resolver configured, the control still names the bare basename" \
      "TASK_FILE: task-probe1.txt" "$line_no_resolver"

rm -rf "$RTMP"

if [ "$fail" -ne 0 ]; then
    echo "Results: FAILED"; exit 1
fi
echo "Results: watch-tasks-stream emit diagnostics — all checks passed"
