#!/bin/bash
# A worker session's cwd is the spawner's --cwd and need not be the repo, so
# `/startup --worker` cannot reach the watcher by a relative path (rc 127) and
# must not call a bare `python3` (the CLT stub on a clean Mac). The launcher is
# the edge that knows both absolutely, so it forwards them — and ONLY to a
# worker: a core launch's env must stay byte-identical (#4215's invariance).
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
pass=0; fail=0
check() { if [ "$1" = "0" ]; then echo "  ok  $2"; pass=$((pass+1)); else echo "  FAIL $2"; fail=$((fail+1)); fi; }
STARTCLI="$REPO/src/agent/claude/cli/start-cli.sh"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin"; for stub in lsof launchctl; do printf '#!/bin/sh\nexit 1\n' > "$TMP/bin/$stub"; done
chmod +x "$TMP"/bin/*
STUB_PATH="$TMP/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# `--print-core-env` is `printf '%s\n' "${CORE_ENV_ARGS[@]}"`, so each element is
# its own line and a value containing spaces stays whole. Never split on spaces.
env_value() { printf '%s\n' "$1" | grep "^$2=" | head -1; }
# The launcher forwards the PHYSICAL path (pwd -P), so expected values are
# canonicalised the same way: $TMP itself sits behind /var -> /private/var on macOS.
canon() { printf '%s/%s' "$(cd "${1%/*}" && pwd -P)" "${1##*/}"; }

# An executable interpreter the launcher must forward verbatim. Injected rather
# than inherited: the host's own resolution is not what this case measures.
INJECTED_PY="$TMP/bin/python3-injected"
printf '#!/bin/sh\nexit 0\n' > "$INJECTED_PY"; chmod +x "$INJECTED_PY"

worker_env="$(env -i HOME="$HOME" PATH="$STUB_PATH" SUTANDO_INSTANCE_ID=w-test \
    SUTANDO_PY="$INJECTED_PY" bash "$STARTCLI" --print-core-env 2>/dev/null)"
core_env="$(env -i HOME="$HOME" PATH="$STUB_PATH" SUTANDO_PY="$INJECTED_PY" \
    bash "$STARTCLI" --print-core-env 2>/dev/null)"

got="$(env_value "$worker_env" SUTANDO_WATCHER_CMD)"
echo "  worker: ${got:-<absent>}"
case "$got" in
  "SUTANDO_WATCHER_CMD=$REPO/src/watch-tasks-stream.sh") check 0 "worker is handed the repo-absolute watcher path" ;;
  *) check 1 "worker is handed the repo-absolute watcher path" ;;
esac
# It must be absolute AND exist, or naming it bought nothing.
case "$got" in *=/*) [ -f "${got#*=}" ]; check $? "the forwarded watcher path exists" ;;
               *) check 1 "the forwarded watcher path exists" ;; esac

py="$(env_value "$worker_env" SUTANDO_PY)"
echo "  worker: ${py:-<absent>}"
[ "$py" = "SUTANDO_PY=$(canon "$INJECTED_PY")" ] && [ -x "${py#*=}" ]
check $? "worker is handed the executable interpreter as its canonical path, not a bare name"

# A checkout path with a space is the DEFAULT macOS install location, and no CI
# runner has one — so the parser is pinned here instead of by where this runs.
spaced="$(printf '%s\n' "-e" "SUTANDO_WATCHER_CMD=/Library/Application Support/x/src/watch-tasks-stream.sh" "-e" "SUTANDO_PY=/x/python3")"
[ "$(env_value "$spaced" SUTANDO_WATCHER_CMD)" = "SUTANDO_WATCHER_CMD=/Library/Application Support/x/src/watch-tasks-stream.sh" ]
check $? "a value containing spaces survives the parse whole"

# resolve_python also probes <repo>/../runtime/python absolutely, which a PATH
# fixture cannot close: absence needs a launcher copy with no runtime/ sibling.
mkdir -p "$TMP/noclt"
for _d in /usr/bin /bin; do
  [ -d "$_d" ] || continue
  for _f in "$_d"/*; do
    _b=${_f##*/}
    case "$_b" in python*|pydoc*|idle*) continue ;; esac
    [ -e "$TMP/noclt/$_b" ] || ln -s "$_f" "$TMP/noclt/$_b" 2>/dev/null
  done
