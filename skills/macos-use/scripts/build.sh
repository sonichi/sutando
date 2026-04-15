#!/usr/bin/env bash
# Build mediar-ai mcp-server-macos-use from source with the Swift 5 workaround.
#
# Why the flag: mcp-server-macos-use's transitive dep `swift-sdk` has
# data-race errors that Swift 6.3+ strict-concurrency trips on, even
# though Package.swift pins tools-version 5.9. `-Xswiftc -swift-version
# -Xswiftc 5` forces the compiler back into Swift 5 mode for the build.
# When upstream fixes the races, drop the flag.
#
# Usage:
#   bash skills/macos-use/scripts/build.sh [--force]
#
# Idempotent: if the binary exists and is newer than the repo mtime, skips.

set -euo pipefail

REPO_URL="https://github.com/mediar-ai/mcp-server-macos-use.git"
CLONE_DIR="${HOME}/.macos-use-mcp"
BIN_NAME="mcp-server-macos-use"
FORCE=false

for arg in "$@"; do
    case "$arg" in
        --force) FORCE=true ;;
        *) ;;
    esac
done

if ! command -v xcrun >/dev/null 2>&1; then
    echo "build.sh: xcrun not found — install Xcode command line tools: xcode-select --install" >&2
    exit 2
fi

if [[ ! -d "$CLONE_DIR/.git" ]]; then
    echo "→ cloning $REPO_URL to $CLONE_DIR"
    git clone --depth=1 "$REPO_URL" "$CLONE_DIR"
else
    if $FORCE; then
        echo "→ updating $CLONE_DIR"
        (cd "$CLONE_DIR" && git fetch --depth=1 origin && git reset --hard origin/HEAD)
    fi
fi

BINARY="$CLONE_DIR/.build/release/$BIN_NAME"

if [[ -f "$BINARY" ]] && ! $FORCE; then
    echo "✓ $BIN_NAME already built at $BINARY"
    echo "  (use --force to rebuild)"
    exit 0
fi

echo "→ building $BIN_NAME (~35s, release mode, swift-version 5 workaround)"
cd "$CLONE_DIR"
xcrun swift build -c release -Xswiftc -swift-version -Xswiftc 5 2>&1 | tail -5

if [[ ! -f "$BINARY" ]]; then
    echo "✗ build failed — binary not produced at $BINARY" >&2
    exit 1
fi

echo "✓ $BIN_NAME built at $BINARY"
echo ""
echo "Next steps:"
echo "  1. Grant Accessibility permission: System Settings → Privacy & Security → Accessibility"
echo "     Add $BINARY (drag from Finder or click +)"
echo "  2. Register the MCP server in Claude Code:"
echo "     bash skills/macos-use/scripts/install-mcp.sh"
echo "  3. Restart Claude Code for the MCP tools to appear"
