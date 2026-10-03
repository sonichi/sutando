#!/usr/bin/env bash
# The parent traps TERM like the watcher; a watchdog subshell in the shipped shape is forked
# and TERMed before it can run. A lost TERM shows as the watchdog sleeping out its bound.
trap 'echo "parent trap"' TERM
hogs="${2:-4}"; n="${1:-300}"; slow=0
hp=(); for _ in $(seq 1 "$hogs"); do ( while :; do :; done ) & hp+=($!); done
for i in $(seq 1 "$n"); do
  t0=$(date +%s%N)
  ( trap 'kill "${_s:-}" 2>/dev/null; exit 0' TERM; sleep 2 & _s=$!; wait "$_s" ) & p=$!
  kill -TERM "$p" 2>/dev/null
  wait "$p" 2>/dev/null
  ms=$(( ($(date +%s%N) - t0) / 1000000 ))
  [ "$ms" -gt 1000 ] && { slow=$((slow+1)); echo "iter $i: TERM lost, watchdog slept its bound (${ms}ms)"; }
done
kill "${hp[@]}" 2>/dev/null
echo "RESULT lost-signal: $slow of $n (bash $BASH_VERSION, $hogs hogs)"
