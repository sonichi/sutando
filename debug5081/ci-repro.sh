#!/usr/bin/env bash
# Linux evidence for #5081. usage: ci-repro.sh <tree> <rounds> <copies> <hogs> <test>...
# Runs <copies> concurrent copies of each test per round under <hogs> CPU hogs; prints
# per-run seconds and the failure count. Output of failed runs is printed in full.
set -u
TREE="$1"; ROUNDS="$2"; COPIES="$3"; HOGS="$4"; shift 4
hp=(); for _ in $(seq 1 "$HOGS"); do ( while :; do :; done ) & hp+=($!); done
trap 'kill "${hp[@]}" 2>/dev/null' EXIT
bad=0; total=0; OUT="$(mktemp -d)"
for r in $(seq 1 "$ROUNDS"); do
  pids=(); names=(); starts=()
  for t in "$@"; do for c in $(seq 1 "$COPIES"); do
    n="$(basename "$t" .test.sh)-r$r-c$c"
    ( s=$SECONDS; bash "$TREE/$t" > "$OUT/$n.out" 2>&1; rc=$?; echo "$rc $((SECONDS - s))" > "$OUT/$n.rc" ) &
    pids+=($!); names+=("$n")
  done; done
  for k in "${!pids[@]}"; do
    wait "${pids[$k]}"; read -r rc secs < "$OUT/${names[$k]}.rc"; total=$((total + 1))
    if [ "$rc" -ne 0 ]; then bad=$((bad + 1)); echo "FAIL ${names[$k]} rc=$rc ${secs}s"; sed 's/^/    /' "$OUT/${names[$k]}.out"; else echo "ok   ${names[$k]} ${secs}s"; fi
  done
done
echo "RESULT tree=$TREE: $bad failed of $total runs"
