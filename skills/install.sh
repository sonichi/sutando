#!/bin/bash
# Install Sutando skills into Claude Code ($CLAUDE_CONFIG_DIR/skills/) and, when the core
# runtime is Codex, into $CODEX_HOME/skills/. Symlinks, so repo updates are picked up.
# Targets resolve via the claude-home-path / codex-home-path helpers so claude-sutando
# users get their workspace-scoped CCD honored.

set -e

SKILLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$(cd "$SKILLS_DIR/.." && pwd)/scripts/sutando-config.sh"
TARGET="$(bash "$CONFIG" claude-home-path skills)"
# The owner's own skills live in <workspace>/skills/, the folder that survives an engine
# update (the engine tree is replaced). A shipped skill wins a name collision.
WS="$(bash "$CONFIG" workspace 2>/dev/null || true)"

link_skills() {
  local TARGET="$1" skill_dir skill_name
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
}

link_skills "$TARGET"
echo ""
echo "Installed. Skills available in any Claude Code session."

# Codex scans $CODEX_HOME/skills and follows symlinks; its bundled skills sit in .system/
# and same-named entries already there are left alone (link_skills never replaces one).
if [ "$(bash "$CONFIG" core-runtime 2>/dev/null || true)" = "codex" ]; then
  CODEX_TARGET="$(bash "$CONFIG" codex-home-path skills)"
  echo ""
  echo "Codex core runtime — linking into $CODEX_TARGET"
  link_skills "$CODEX_TARGET"
  echo ""
  echo "Installed. Skills available in the next Codex session."
fi
