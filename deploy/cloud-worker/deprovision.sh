#!/usr/bin/env bash
# deprovision.sh <worker-id> [--purge]
# Idempotent: stops and removes sutando-worker-<id>, its ag2-assistant sidecar
# and per-user network if present; --purge also deletes the volumes (the seat's
# tasks, results, state, any CLI login, and the sidecar's /data + /workspace).
# Exit: 0 removed or already absent, 2 usage, 4 docker unavailable, 6 removal failed.
set -uo pipefail

WORKER_ID=""; PURGE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --purge) PURGE=1; shift ;;
    -h|--help) sed -n '2,5p' "${BASH_SOURCE[0]}" >&2; exit 2 ;;
    -*) echo "deprovision: unknown option $1" >&2; exit 2 ;;
    *) [ -z "$WORKER_ID" ] || { echo "deprovision: unexpected argument $1" >&2; exit 2; }
       WORKER_ID="$1"; shift ;;
  esac
done
[ -n "$WORKER_ID" ] || { sed -n '2,5p' "${BASH_SOURCE[0]}" >&2; exit 2; }
if ! printf '%s' "$WORKER_ID" | grep -Eq '^[A-Za-z0-9_-]{1,32}$'; then
  echo "deprovision: worker id must match [A-Za-z0-9_-]{1,32}, got '$WORKER_ID'" >&2; exit 2
fi
if ! docker info >/dev/null 2>&1; then
  echo "deprovision: docker is not available (daemon down or not installed)" >&2; exit 4
fi

NAME="sutando-worker-$WORKER_ID"
ASSISTANT="sutando-assistant-$WORKER_ID"
for c in "$NAME" "$ASSISTANT"; do
  if docker container inspect "$c" >/dev/null 2>&1; then
    docker rm -f "$c" >/dev/null || { echo "deprovision: could not remove $c" >&2; exit 6; }
    echo "deprovision: removed $c"
  else
    echo "deprovision: $c already absent"
  fi
done
if docker network inspect "$NAME" >/dev/null 2>&1; then
  docker network rm "$NAME" >/dev/null || { echo "deprovision: could not remove network $NAME" >&2; exit 6; }
  echo "deprovision: removed network $NAME"
fi
if [ "$PURGE" -eq 1 ]; then
  for v in "$NAME" "$ASSISTANT-data" "$ASSISTANT-workspace"; do
    if docker volume inspect "$v" >/dev/null 2>&1; then
      docker volume rm "$v" >/dev/null || { echo "deprovision: could not remove volume $v" >&2; exit 6; }
      echo "deprovision: removed volume $v"
    else
      echo "deprovision: volume $v already absent"
    fi
  done
fi
