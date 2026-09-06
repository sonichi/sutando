# One cloud seat of a Sutando agent: the gateway client in cloud mode plus a
# pluggable seat runtime. Context = repo root; provision.sh stages only the COPY sources.
FROM python:3.12-slim AS base

ARG ENGINE_SHA=""
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SUTANDO_ENGINE_DIR=/app/engine \
    SUTANDO_CLOUD_WORKSPACE=/workspace

RUN useradd --system --uid 1000 --home-dir /srv/sutando --create-home sutando \
 && mkdir -p /app/engine /workspace \
 && chown -R sutando:sutando /app /workspace

# ACP client for the ag2-assistant seat (the SDK ag2-assistant itself speaks); [http] = websockets.
RUN pip install --no-cache-dir --root-user-action=ignore 'agent-client-protocol[http]==0.12.1'

WORKDIR /app/engine

# Exactly the client's import graph (tests/cloud-worker-container.test.sh pins
# this list): package, loader shim + its src/ modules, the workspace resolver.
COPY --chown=sutando:sutando sutando.config.json ./
COPY --chown=sutando:sutando packages/ag2-sparrow/ag2_sparrow/ packages/ag2-sparrow/ag2_sparrow/
COPY --chown=sutando:sutando src/remote-gateway-bridge.py src/task_archive.py src/workspace_default.py src/sutando_config.py src/util_paths.py src/proactive_routing.py src/task_envelope.py src/git_binary.py src/result_markers.py src/
COPY --chown=sutando:sutando scripts/sutando-config.sh scripts/python-binary.sh scripts/
COPY --chown=sutando:sutando deploy/cloud-worker/entrypoint.sh deploy/cloud-worker/seat-stub.py deploy/cloud-worker/seat-ag2-assistant.py deploy/cloud-worker/healthcheck.sh deploy/cloud-worker/

# No git in the image: the client's runtime self-report reads the manifest sha.
RUN if [ -n "$ENGINE_SHA" ]; then printf '{"sha": "%s"}\n' "$ENGINE_SHA" > ENGINE_MANIFEST.json; fi

USER sutando
VOLUME ["/workspace"]
HEALTHCHECK --interval=60s --timeout=10s --start-period=45s --retries=3 \
  CMD ["bash", "/app/engine/deploy/cloud-worker/healthcheck.sh"]
ENTRYPOINT ["bash", "/app/engine/deploy/cloud-worker/entrypoint.sh"]

# Opt-in seat runtime (`--target claude`): the Claude Code CLI via its native
# installer, as the seat user. Login is per container — README.md "Runtime: claude".
FROM base AS claude
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*
USER sutando
RUN curl -fsSL https://claude.ai/install.sh | bash
ENV PATH="/srv/sutando/.local/bin:${PATH}"
