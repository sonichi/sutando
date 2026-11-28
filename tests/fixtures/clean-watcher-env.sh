# shellcheck shell=bash
# Sourced by a shell test that launches the real src/watch-tasks-stream.sh: drops
# the names tests/fixtures/clean_watcher_env.py drops, so a worker seat's env never leaks in.
for __clean_var in $(env | awk -F= '/^(SUTANDO_|TMUX|GIT_)/ {print $1}'); do
  unset "$__clean_var"
done
unset AGENT_ID AG2_AGENT_NAME CLAUDECODE __clean_var
