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

worker_env="$(env -i HOME="$HOME" PATH="$STUB_PATH" SUTANDO_INSTANCE_ID=w-test \
    bash "$STARTCLI" --print-core-env 2>/dev/null)"
core_env="$(env -i HOME="$HOME" PATH="$STUB_PATH" \
    bash "$STARTCLI" --print-core-env 2>/dev/null)"

got="$(printf '%s\n' "$worker_env" | tr ' ' '\n' | grep '^SUTANDO_WATCHER_CMD=' | head -1)"
echo "  worker: ${got:-<absent>}"
case "$got" in
  "SUTANDO_WATCHER_CMD=$REPO/src/watch-tasks-stream.sh") check 0 "worker is handed the repo-absolute watcher path" ;;
  *) check 1 "worker is handed the repo-absolute watcher path" ;;
esac
# It must be absolute AND exist, or naming it bought nothing.
case "$got" in *=/*) [ -f "${got#*=}" ]; check $? "the forwarded watcher path exists" ;;
               *) check 1 "the forwarded watcher path exists" ;; esac

py="$(printf '%s\n' "$worker_env" | tr ' ' '\n' | grep '^SUTANDO_PY=' | head -1)"
echo "  worker: ${py:-<absent>}"
case "$py" in *=/*) [ -x "${py#*=}" ]; check $? "worker is handed an executable interpreter, not a bare name" ;;
               *) check 1 "worker is handed an executable interpreter, not a bare name" ;; esac

# The control that makes the two above a real result: a core launch gets neither.
printf '%s\n' "$core_env" | tr ' ' '\n' | grep -qE '^SUTANDO_(WATCHER_CMD|PY)='
if [ $? -ne 0 ]; then check 0 "core launch forwards neither — env invariance holds"; else
  echo "    core env leaked: $(printf '%s\n' "$core_env" | tr ' ' '\n' | grep -E '^SUTANDO_(WATCHER_CMD|PY)=')"
  check 1 "core launch forwards neither — env invariance holds"; fi

echo
if [ "$fail" -eq 0 ]; then echo "PASS — $pass checks green"; else echo "FAIL — $fail failed, $pass passed"; exit 1; fi
