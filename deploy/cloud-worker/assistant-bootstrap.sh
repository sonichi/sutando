#!/bin/sh
# Runs INSIDE the ag2-assistant sidecar (mounted at /bootstrap.sh): first-boot
# profile + provider-key seed, then the ACP WebSocket listener. POSIX sh.
set -eu
[ -n "${AG2ASSISTANT_ACP_TOKEN:-}" ] || { echo "assistant-bootstrap: AG2ASSISTANT_ACP_TOKEN is required" >&2; exit 2; }
[ -s /data/profiles.json ] || ag2-assistant profiles create backup
# acp-serve gates sessions on the secret store, not the process env: seed it.
python - <<'PY'
import os
from pathlib import Path
from assistant.paths import Paths
from assistant.secrets import SecretStore
store = SecretStore(Paths.from_env(os.environ, Path.home()))
for provider, var in (("gemini", "GEMINI_API_KEY"), ("openai", "OPENAI_API_KEY"), ("anthropic", "ANTHROPIC_API_KEY")):
    if os.environ.get(var):
        store.set_key(provider, os.environ[var])
        print(f"assistant-bootstrap: {var} seeded into the secret store", flush=True)
PY
exec ag2-assistant acp-serve --host 0.0.0.0 --port "${AG2ASSISTANT_ACP_PORT:-8802}" --token "$AG2ASSISTANT_ACP_TOKEN"
