#!/bin/bash
# quiet-hours.sh — daily volume switch for this host.
#
# Owner 2026-08-02: 「你们设置开关 volume 每天 12 点到八点吧」 — mute overnight,
# restore in the morning, on a schedule instead of by hand.
#
#   bash scripts/quiet-hours.sh on    # remember the current level, then mute
#   bash scripts/quiet-hours.sh off   # restore the remembered level
#   bash scripts/quiet-hours.sh status
#
# WHY IT SAVES THE LEVEL. Restoring to a hardcoded number would quietly rewrite
# a setting the owner chose. The level in effect when quiet hours BEGIN is
# written to <workspace>/state/quiet-hours.json and put back verbatim at the
# end, so a full night leaves the volume exactly where she left it.
#
# Idempotent both ways: `on` twice does not overwrite the saved level with 0
# (which would restore to silence in the morning — the one failure that would
# be invisible until she wondered why her speakers were dead).
set -uo pipefail

_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE="$(bash "$_dir/sutando-config.sh" workspace 2>/dev/null)"
STATE="${WORKSPACE:-/tmp}/state/quiet-hours.json"

_vol()   { osascript -e 'output volume of (get volume settings)' 2>/dev/null; }
_muted() { osascript -e 'output muted of (get volume settings)' 2>/dev/null; }
_set()   { osascript -e "set volume output volume $1" 2>/dev/null; }

case "${1:-status}" in
  on)
    cur="$(_vol)"
    if [ -f "$STATE" ] && grep -q '"active": *true' "$STATE" 2>/dev/null; then
      echo "quiet-hours: already on (saved level $(grep -o '"saved_volume": *[0-9]*' "$STATE" | grep -o '[0-9]*')) — not re-saving"
      _set 0
      exit 0
    fi
    mkdir -p "$(dirname "$STATE")"
    printf '{"active": true, "saved_volume": %s, "saved_muted": %s, "since": "%s"}\n' \
      "${cur:-50}" "$(_muted)" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$STATE"
    _set 0
    echo "quiet-hours: ON — was ${cur}, now $(_vol); level saved to $STATE"
    ;;
  off)
    if [ ! -f "$STATE" ]; then
      echo "quiet-hours: no saved state — leaving volume at $(_vol) rather than guessing" >&2
      exit 0
    fi
    saved="$(grep -o '"saved_volume": *[0-9]*' "$STATE" | grep -o '[0-9]*')"
    if [ -z "$saved" ]; then
      echo "quiet-hours: saved level unreadable — leaving volume at $(_vol)" >&2
      exit 0
    fi
    _set "$saved"
    printf '{"active": false, "saved_volume": %s, "restored_at": "%s"}\n' \
      "$saved" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$STATE"
    echo "quiet-hours: OFF — restored to $(_vol)"
    ;;
  status)
    echo "volume=$(_vol) muted=$(_muted)"
    [ -f "$STATE" ] && cat "$STATE" || echo "(no state file yet)"
    ;;
  *)
    echo "usage: $0 {on|off|status}" >&2; exit 2 ;;
esac
