#!/usr/bin/env bash
# Sutando lint: forbid NEW hardcoded ⌃-combo hotkey assertions in docs/comments.
#
# Hotkey bindings are CONFIGURABLE. The shipped defaults are published in
# `state/hotkeys.json` (the source of truth) and overridable per-machine via
# `~/.config/sutando/hotkeys.json`. Docs and code comments must therefore NOT
# assert a fixed keystroke as if it were the binding ("the ⌃C drop-context
# hotkey") — when the default changes (#1920/#1924 made these configurable),
# every such assertion silently goes stale (issue #1925).
#
# Instead, refer to the ACTION name (drop_context, toggle_voice, …) — the stable
# key in state/hotkeys.json — and, if you must name the keystroke, mark it as a
# *default* and point at the contract, e.g.:
#     "drop_context (default ⌃⇧C, see state/hotkeys.json)"
#
# A line that names a ⌃-combo AND references the contract (mentions
# `hotkeys.json`, or the words `default`/`configurable`) passes — that's the
# correct, non-stale framing. Only bare assertions are flagged.
#
# Allowed files — legitimately reference ⌃-combos:
#   README.md                     the sanctioned Keyboard-shortcuts table
#                                 (already framed as defaults sourced from
#                                 state/hotkeys.json)
#   src/Sutando/main.swift        code + the ⌃C/⌃R-shadow-SIGINT design
#                                 rationale (why, not a stale binding claim)
#   scripts/lint-hotkey-assertions.sh   this file
#   CHANGELOG*                    historical record, not live docs
#
# This lint catches new offenders on ADDED lines (PR diff). The sweep that
# introduces it clears existing ones; future contributors get a CI failure if
# they reintroduce the pattern. Owner directive 2026-07-14 (CR #1958:
# "make this general, not partial" — close the class, don't band-aid).
#
# Usage:
#   bash scripts/lint-hotkey-assertions.sh          # scan whole tree
#   bash scripts/lint-hotkey-assertions.sh --diff   # scan only added lines vs BASE_REF

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO_ROOT"

mode="${1:-all}"

# A control-combo hotkey token: ⌃ then optional ⇧/⌥/⌘ then a letter/digit.
PATTERN_HOTKEY='⌃[⇧⌥⌘]?[A-Za-z0-9]'

# Lines that also cite the contract are correctly framed — not stale assertions.
ESCAPE='hotkeys\.json|[Cc]onfigurable|[Dd]efault'

# Files allowed to reference ⌃-combos (sanctioned table / code-and-why design
# rationale). Per CR #1958 the owner exempts "code/why" — internal comments that
# explain an action's mechanics (e.g. the drop_video_clip two-press toggle state
# machine), as opposed to stale binding assertions in user-facing docs. These
# describe how the code works, not "the hotkey is X".
ALLOWED='^(README\.md|src/Sutando/main\.swift|src/screen-capture-server\.py|src/web-client\.ts|tests/.*|scripts/lint-hotkey-assertions\.sh|CHANGELOG.*)$'

if [[ "$mode" == "--diff" ]]; then
  base="${BASE_REF:-origin/main}"
  # NO `|| true` here. An empty $files is a legitimate result (this PR touched
  # nothing scannable) and exits 0 two lines below -- which is exactly why a
  # FAILED discovery must not also produce an empty $files. `set -e` aborts on a
  # failing command substitution in a plain assignment, so leaving this bare is
  # what keeps "git could not resolve $base" distinguishable from "clean". With
  # the `|| true` this line used to carry, a bad BASE_REF printed
  # `fatal: bad revision` to stderr and then `nothing to scan` with exit 0 --
  # a required gate reporting clean because it could not look. The three sibling
  # --diff lints are bare for the same reason; tests/lint-diff-discovery-failure.test.py
  # pins all four.
  files="$(git diff --name-only --diff-filter=AM "$base"...HEAD -- '*.md' '*.swift' '*.sh' '*.py' '*.ts')"
else
  files="$(git ls-files -- '*.md' '*.swift' '*.sh' '*.py' '*.ts')"
fi

if [[ -z "$files" ]]; then
  echo "lint-hotkey-assertions: nothing to scan (mode=$mode)"
  exit 0
fi

found=0
while IFS= read -r f; do
  [[ -f "$f" ]] || continue
  if [[ "$f" =~ $ALLOWED ]]; then
    continue
  fi
  if [[ "$mode" == "--diff" ]]; then
    # Only flag lines ADDED in the diff (git diff prefix '+', not context ' ').
    candidate="$(git diff "$base"...HEAD -- "$f" | awk '/^\+[^+]/ {print substr($0, 2)}')"
  else
    candidate="$(cat "$f")"
  fi
  # Flag lines that name a ⌃-combo but do NOT cite the hotkeys.json contract.
  matches="$(echo "$candidate" | grep -nE "$PATTERN_HOTKEY" | grep -vE "$ESCAPE" || true)"
  if [[ -n "$matches" ]]; then
    found=1
    echo "lint-hotkey-assertions: stale hardcoded hotkey assertion in $f"
    while IFS= read -r line; do
      echo "  $line"
    done <<< "$matches"
  fi
done <<< "$files"

if [[ "$found" -eq 1 ]]; then
  cat >&2 <<'EOF'

ERROR: One or more docs/comments assert a hardcoded ⌃-combo hotkey as if it were
the binding. Hotkeys are configurable (state/hotkeys.json); a fixed-keystroke
assertion goes stale when the default changes (issue #1925).

Fix: refer to the ACTION name (drop_context, toggle_voice, drop_screenshot,
drop_video_clip, toggle_mute) — the stable key in state/hotkeys.json. If you
name the keystroke, mark it a default and cite the contract, e.g.:

  Before:  the ⌃C "drop context" hotkey shells out to this binary
  After:   the "drop context" action (default ⌃⇧C, see state/hotkeys.json)
           shells out to this binary

A line that mentions `hotkeys.json`, `default`, or `configurable` alongside the
combo passes the lint.
EOF
  exit 1
fi

echo "lint-hotkey-assertions: clean (mode=$mode, scanned $(wc -l <<< "$files" | tr -d ' ') files)"
exit 0
