#!/bin/bash
# Sutando Migration Script — bundle current machine state for transfer to new Mac
#
# Usage:
#   bash src/migrate.sh              # creates ~/Desktop/sutando-migration.tar.gz
#   # Transfer to new Mac, then:
#   bash setup-new-mac.sh            # runs on new machine

set -e
REPO="$(cd "$(dirname "$0")/.." && pwd)"
BUNDLE="$HOME/Desktop/sutando-migration"
rm -rf "$BUNDLE"
mkdir -p "$BUNDLE"

echo "=== Sutando Migration Bundle ==="

# Generate fresh session state snapshot
if [ -f "$REPO/src/session-handoff.sh" ]; then
  echo "Generating session snapshot..."
  bash "$REPO/src/session-handoff.sh" 2>/dev/null || true
fi

echo "Collecting files..."

# 1. Environment (secrets)
cp "$REPO/.env" "$BUNDLE/.env" 2>/dev/null && echo "  ✓ .env"

# 2. Memory system
MEMORY_DIR="$HOME/.claude/projects/-Users-$(whoami)-Desktop-sutando/memory"
if [ -d "$MEMORY_DIR" ]; then
  cp -r "$MEMORY_DIR" "$BUNDLE/memory"
  echo "  ✓ memory ($(ls "$MEMORY_DIR"/*.md 2>/dev/null | wc -l) files)"
fi

# 3. Claude Code settings
if [ -d "$HOME/.claude" ]; then
  mkdir -p "$BUNDLE/claude-config"
  cp "$HOME/.claude/settings.json" "$BUNDLE/claude-config/" 2>/dev/null
  cp -r "$HOME/.claude/channels" "$BUNDLE/claude-config/channels" 2>/dev/null
  cp -r "$HOME/.claude/skills" "$BUNDLE/claude-config/skills" 2>/dev/null
  echo "  ✓ claude config (settings, channels, skills)"
fi

# 4. Gitignored runtime files
for f in stand-identity.json tab-aliases.json PERSONAL_CLAUDE.md; do
  [ -f "$REPO/$f" ] && cp "$REPO/$f" "$BUNDLE/" && echo "  ✓ $f"
done

# 5. Google credentials (gmail — encrypted creds + token cache)
if [ -d "$HOME/.config/gws" ]; then
  mkdir -p "$BUNDLE/gws"
  for f in client_secret.json credentials.enc .encryption_key token_cache.json; do
    [ -f "$HOME/.config/gws/$f" ] && cp "$HOME/.config/gws/$f" "$BUNDLE/gws/" && echo "  ✓ gws/$f"
  done
fi

# 6. Personal scripts (Zacks, etc.)
if [ -d "$HOME/scripts/sutando-personal" ]; then
  cp -r "$HOME/scripts/sutando-personal" "$BUNDLE/sutando-personal"
  echo "  ✓ sutando-personal scripts"
fi

