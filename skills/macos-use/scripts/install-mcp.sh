#!/usr/bin/env bash
# Register mcp-server-macos-use as an MCP server in Claude Code's settings.
#
# Adds the binary under ~/.macos-use-mcp/.build/release/mcp-server-macos-use
# to ~/.claude.json's mcpServers map. Idempotent — won't duplicate entries.
#
# Usage:
#   bash skills/macos-use/scripts/install-mcp.sh [--user|--project]
#
# --user    (default) registers in ~/.claude.json
# --project registers in ./.mcp.json (current repo only)

set -euo pipefail

BINARY="${HOME}/.macos-use-mcp/.build/release/mcp-server-macos-use"
SCOPE="user"
CONFIG_FILE="${HOME}/.claude.json"

for arg in "$@"; do
    case "$arg" in
        --project) SCOPE="project"; CONFIG_FILE="$(pwd)/.mcp.json" ;;
        --user) SCOPE="user"; CONFIG_FILE="${HOME}/.claude.json" ;;
        *) ;;
    esac
done

if [[ ! -x "$BINARY" ]]; then
    echo "install-mcp: binary not found at $BINARY" >&2
    echo "  Run bash skills/macos-use/scripts/build.sh first." >&2
    exit 2
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "install-mcp: python3 required for JSON manipulation" >&2
    exit 2
fi

# Pass values via env vars instead of heredoc interpolation so quotes /
# special characters in $CONFIG_FILE or $BINARY can't break the Python source.
MACOS_USE_CONFIG="$CONFIG_FILE" \
MACOS_USE_BINARY="$BINARY" \
MACOS_USE_SCOPE="$SCOPE" \
python3 - <<'PY'
import json
import os
import sys
import tempfile
from pathlib import Path

config_path = Path(os.environ["MACOS_USE_CONFIG"])
binary = os.environ["MACOS_USE_BINARY"]
scope = os.environ["MACOS_USE_SCOPE"]

if config_path.exists():
    try:
        cfg = json.loads(config_path.read_text())
    except json.JSONDecodeError as e:
        print(f"install-mcp: cannot parse {config_path}: {e}", file=sys.stderr)
        sys.exit(3)
else:
    cfg = {}

cfg.setdefault("mcpServers", {})
existing = cfg["mcpServers"].get("macos-use")
desired = {
    "command": binary,
    "args": [],
}

if existing == desired:
    print(f"✓ macos-use already registered in {config_path}")
else:
    cfg["mcpServers"]["macos-use"] = desired
    config_path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: tmp file in same dir + rename, so a crash mid-write
    # can't leave a corrupted ~/.claude.json.
    new_text = json.dumps(cfg, indent=2) + "\n"
    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=config_path.name + ".", suffix=".tmp", dir=str(config_path.parent)
    )
    try:
        with os.fdopen(tmp_fd, "w") as fh:
            fh.write(new_text)
        os.replace(tmp_path, config_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    action = "updated" if existing else "added"
    print(f"✓ macos-use {action} in {config_path} ({scope} scope)")
    print()
    print("Restart Claude Code for the new MCP server to load.")
    print("Tools will appear as mcp__macos-use__*:")
    print("  - mcp__macos-use__open_application_and_traverse")
    print("  - mcp__macos-use__click_and_traverse")
    print("  - mcp__macos-use__type_and_traverse")
    print("  - mcp__macos-use__press_key_and_traverse")
    print("  - mcp__macos-use__scroll_and_traverse")
    print("  - mcp__macos-use__refresh_traversal")
PY
