#!/bin/bash
# Core-monitor log policy shared by the runtime launchers: each workspace's own logs/, never a
# shared /tmp file, and a log that cannot be made is /dev/null rather than a refused start.

# sutando_core_workspace_log <workspace> <name>: prints <workspace>/logs/<name>, making logs/;
# prints /dev/null when logs/ cannot be made, so the caller still starts what it would log.
sutando_core_workspace_log() {
  if mkdir -p "$1/logs" 2>/dev/null; then
    printf '%s/logs/%s' "$1" "$2"
  else
    printf '/dev/null'
  fi
}
