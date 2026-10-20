#!/bin/bash
# Sourced by both start-cli.sh entry points before any launcher effect: any -witnes*/--witnes* argument, in any
# case, must be an exact supported flag, so a retired or misspelt one never reaches a production launch.
witness_flags_exact() {
  local a
  for a in "$@"; do
    case "$a" in
      --witness|--witness-stop|--witness-status|--witness-scrubbed) ;;
      -[wW][iI][tT][nN][eE][sS]*|--[wW][iI][tT][nN][eE][sS]*)
        echo "start-cli: unknown witness flag '$a'; supported: --witness <name>, --witness-stop <name>, --witness-status <name> (no '='). Nothing was started." >&2
        exit 2
        ;;
    esac
  done
}
