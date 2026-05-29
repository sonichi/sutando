#!/bin/bash
# One-shot fix for Mac Mini after migration bundle setup
# Usage: git pull && bash src/fix-setup.sh

echo "Fixing setup..."
REPO="$HOME/Desktop/sutando"

# Find and copy identity files from wherever the bundle extracted
for d in ~/Downloads ~/Desktop ~/Downloads/sutando-migration ~/Desktop/sutando-migration; do
  for f in stand-identity.json stand-avatar.png .env; do
    [ -f "$d/$f" ] && [ ! -f "$REPO/$f" ] && cp "$d/$f" "$REPO/" && echo "  ✓ $f"
  done
done

# Ensure PATH has Claude Code
export PATH="$HOME/.local/bin:$PATH"
grep -q '.local/bin' ~/.zshrc 2>/dev/null || echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc

# Source nvm
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"

echo ""
echo "Done. Next: claude auth login"
