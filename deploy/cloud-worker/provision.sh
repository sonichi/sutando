#!/usr/bin/env bash
# provision.sh <worker-id> <env-file> [--build] [--target claude] [--with-assistant] [-- <docker run args>]
# provision.sh --build-only [--target claude]      (target defaults to base)
# Idempotent: builds/pulls the image if absent, then creates or starts the
# container sutando-worker-<id> on volume sutando-worker-<id>:/workspace. With
# --with-assistant (implied by SUTANDO_WORKER_RUNTIME=ag2-assistant in the env
# file) it also runs the ag2-assistant sidecar sutando-assistant-<id> on a
# per-user network, alias `assistant`, forwarding only its own keys to it.
# Exit: 0 running/built, 2 usage, 3 env file unreadable or missing a key the
#       sidecar needs, 4 docker unavailable, 5 image unavailable, 6 a container did not start.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
IMAGE="${SUTANDO_CLOUD_WORKER_IMAGE:-sutando-cloud-worker:local}"
ASSISTANT_IMAGE="${SUTANDO_ASSISTANT_IMAGE:-ghcr.io/ag2ai/ag2-assistant:latest}"
DOCKERFILE_REL="deploy/cloud-worker/Dockerfile"

usage() { sed -n '2,10p' "${BASH_SOURCE[0]}" >&2; exit 2; }

WORKER_ID=""; ENV_FILE=""; BUILD=0; BUILD_ONLY=0; TARGET="base"; WITH_ASSISTANT=0; EXTRA=()
while [ $# -gt 0 ]; do
  case "$1" in
    --build) BUILD=1; shift ;;
    --build-only) BUILD=1; BUILD_ONLY=1; shift ;;
    --target) [ $# -ge 2 ] || usage; TARGET="$2"; shift 2 ;;
    --with-assistant) WITH_ASSISTANT=1; shift ;;
    --) shift; EXTRA=("$@"); break ;;
    -h|--help) usage ;;
    -*) echo "provision: unknown option $1" >&2; usage ;;
    *)
      if [ -z "$WORKER_ID" ]; then WORKER_ID="$1"
      elif [ -z "$ENV_FILE" ]; then ENV_FILE="$1"
      else echo "provision: unexpected argument $1" >&2; usage; fi
      shift ;;
  esac
done

if [ "$BUILD_ONLY" -eq 0 ]; then
  [ -n "$WORKER_ID" ] && [ -n "$ENV_FILE" ] || usage
  if ! printf '%s' "$WORKER_ID" | grep -Eq '^[A-Za-z0-9_-]{1,32}$'; then
    echo "provision: worker id must match [A-Za-z0-9_-]{1,32}, got '$WORKER_ID'" >&2; exit 2
  fi
  if [ ! -r "$ENV_FILE" ]; then
    echo "provision: env file not readable: $ENV_FILE" >&2; exit 3
  fi
fi

# Last `KEY=value` line of the env file (docker --env-file syntax), or "".
env_value() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2-; }
if [ "$BUILD_ONLY" -eq 0 ] && [ "$(env_value SUTANDO_WORKER_RUNTIME)" = "ag2-assistant" ]; then
  WITH_ASSISTANT=1
fi
if [ "$WITH_ASSISTANT" -eq 1 ] && [ -z "$(env_value AG2ASSISTANT_ACP_TOKEN)" ]; then
  echo "provision: the ag2-assistant sidecar needs AG2ASSISTANT_ACP_TOKEN in $ENV_FILE" >&2; exit 3
fi

if ! docker info >/dev/null 2>&1; then
  echo "provision: docker is not available (daemon down or not installed)" >&2; exit 4
fi

# Build context = the Dockerfile's own COPY sources, staged into a scratch dir.
# The working tree (and any workspace under it) never reaches the daemon.
copy_sources() {
  grep -E '^COPY ' "$REPO/$DOCKERFILE_REL" \
    | sed -E 's/--[a-z]+=[^ ]+ //g' \
    | awk '{ for (i = 2; i < NF; i++) print $i }'
}

build_image() {
  local sha ctx rc sources=()
  sha="$(git -C "$REPO" rev-parse HEAD 2>/dev/null || true)"
  while IFS= read -r p; do sources+=("$p"); done < <(copy_sources)
  ctx="$(mktemp -d "${TMPDIR:-/tmp}/sutando-cloud-worker-ctx.XXXXXX")" || return 1
  echo "provision: building $IMAGE (target $TARGET) from ${#sources[@]} COPY sources, sha=${sha:-none}"
  tar -C "$REPO" --exclude='__pycache__' --exclude='*.pyc' -cf - "${sources[@]}" "$DOCKERFILE_REL" \
    | tar -C "$ctx" -xf -
  docker build -f "$ctx/$DOCKERFILE_REL" --build-arg "ENGINE_SHA=$sha" \
    --target "$TARGET" -t "$IMAGE" "$ctx"
  rc=$?
  rm -rf "$ctx"
  return $rc
}

