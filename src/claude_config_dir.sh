#!/bin/bash
# Shared CLAUDE_CONFIG_DIR resolution for start-cli.sh and startup.sh. Refusing to
# start beats leaving it unset: unset silently selects a foreign credential store.

resolve_claude_config_dir() {
  local repo="$1"
  local label="${2:-start-cli}"
  local helper="$repo/scripts/sutando-config.sh"

  # -r, not -x: every call site runs `bash <helper>`, which needs read only,
  # and an extracted tarball can drop the exec bit from a usable helper.
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