# 7. Claude Code session (for --resume on new machine)
SESSION_DIR="$HOME/.claude/projects/-Users-$(whoami)-Desktop-sutando"
if [ -d "$SESSION_DIR" ]; then
  mkdir -p "$BUNDLE/session"
  # Copy most recent session JSONL (the active one)
  LATEST=$(ls -t "$SESSION_DIR"/*.jsonl 2>/dev/null | head -1)
  if [ -n "$LATEST" ]; then
    cp "$LATEST" "$BUNDLE/session/"
    SESSION_ID=$(basename "$LATEST" .jsonl)
    echo "$SESSION_ID" > "$BUNDLE/session/session-id.txt"
    echo "  ✓ session transcript ($(du -h "$LATEST" | cut -f1)) — ID: $SESSION_ID"
  fi
  # Copy session index if exists
  [ -f "$SESSION_DIR/sessions-index.json" ] && cp "$SESSION_DIR/sessions-index.json" "$BUNDLE/session/"
fi

# 8. Behavioral flywheel data (conversation history, call logs, build log)
mkdir -p "$BUNDLE/flywheel"
[ -f "$REPO/session-state.md" ] && cp "$REPO/session-state.md" "$BUNDLE/flywheel/" && echo "  ✓ session-state.md"
[ -f "$REPO/conversation.log" ] && cp "$REPO/conversation.log" "$BUNDLE/flywheel/" && echo "  ✓ conversation.log"
[ -f "$REPO/build_log.md" ] && cp "$REPO/build_log.md" "$BUNDLE/flywheel/" && echo "  ✓ build_log.md"
[ -d "$REPO/results/calls" ] && cp -r "$REPO/results/calls" "$BUNDLE/flywheel/calls" && echo "  ✓ call transcripts"
# Task result history (recent)
if [ -d "$REPO/results" ]; then
  mkdir -p "$BUNDLE/flywheel/results"
  find "$REPO/results" -name "task-*.txt" -maxdepth 1 | head -100 | while read f; do cp "$f" "$BUNDLE/flywheel/results/"; done
  echo "  ✓ task results (recent)"
fi

# 8. Generate setup script for new machine
cat > "$BUNDLE/setup-new-mac.sh" << 'SETUP'
#!/bin/bash
# Sutando New Mac Setup — run after transferring migration bundle
set -e

echo "=== Sutando New Mac Setup ==="

# Install prerequisites
echo "Installing prerequisites..."
which brew >/dev/null || /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
brew install fswatch ffmpeg python3 node 2>/dev/null || true

# Install Claude Code (native installer — recommended, auto-updates)
echo "Installing Claude Code..."
curl -fsSL https://claude.ai/install.sh | bash

# Clone repo
REPO="$HOME/Desktop/sutando"
if [ ! -d "$REPO" ]; then
  git clone https://github.com/sonichi/sutando.git "$REPO"
fi
cd "$REPO"

# Install npm deps
npm install

# Copy .env
BUNDLE_DIR="$(cd "$(dirname "$0")" && pwd)"
[ -f "$BUNDLE_DIR/.env" ] && cp "$BUNDLE_DIR/.env" "$REPO/.env" && echo "  ✓ .env restored"

# Copy memory
if [ -d "$BUNDLE_DIR/memory" ]; then
  MEMORY_DIR="$HOME/.claude/projects/-Users-$(whoami)-Desktop-sutando/memory"
  mkdir -p "$MEMORY_DIR"
  cp -r "$BUNDLE_DIR/memory/"* "$MEMORY_DIR/"
  echo "  ✓ memory restored"
fi

# Copy claude config
if [ -d "$BUNDLE_DIR/claude-config" ]; then
  mkdir -p "$HOME/.claude"
  [ -f "$BUNDLE_DIR/claude-config/settings.json" ] && cp "$BUNDLE_DIR/claude-config/settings.json" "$HOME/.claude/"
  [ -d "$BUNDLE_DIR/claude-config/channels" ] && cp -r "$BUNDLE_DIR/claude-config/channels" "$HOME/.claude/"
  [ -d "$BUNDLE_DIR/claude-config/skills" ] && cp -r "$BUNDLE_DIR/claude-config/skills" "$HOME/.claude/"
  echo "  ✓ claude config restored"
fi

# Copy gitignored files
for f in stand-identity.json tab-aliases.json PERSONAL_CLAUDE.md; do
  [ -f "$BUNDLE_DIR/$f" ] && cp "$BUNDLE_DIR/$f" "$REPO/" && echo "  ✓ $f restored"
done

# Copy Google credentials (gmail)
if [ -d "$BUNDLE_DIR/gws" ]; then
  mkdir -p "$HOME/.config/gws"
  cp "$BUNDLE_DIR/gws/"* "$HOME/.config/gws/"
  chmod 600 "$HOME/.config/gws/.encryption_key" "$HOME/.config/gws/credentials.enc" 2>/dev/null
  echo "  ✓ Google gmail credentials restored"
fi

# Copy personal scripts
if [ -d "$BUNDLE_DIR/sutando-personal" ]; then
  mkdir -p "$HOME/scripts"
  cp -r "$BUNDLE_DIR/sutando-personal" "$HOME/scripts/sutando-personal"
  echo "  ✓ Personal scripts restored"
fi

# Restore Claude Code session
if [ -d "$BUNDLE_DIR/session" ]; then
  SESSION_DIR="$HOME/.claude/projects/-Users-$(whoami)-Desktop-sutando"
  mkdir -p "$SESSION_DIR"
  cp "$BUNDLE_DIR/session/"*.jsonl "$SESSION_DIR/" 2>/dev/null
  [ -f "$BUNDLE_DIR/session/sessions-index.json" ] && cp "$BUNDLE_DIR/session/sessions-index.json" "$SESSION_DIR/"
  if [ -f "$BUNDLE_DIR/session/session-id.txt" ]; then
    SID=$(cat "$BUNDLE_DIR/session/session-id.txt")
    echo "  ✓ Session restored — try: claude --resume $SID"
  fi
fi

# Restore flywheel data
if [ -d "$BUNDLE_DIR/flywheel" ]; then
  [ -f "$BUNDLE_DIR/flywheel/session-state.md" ] && cp "$BUNDLE_DIR/flywheel/session-state.md" "$REPO/" && echo "  ✓ session-state.md restored"
  [ -f "$BUNDLE_DIR/flywheel/conversation.log" ] && cp "$BUNDLE_DIR/flywheel/conversation.log" "$REPO/" && echo "  ✓ conversation.log restored"
  [ -f "$BUNDLE_DIR/flywheel/build_log.md" ] && cp "$BUNDLE_DIR/flywheel/build_log.md" "$REPO/" && echo "  ✓ build_log.md restored"
  [ -d "$BUNDLE_DIR/flywheel/calls" ] && mkdir -p "$REPO/results" && cp -r "$BUNDLE_DIR/flywheel/calls" "$REPO/results/calls" && echo "  ✓ call transcripts restored"
  [ -d "$BUNDLE_DIR/flywheel/results" ] && mkdir -p "$REPO/results" && cp "$BUNDLE_DIR/flywheel/results/"* "$REPO/results/" 2>/dev/null && echo "  ✓ task results restored"
fi

# Compile Sutando app
echo "Compiling Sutando menu bar app..."
cd "$REPO/src/Sutando"
swiftc -O -o Sutando main.swift -framework Cocoa -framework Carbon -framework ApplicationServices 2>/dev/null && echo "  ✓ Sutando compiled" || echo "  ⚠ Compile failed — run manually"
cd "$REPO"

# Python deps
pip3 install google-genai discord.py python-telegram-bot Pillow 2>/dev/null || true

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Run: bash src/startup.sh"
echo "  2. Grant macOS permissions when prompted (Screen Recording, Accessibility, Notifications)"
echo "  3. Start the proactive loop: /proactive-loop"
echo ""
echo "Note: Google Calendar uses macOS keyring — run the calendar script once to re-authenticate."
echo "      Gmail (gws) credentials are transferred but may need token refresh."
SETUP
chmod +x "$BUNDLE/setup-new-mac.sh"

# Create tarball
cd "$HOME/Desktop"
tar czf sutando-migration.tar.gz -C "$BUNDLE" .
echo ""
echo "=== Bundle created ==="
echo "  $(du -h "$HOME/Desktop/sutando-migration.tar.gz" | cut -f1) → ~/Desktop/sutando-migration.tar.gz"
echo ""
echo "Transfer to new Mac, extract, and run: bash setup-new-mac.sh"

# Cleanup
rm -rf "$BUNDLE"