done
NOPY_REPO="$TMP/norepo/sutando"
mkdir -p "$NOPY_REPO/src/agent/claude/cli" "$NOPY_REPO/src/agent" "$NOPY_REPO/scripts"
cp "$STARTCLI" "$NOPY_REPO/src/agent/claude/cli/start-cli.sh"
cp "$REPO/scripts/python-binary.sh" "$NOPY_REPO/scripts/" 2>/dev/null
for _dep in "$REPO/src/agent"/*.sh; do [ -f "$_dep" ] && cp "$_dep" "$NOPY_REPO/src/agent/"; done
[ -d "$TMP/norepo/runtime/python" ] && { echo "  fixture invalid: a runtime/ sibling exists"; exit 1; }
noclt_env="$(env -i HOME="$HOME" PATH="$TMP/noclt" \
    SUTANDO_INSTANCE_ID=w-test bash "$NOPY_REPO/src/agent/claude/cli/start-cli.sh" \
    --print-core-env 2>/dev/null)"
noclt_py="$(env_value "$noclt_env" SUTANDO_PY)"
noclt_watcher="$(env_value "$noclt_env" SUTANDO_WATCHER_CMD)"
echo "  no-interpreter worker: py=${noclt_py:-<absent>} watcher=${noclt_watcher:+present}"
case "$noclt_py" in
  "SUTANDO_PY=")  bare=0 ;;
  SUTANDO_PY=/*)  [ -x "${noclt_py#*=}" ]; bare=$? ;;
  *)              bare=1 ;;
esac
[ "$bare" -eq 0 ] && [ -n "$noclt_watcher" ]
check $? "no-interpreter host: never a bare interpreter name, and the watcher is still named"
# An EMPTY override, not an absent one: tmux hands a worker the server's global
# env, so a launch that omits -e leaves a stale SUTANDO_PY from an earlier core in force.
[ "$noclt_py" = "SUTANDO_PY=" ]
check $? "no runnable interpreter: an empty SUTANDO_PY override is emitted, never nothing"

# The forwarded interpreter is CANONICAL: a relative override is executable from
# the launcher's cwd only, and `..` cannot be trusted from the spawner's --cwd.
mkdir -p "$TMP/relpy"; printf '#!/bin/sh\nexit 0\n' > "$TMP/relpy/python3"; chmod +x "$TMP/relpy/python3"
ln -s "$TMP/relpy" "$REPO/.relpy-test-$$" 2>/dev/null
rel_env="$(env -i HOME="$HOME" PATH="$STUB_PATH" SUTANDO_INSTANCE_ID=w-test \
    SUTANDO_PY=".relpy-test-$$/python3" bash "$STARTCLI" --print-core-env 2>/dev/null)"
rm -f "$REPO/.relpy-test-$$"
rel_py="$(env_value "$rel_env" SUTANDO_PY)"; echo "  relative override -> ${rel_py:-<absent>}"
case "$rel_py" in SUTANDO_PY=/*) [ -x "${rel_py#*=}" ] && [ "${rel_py#*=}" = "$(canon "$TMP/relpy/python3")" ] ;; *) false ;; esac
check $? "a relative SUTANDO_PY is forwarded as its canonical absolute path"
dot_env="$(env -i HOME="$HOME" PATH="$STUB_PATH" SUTANDO_INSTANCE_ID=w-test \
    SUTANDO_PY="$TMP/relpy/../relpy/python3" bash "$STARTCLI" --print-core-env 2>/dev/null)"
dot_py="$(env_value "$dot_env" SUTANDO_PY)"; echo "  dot-dot override -> ${dot_py:-<absent>}"
[ "$dot_py" = "SUTANDO_PY=$(canon "$TMP/relpy/python3")" ] && case "$dot_py" in *"/../"*) false ;; *) true ;; esac
check $? "a dot-dot SUTANDO_PY is forwarded canonical, without the .."

# --print-core-env is a PROBE. It used to run the hook installer first, so every
# call in this file wrote <checkout>/.claude/settings.json (kewei, #4235).
SETTINGS="$REPO/.claude/settings.json"
snap() { [ -f "$SETTINGS" ] && cksum < "$SETTINGS" || echo ABSENT; }
s_before="$(snap)"
env -i HOME="$HOME" PATH="$STUB_PATH" SUTANDO_INSTANCE_ID=w-test bash "$STARTCLI" --print-core-env >/dev/null 2>&1
[ "$(snap)" = "$s_before" ]
check $? "--print-core-env leaves the checkout's .claude/settings.json untouched"

# The control that makes the cases above a real result: a core launch gets neither.
if ! printf '%s\n' "$core_env" | grep -qE '^SUTANDO_(WATCHER_CMD|PY)='; then
  check 0 "core launch forwards neither — env invariance holds"
else
  echo "    core env leaked: $(printf '%s\n' "$core_env" | grep -E '^SUTANDO_(WATCHER_CMD|PY)=')"
  check 1 "core launch forwards neither — env invariance holds"
fi

echo
if [ "$fail" -eq 0 ]; then echo "PASS — $pass checks green"; else echo "FAIL — $fail failed, $pass passed"; exit 1; fi
