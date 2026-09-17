#!/bin/bash
# The watcher/notifier shells must reach python through the SHARED resolver.
#
# `scripts/python-binary.sh` exists because a bare `python3` on a clean Mac can
# be Apple's CLT stub, which raises a modal before it can fail. A caller that
# resolves the safe interpreter and then shells `python3` anyway gets the stub
# on exactly the hosts the resolver was written for.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok    %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL  %s\n' "$1"; }

SHELLS=(src/watch-tasks-stream.sh src/watcher_sentinel.sh src/agent/codex/cli/task-notifier.sh)

# 1. structural: no bare `python3 ` invocation survives in any of the three.
for f in "${SHELLS[@]}"; do
  # A leading $ or " means it is already a resolved variable; a # means a comment.
  hits="$(grep -nE '(^|[^"$/[:alnum:]_-])python3 ' "$REPO/$f" | grep -vE '^\s*[0-9]+:\s*#' | wc -l | tr -d ' ')"
  [ "$hits" = "0" ] && ok "$f invokes no bare python3" || bad "$f still has $hits bare python3 call(s)"
done

# 2. the control that matters: configured-good interpreter, BROKEN python3 on PATH.
REAL_PY="$(command -v python3)"
TD="$(mktemp -d)"; trap 'rm -rf "$TD"' EXIT
printf '#!/bin/sh\necho "stub: refusing" >&2\nexit 1\n' > "$TD/python3"
chmod +x "$TD/python3"

out="$(PATH="$TD:$PATH" SUTANDO_PY="$REAL_PY" bash -c '
  . "'"$REPO"'/src/watcher_sentinel.sh" >/dev/null 2>&1 || true
  sentinel_path_for "'"$TD"'/state" 2>&1')"
rc=$?
if [ "$rc" = "0" ] && [ -n "$out" ] && ! printf '%s' "$out" | grep -q "refusing"; then
  ok "watcher_sentinel resolves through SUTANDO_PY with a broken PATH python3"
else
  bad "watcher_sentinel fell through to the PATH python3 (rc=$rc out=$out)"
fi

# 3. the negative control: with NO configured interpreter and a broken PATH one,
#    it must REFUSE rather than shell the broken binary.
out2="$(PATH="$TD:/usr/bin:/bin" SUTANDO_PY= bash -c '
  . "'"$REPO"'/src/watcher_sentinel.sh" >/dev/null 2>&1 || true
  sentinel_path_for "'"$TD"'/state" 2>&1'; echo "rc=$?")"
if printf '%s' "$out2" | grep -q "rc=0"; then
  bad "it returned success with no runnable interpreter"
else
  ok "no runnable interpreter refuses instead of guessing a path"
fi

printf '\nPASSED: %s\nFAILED: %s\n' "$PASS" "$FAIL"
[ "$FAIL" = "0" ]
