#!/bin/bash
# Sourceable: start_inbox_events <python> <fifo> <path>... starts fswatch behind
# src/line_relay.py; $! is fswatch's pid, and the relay exits when fswatch does.
__INBOX_EVENTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
start_inbox_events() {
  local py="$1" fifo="$2"
  shift 2
  fswatch -l 0.5 --event Created --event Renamed --event Updated "$@" 2>/dev/null \
    > >("$py" "$__INBOX_EVENTS_DIR/line_relay.py" > "$fifo") &
}
