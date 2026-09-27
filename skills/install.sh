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

# Skills outside the engine tree (workspace, external plugin dirs, sibling checkouts), in skill-roots
# order. A shipped skill wins a name collision; otherwise the first root holding the name does.
CONFIG="$(cd "$SKILLS_DIR/.." && pwd)/scripts/sutando-config.sh"
WS="$(bash "$CONFIG" workspace 2>/dev/null || true)"
ROOTS=""
[ -n "$WS" ] && ROOTS="$(bash "$CONFIG" skill-roots "$WS" 2>/dev/null || true)"
CLAIMED=$'\n'
while IFS= read -r root; do
  [ -n "$root" ] || continue
  [ "$(cd "$root" && pwd -P)" = "$(cd "$SKILLS_DIR" && pwd -P)" ] && continue
  if [ "$root" = "$WS/skills" ]; then kind="workspace"; else kind="$root"; fi
  for skill_dir in "$root"/*/; do
    [ -d "$skill_dir" ] || continue
    skill_name=$(basename "$skill_dir")
    [ ! -f "$skill_dir/SKILL.md" ] && continue
    if [ -d "$SKILLS_DIR/$skill_name" ] && [ -f "$SKILLS_DIR/$skill_name/SKILL.md" ]; then
      echo "  ⚠ $skill_name ($kind copy shadowed by the shipped skill of the same name — rename yours)"
      continue
    fi
    case "$CLAIMED" in *$'\n'"$skill_name"$'\n'*)
      echo "  ⚠ $skill_name ($kind copy shadowed by an earlier skill root)"; continue ;;
    esac
    CLAIMED="$CLAIMED$skill_name"$'\n'
    label="$kind skill"; [ "$kind" = "workspace" ] || label="skill from $kind"
    if [ -L "$TARGET/$skill_name" ] && [ "$(readlink "$TARGET/$skill_name")" = "${skill_dir%/}" ]; then
      echo "  ↻ $skill_name ($label, symlink exists)"
    elif [ -L "$TARGET/$skill_name" ] && [ ! -e "$TARGET/$skill_name" ]; then
      rm "$TARGET/$skill_name"; ln -s "${skill_dir%/}" "$TARGET/$skill_name"
      echo "  ✓ $skill_name ($label, relinked — old symlink was broken)"
    elif [ -L "$TARGET/$skill_name" ] && [ ! -f "$TARGET/$skill_name/SKILL.md" ]; then
      # A removed skill can leave its folder behind (untracked files), so the old link still resolves.
      rm "$TARGET/$skill_name"; ln -s "${skill_dir%/}" "$TARGET/$skill_name"
      echo "  ✓ $skill_name ($label, relinked — old link pointed at a folder with no SKILL.md)"
    elif [ -e "$TARGET/$skill_name" ]; then
      echo "  ⚠ $skill_name ($label; target exists, skipping)"
    else
      ln -s "${skill_dir%/}" "$TARGET/$skill_name"
      echo "  ✓ $skill_name ($label)"
    fi
  done
done <<< "$ROOTS"

echo ""
echo "Installed. Skills available in any Claude Code session."
