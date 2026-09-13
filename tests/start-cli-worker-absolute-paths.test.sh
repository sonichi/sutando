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
[ "$py" = "SUTANDO_PY=$INJECTED_PY" ] && [ -x "${py#*=}" ]
check $? "worker is handed the executable interpreter verbatim, not a bare name"

# A checkout path with a space is the DEFAULT macOS install location, and no CI
# runner has one — so the parser is pinned here instead of by where this runs.
spaced="$(printf '%s\n' "-e" "SUTANDO_WATCHER_CMD=/Library/Application Support/x/src/watch-tasks-stream.sh" "-e" "SUTANDO_PY=/x/python3")"
[ "$(env_value "$spaced" SUTANDO_WATCHER_CMD)" = "SUTANDO_WATCHER_CMD=/Library/Application Support/x/src/watch-tasks-stream.sh" ]
check $? "a value containing spaces survives the parse whole"

# Fail closed: the launcher must never hand a worker a BARE interpreter name.
# The CLT-stub rule is macOS-only by design in _sutando_safe_path_python, and
# $OSTYPE is shell-set so a PATH stub cannot fake the platform — so the absence
# half is asserted only where production applies it.
mkdir -p "$TMP/noclt"; printf '#!/bin/sh\nexit 1\n' > "$TMP/noclt/xcode-select"
for stub in lsof launchctl; do printf '#!/bin/sh\nexit 1\n' > "$TMP/noclt/$stub"; done
chmod +x "$TMP"/noclt/*
noclt_env="$(env -i HOME="$HOME" PATH="$TMP/noclt:/usr/bin:/bin:/usr/sbin:/sbin" \
    SUTANDO_INSTANCE_ID=w-test bash "$STARTCLI" --print-core-env 2>/dev/null)"
noclt_py="$(env_value "$noclt_env" SUTANDO_PY)"
noclt_watcher="$(env_value "$noclt_env" SUTANDO_WATCHER_CMD)"
echo "  no-CLT worker: py=${noclt_py:-<absent>} watcher=${noclt_watcher:+present}"
case "$noclt_py" in
  "")            bare=0 ;;
  SUTANDO_PY=/*) [ -x "${noclt_py#*=}" ]; bare=$? ;;
  *)             bare=1 ;;
esac
[ "$bare" -eq 0 ] && [ -n "$noclt_watcher" ]
check $? "no-CLT host: never a bare interpreter name, and the watcher is still named"
case "${OSTYPE:-$(uname -s 2>/dev/null)}" in
  darwin*|Darwin)
    [ -z "$noclt_py" ]
    check $? "macOS no-CLT: no SUTANDO_PY forwarded at all" ;;
  *)
    echo "  (absence half skipped: the CLT-stub rule is macOS-only in _sutando_safe_path_python)" ;;
esac

# The control that makes the cases above a real result: a core launch gets neither.
if ! printf '%s\n' "$core_env" | grep -qE '^SUTANDO_(WATCHER_CMD|PY)='; then
  check 0 "core launch forwards neither — env invariance holds"
else
  echo "    core env leaked: $(printf '%s\n' "$core_env" | grep -E '^SUTANDO_(WATCHER_CMD|PY)=')"
  check 1 "core launch forwards neither — env invariance holds"
fi

echo
if [ "$fail" -eq 0 ]; then echo "PASS — $pass checks green"; else echo "FAIL — $fail failed, $pass passed"; exit 1; fi
