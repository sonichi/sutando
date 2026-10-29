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
REPO="$(cd "$SKILLS_DIR/.." && pwd)"
. "$REPO/scripts/python-binary.sh"
PY="$(resolve_python "$REPO")"
[ -n "$PY" ] || { echo "skills/install.sh: no runnable python3 to decide which skills are developer-only; linking nothing" >&2; exit 1; }

# "<name>\t<reason>" per skill this install must not link (src/skill_audience.py owns the rule).
excluded_in() {
  local root="$1" dirs=()
  for d in "$root"/*/; do [ -f "$d/SKILL.md" ] && dirs+=("${d%/}"); done
  [ "${#dirs[@]}" -gt 0 ] || return 0
  "$PY" "$REPO/src/skill_audience.py" excluded "${dirs[@]}"
}
REPO_EXCLUDED="$(excluded_in "$SKILLS_DIR")" || { echo "skills/install.sh: audience check failed; linking nothing" >&2; exit 1; }
WS_EXCLUDED=""
if [ -n "$WS" ] && [ -d "$WS/skills" ]; then
  WS_EXCLUDED="$(excluded_in "$WS/skills")" || { echo "skills/install.sh: audience check failed; linking nothing" >&2; exit 1; }
fi

# Prints the reason when $2 names a skill in the excluded list $1, else nothing.
excluded_reason() {
  printf '%s\n' "$1" | awk -F'\t' -v n="$2" '$1 == n { print $2; exit }'
}

# An excluded skill is never linked; one already linked is left in place, with a note.
skip_excluded() {
  local reason="$1" target="$2" name="$3" label="$4"
  if [ -L "$target/$name" ] || [ -e "$target/$name" ]; then
    echo "  ⚠ $name ($label$reason; already installed, left in place — remove it yourself, or set skills.developer)"
  else
    echo "  - $name ($label$reason; not installed — set skills.developer to opt in)"
  fi
}

link_skills() {
  local TARGET="$1" skill_dir skill_name reason
  mkdir -p "$TARGET"

  for skill_dir in "$SKILLS_DIR"/*/; do
    skill_name=$(basename "$skill_dir")
    [ "$skill_name" = "install.sh" ] && continue
    [ ! -f "$skill_dir/SKILL.md" ] && continue
    reason="$(excluded_reason "$REPO_EXCLUDED" "$skill_name")"
    if [ -n "$reason" ]; then skip_excluded "$reason" "$TARGET" "$skill_name" ""; continue; fi

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
      reason="$(excluded_reason "$WS_EXCLUDED" "$skill_name")"
      if [ -n "$reason" ]; then skip_excluded "$reason" "$TARGET" "$skill_name" "workspace skill, "; continue; fi
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
