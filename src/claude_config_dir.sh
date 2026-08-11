#!/bin/bash
# Shared CLAUDE_CONFIG_DIR resolution for the launchers. Source this file with:
#
#   source "$REPO/src/claude_config_dir.sh"
#   if _ccd="$(resolve_claude_config_dir "$REPO" start-cli)"; then ...
#
# Single source for the resolve-or-refuse policy previously duplicated in
# src/agent/claude/cli/start-cli.sh and src/startup.sh — the second copy is the
# path CLAUDE.md documents as "To start everything", so a one-launcher fix left
# the failure reachable through the other door.
#
# Prints the resolved config dir on stdout. Exit status IS the branch:
#   0 → the M0 helper resolved it. The caller must `mkdir -p` and export it,
#       and owns whatever seeding it does inside that dir.
#   2 → helper absent, but the caller already exported CLAUDE_CONFIG_DIR (the
#       desktop app scopes the core this way). Nothing to resolve, nothing
#       unsafe: the value is echoed back and stays as the caller set it.
#   1 → refuse to start. A diagnostic is already on stderr.
#
# Refusal is the point. Leaving CLAUDE_CONFIG_DIR unset sends the process to
# Claude Code's user-level default config dir — a DIFFERENT credential store
# than every other Sutando process on the host, which surfaces as an
# unrecoverable 401 loop rather than as the install error it actually is.
#
# Presence is tested with `-r`, not `-x`: every call site invokes the helper as
# `bash <file>`, which needs read permission only. An extracted tarball can lose
# the exec bit on a helper that is still fully usable, and that population's
# ~/.claude IS their real credential store — refusing to boot there would break
# a working install to protect it from a problem it does not have.

resolve_claude_config_dir() {
  local repo="$1"
  local label="${2:-start-cli}"
  local helper="$repo/scripts/sutando-config.sh"

  if [ -r "$helper" ]; then
    local err ccd
    err="$(mktemp -t sutando-ccd.XXXXXX)"
    if ccd="$(bash "$helper" claude-sutando-config-dir 2>"$err")"; then
      rm -f "$err"
      printf '%s\n' "$ccd"
      return 0
    fi
    echo "$label: claude_sutando_config_dir invalid — refusing to start" >&2
    cat "$err" >&2
    rm -f "$err"
    return 1
  fi

  if [ -n "${CLAUDE_CONFIG_DIR:-}" ]; then
    printf '%s\n' "$CLAUDE_CONFIG_DIR"
    return 2
  fi

  {
    echo "$label: $helper missing or unreadable, and no CLAUDE_CONFIG_DIR from the"
    echo "  caller — refusing to start. It would go unset and the process would fall"
    echo "  back to Claude Code's user-level default config dir, i.e. a different"
    echo "  credential store than the rest of Sutando on this host. Re-run once the"
    echo "  install completes, or export CLAUDE_CONFIG_DIR before launching."
  } >&2
  return 1
}