ensure_image() {
  if [ "$BUILD" -eq 0 ] && docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "provision: image $IMAGE present"; return 0
  fi
  if [ "$BUILD" -eq 0 ] && docker pull "$IMAGE" >/dev/null 2>&1; then
    echo "provision: pulled $IMAGE"; return 0
  fi
  build_image && return 0
  echo "provision: image $IMAGE unavailable — pull and build both failed" >&2
  return 5
}

ensure_image || exit $?
[ "$BUILD_ONLY" -eq 1 ] && exit 0

NAME="sutando-worker-$WORKER_ID"
VOLUME="$NAME"
NET_ARGS=()
if [ "$WITH_ASSISTANT" -eq 1 ]; then
  NET="$NAME"; ASSISTANT="sutando-assistant-$WORKER_ID"
  if ! docker network inspect "$NET" >/dev/null 2>&1; then
    docker network create "$NET" >/dev/null || { echo "provision: could not create network $NET" >&2; exit 6; }
  fi
  if docker container inspect "$ASSISTANT" >/dev/null 2>&1; then
    if [ "$(docker container inspect -f '{{.State.Status}}' "$ASSISTANT")" != running ]; then
      docker start "$ASSISTANT" >/dev/null || { echo "provision: could not start $ASSISTANT" >&2; exit 6; }
      echo "provision: started existing $ASSISTANT"
    else
      echo "provision: $ASSISTANT already running"
    fi
  else
    # Only the sidecar's own keys reach it — never the broker token.
    side_env="$(mktemp "${TMPDIR:-/tmp}/sutando-assistant-env.XXXXXX")"
    grep -E '^(AG2ASSISTANT_ACP_TOKEN|GEMINI_API_KEY|OPENAI_API_KEY|ANTHROPIC_API_KEY|TZ)=' "$ENV_FILE" > "$side_env"
    if ! docker run -d --name "$ASSISTANT" --restart unless-stopped \
        --network "$NET" --network-alias assistant --env-file "$side_env" \
        -v "$ASSISTANT-data:/data" -v "$ASSISTANT-workspace:/workspace" \
        -v "$HERE/assistant-bootstrap.sh:/bootstrap.sh:ro" \
        --entrypoint sh "$ASSISTANT_IMAGE" /bootstrap.sh >/dev/null; then
      rm -f "$side_env"; echo "provision: docker run failed for $ASSISTANT" >&2; exit 6
    fi
    rm -f "$side_env"
    echo "provision: created $ASSISTANT (image $ASSISTANT_IMAGE, network $NET, alias assistant)"
  fi
  NET_ARGS=(--network "$NET" -e AG2ASSISTANT_ACP_URL=ws://assistant:8802)
fi
# Typed inspect: a bare `docker inspect <name>` also matches the same-named
# network/volume. Existence first — `-f` on a missing name prints a blank line.
if docker container inspect "$NAME" >/dev/null 2>&1; then
  state="$(docker container inspect -f '{{.State.Status}}' "$NAME" 2>/dev/null)"
else
  state=absent
fi
case "$state" in
  running)
    echo "provision: $NAME already running"; exit 0 ;;
  absent)
    if ! docker run -d --name "$NAME" --restart unless-stopped \
        --env-file "$ENV_FILE" -e "SUTANDO_WORKER_ID=$WORKER_ID" -e SUTANDO_WORKER_LOCATION=cloud \
        -v "$VOLUME:/workspace" ${NET_ARGS[@]+"${NET_ARGS[@]}"} ${EXTRA[@]+"${EXTRA[@]}"} "$IMAGE" >/dev/null; then
      echo "provision: docker run failed for $NAME" >&2; exit 6
    fi
    echo "provision: created $NAME (volume $VOLUME, image $IMAGE)" ;;
  *)
    if ! docker start "$NAME" >/dev/null; then
      echo "provision: could not start existing $NAME (state $state)" >&2; exit 6
    fi
    echo "provision: started existing $NAME (was $state)" ;;
esac
docker container inspect -f 'provision: {{.Name}} state={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}n/a{{end}}' "$NAME" 2>/dev/null || true
exit 0
