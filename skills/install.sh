#!/bin/bash
# Install Sutando skills into Claude Code ($CLAUDE_CONFIG_DIR/skills/).
# Creates symlinks so updates to the repo are picked up automatically.
# Resolves the target via the M0 claude-home-path helper so claude-sutando
# users get their workspace-scoped CCD honored.

set -e

SKILLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="$(bash "$(cd "$SKILLS_DIR/.." && pwd)/scripts/sutando-config.sh" claude-home-path skills)"

mkdir -p "$TARGET"

for skill_dir in "$SKILLS_DIR"/*/; do
  skill_name=$(basename "$skill_dir")
  [ "$skill_name" = "install.sh" ] && continue
  [ ! -f "$skill_dir/SKILL.md" ] && continue

  if [ -L "$TARGET/$skill_name" ] && [ ! -e "$TARGET/$skill_name" ]; then
    rm "$TARGET/$skill_name"
    ln -s "$skill_dir" "$TARGET/$skill_name"
    echo "  ✓ $skill_name (relinked — old symlink was broken)"
  elif [ -L "$TARGET/$skill_name" ]; then
    echo "  ↻ $skill_name (symlink exists)"
  elif [ -d "$TARGET/$skill_name" ]; then
    echo "  ⚠ $skill_name (directory exists, skipping — remove manually to reinstall)"
  else
    ln -s "$skill_dir" "$TARGET/$skill_name"
    echo "  ✓ $skill_name"
  fi
done

# The owner's own skills live in <workspace>/skills/, the folder that survives an engine
# update (the engine tree is replaced). A shipped skill wins a name collision.
WS="$(bash "$(cd "$SKILLS_DIR/.." && pwd)/scripts/sutando-config.sh" workspace 2>/dev/null || true)"
if [ -n "$WS" ] && [ -d "$WS/skills" ]; then
  for skill_dir in "$WS"/skills/*/; do
    [ -d "$skill_dir" ] || continue
    skill_name=$(basename "$skill_dir")
    [ ! -f "$skill_dir/SKILL.md" ] && continue
    if [ -d "$SKILLS_DIR/$skill_name" ] && [ -f "$SKILLS_DIR/$skill_name/SKILL.md" ]; then
      echo "  ⚠ $skill_name (workspace copy shadowed by the shipped skill of the same name — rename yours)"
      continue
    fi
    if [ -L "$TARGET/$skill_name" ] && [ "$(readlink "$TARGET/$skill_name")" = "${skill_dir%/}" ]; then
      echo "  ↻ $skill_name (workspace skill, symlink exists)"
    elif [ -L "$TARGET/$skill_name" ] && [ ! -e "$TARGET/$skill_name" ]; then
      rm "$TARGET/$skill_name"; ln -s "${skill_dir%/}" "$TARGET/$skill_name"
      echo "  ✓ $skill_name (workspace skill, relinked — old symlink was broken)"
    elif [ -e "$TARGET/$skill_name" ]; then
      echo "  ⚠ $skill_name (workspace skill; target exists, skipping)"
    else
      ln -s "${skill_dir%/}" "$TARGET/$skill_name"
      echo "  ✓ $skill_name (workspace skill)"
    fi
  done
fi

echo ""
# One release of alias: `room-doc` was renamed `room-collab`. A seat that still
# says /room-doc gets the new skill; a broken old link is replaced, a real dir is left alone.
if [ -e "$TARGET/room-collab" ]; then
  if [ -L "$TARGET/room-doc" ] || [ ! -e "$TARGET/room-doc" ]; then
    ln -sfn "$TARGET/room-collab" "$TARGET/room-doc"
    echo "  ↪ room-doc → room-collab (alias)"
  fi
fi

echo "Installed. Skills available in any Claude Code session."
