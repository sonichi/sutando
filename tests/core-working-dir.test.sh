#!/usr/bin/env bash
# scripts/core-working-dir.sh — the one resolver for SUTANDO_CLAUDE_WORKING_DIR.
#
# Two halves. (1) The contract on the four input shapes. (2) Delegation: every site that
# decides or targets the core's launch dir sources the resolver and none keeps a private
# `${v/#\~/$HOME}` expansion — four private copies disagreed on `~user` (the launcher created
# and launched into $HOME + "user/…" while the installer refused it), which is the defect.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
RESOLVER="$REPO/scripts/core-working-dir.sh"
unset SUTANDO_CLAUDE_WORKING_DIR

pass=0; fail=0
ok() { if [ "$2" = 0 ]; then echo "ok   $1"; pass=$((pass+1)); else echo "FAIL $1"; fail=$((fail+1)); fi; }

ROOT="$(mktemp -d "${TMPDIR:-/tmp}/core cwd test.XXXXXX")"
export HOME="$ROOT/home"; mkdir -p "$HOME"
. "$RESOLVER"

# --- 1. contract ---------------------------------------------------------------
ok "unset: prints the default unchanged" "$([ "$(sutando_core_working_dir "$ROOT/default")" = "$ROOT/default" ] && echo 0 || echo 1)"
ok "empty: prints the default unchanged" "$([ "$(SUTANDO_CLAUDE_WORKING_DIR= sutando_core_working_dir "$ROOT/default")" = "$ROOT/default" ] && echo 0 || echo 1)"
OUT="$(SUTANDO_CLAUDE_WORKING_DIR="$ROOT/abs dir" sutando_core_working_dir "$ROOT/default")"; RC=$?
ok "absolute: created and printed physically" "$([ $RC = 0 ] && [ -d "$ROOT/abs dir" ] && [ "$OUT" = "$(cd "$ROOT/abs dir" && pwd -P)" ] && echo 0 || echo 1)"
OUT="$(SUTANDO_CLAUDE_WORKING_DIR="~/core home" sutando_core_working_dir "$ROOT/default")"; RC=$?
ok "~/: resolves under HOME" "$([ $RC = 0 ] && [ "$OUT" = "$(cd "$HOME/core home" && pwd -P)" ] && echo 0 || echo 1)"
ERR="$(SUTANDO_CLAUDE_WORKING_DIR="~someoneelse/core" sutando_core_working_dir "$ROOT/default" 2>&1 >/dev/null)"; RC=$?
ok "~user: refused (rc 1)" "$([ $RC = 1 ] && echo 0 || echo 1)"
ok "~user: message names the contract" "$(echo "$ERR" | grep -q "absolute path or start with ~/" && echo 0 || echo 1)"
ok "~user: nothing created (no HOME+user mangling)" "$([ ! -e "$HOME/someoneelse" ] && [ ! -e "${HOME}someoneelse" ] && echo 0 || echo 1)"
SUTANDO_CLAUDE_WORKING_DIR="relative/dir" sutando_core_working_dir "$ROOT/default" >/dev/null 2>&1; RC=$?
ok "relative: refused (rc 1)" "$([ $RC = 1 ] && echo 0 || echo 1)"
ok "relative: nothing created" "$([ ! -e "$PWD/relative" ] && echo 0 || echo 1)"

# --- 2. delegation: every deciding site sources the resolver, none keeps a private copy -------
SITES=(src/agent/claude/cli/start-cli.sh src/install-claude-hooks.sh scripts/install-personal-claude-hook.sh scripts/install-session-start-hook.sh)
for s in "${SITES[@]}"; do
  ok "$s sources scripts/core-working-dir.sh" "$(grep -q 'scripts/core-working-dir.sh' "$REPO/$s" && echo 0 || echo 1)"
  ok "$s calls sutando_core_working_dir" "$(grep -q 'sutando_core_working_dir' "$REPO/$s" && echo 0 || echo 1)"
  ok "$s keeps no private tilde expansion" "$(grep -q '/#\\~/' "$REPO/$s" && echo 1 || echo 0)"
done
# --- 3. the python mirror AGREES with the bash resolver, shape for shape ---------------------
# The probe cannot source bash, so _hook_settings_target is a second implementation. A comment
# naming the resolver pins nothing; this runs both over one table. Agreement means: the same
# path, or the same REFUSAL (bash rc 1 <-> python None) — never "refused there, repo here".
ok "health-check names the shared resolver as the contract it mirrors" "$(grep -q 'core-working-dir.sh' "$REPO/src/health-check.py" && echo 0 || echo 1)"
py_target() {  # py_target <override|-unset-> -> printed path, or REFUSED, or ERROR
  if [ "$1" = "-unset-" ]; then env -u SUTANDO_CLAUDE_WORKING_DIR "$PYBIN" "$PYPROBE" "$ROOT/default"
  else SUTANDO_CLAUDE_WORKING_DIR="$1" "$PYBIN" "$PYPROBE" "$ROOT/default"; fi
}
sh_target() {  # sh_target <override|-unset-> -> printed path, or REFUSED
  if [ "$1" = "-unset-" ]; then out="$(env -u SUTANDO_CLAUDE_WORKING_DIR bash -c ". '$RESOLVER'; sutando_core_working_dir '$ROOT/default'" 2>/dev/null)" && printf '%s' "$out" || echo REFUSED
  else out="$(SUTANDO_CLAUDE_WORKING_DIR="$1" bash -c ". '$RESOLVER'; sutando_core_working_dir '$ROOT/default'" 2>/dev/null)" && printf '%s' "$out" || echo REFUSED; fi
}
PYBIN="$(command -v python3)"
PYPROBE="$ROOT/probe.py"
cat > "$PYPROBE" <<PY
import importlib.util, sys, pathlib
spec = importlib.util.spec_from_file_location("hc", "$REPO/src/health-check.py")
m = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(m)
except SystemExit:
    pass
t = m._hook_settings_target(pathlib.Path(sys.argv[1]))
print("REFUSED" if t is None else str(t))
PY
mkdir -p "$ROOT/abs dir" "$ROOT/default"  # the default exists too, so every path compares physically
for shape in "-unset-" "$ROOT/abs dir" "~/core home" "~someoneelse/core" "relative/dir"; do
  S="$(sh_target "$shape")"; P="$(py_target "$shape")"
  # bash prints the physical path; python resolves too — compare physically when both are paths.
  if [ "$S" != REFUSED ] && [ "$P" != REFUSED ]; then S="$(cd "$S" 2>/dev/null && pwd -P || echo "$S")"; P="$(cd "$P" 2>/dev/null && pwd -P || echo "$P")"; fi
  ok "agreement on '$shape': bash='$S' python='$P'" "$([ "$S" = "$P" ] && echo 0 || echo 1)"
done
ok "the refused forms are refused on BOTH sides (not repo on one)" \
   "$([ "$(sh_target '~someoneelse/core')" = REFUSED ] && [ "$(py_target '~someoneelse/core')" = REFUSED ] && [ "$(sh_target 'relative/dir')" = REFUSED ] && [ "$(py_target 'relative/dir')" = REFUSED ] && echo 0 || echo 1)"

rm -rf "$ROOT"
echo "---"
if [ "$fail" -gt 0 ]; then echo "FAILED — $fail of $((pass+fail)) checks"; exit 1; fi
echo "PASS — core-working-dir ($pass checks)"
