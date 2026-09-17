#!/usr/bin/env bash
# A checkout whose src/watch-tasks-stream.sh is a harmless sleeper and whose every
# other src/ entry and scripts/ are the real ones, linked. Sourcing the sandbox's
# startup-runtime.sh resolves the sandbox as the repo, so "this checkout's
# watcher" is the sleeper and any other path is a foreign checkout's.
#
#   make_sandbox_checkout <sandbox-dir> <real-repo>
#   spawn_sandbox_watcher <script>   -> pid, on its own, argv "bash <script>"
make_sandbox_checkout() {
  local sb="$1" real="$2" e
  mkdir -p "$sb/src"
  for e in "$real"/src/* "$real"/src/.[!.]*; do
    [ -e "$e" ] || continue
    ln -s "$e" "$sb/src/$(basename "$e")"
  done
  rm -f "$sb/src/watch-tasks-stream.sh"
  ln -s "$real/scripts" "$sb/scripts"
  write_sandbox_watcher "$sb/src/watch-tasks-stream.sh"
}

# /bin/sleep by absolute path so a stubbed `sleep` on PATH cannot busy-loop it;
# no TERM trap, so a signal ends it at once.
write_sandbox_watcher() {
  printf '#!/usr/bin/env bash\nwhile :; do /bin/sleep 0.2; done\n' > "$1"
  chmod +x "$1"
}

# stdout/stderr redirected: a caller wraps this in $( ), and a child inheriting
# that pipe would hold the substitution open for its whole life.
spawn_sandbox_watcher() {
  local script="$1" pid
  bash "$script" >/dev/null 2>&1 & pid=$!
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    ps -p "$pid" -o args= 2>/dev/null | grep -q "watch-tasks-stream" && break
    sleep 0.1
  done
  printf '%s' "$pid"
}
