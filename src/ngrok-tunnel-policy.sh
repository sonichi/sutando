#!/bin/bash
# Bind webhook URL discovery to the supervised ngrok process's own API listener,
# and accept only well-formed https base URLs, optionally pinned to a host.

ngrok_api_port() {
  local pid="$1"
  lsof -nP -a -p "$pid" -iTCP -sTCP:LISTEN -Fn 2>/dev/null \
    | sed -n 's/^n127\.0\.0\.1:\([0-9][0-9]*\)$/\1/p' \
    | head -1
}

webhook_base_url_valid() {
  local url="$1" expected_host="${2:-}" regex host
  regex='^https://[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?(:[0-9]{1,5})?/?$'
  [[ "$url" != *$'\n'* && "$url" != *$'\r'* && "$url" =~ $regex ]] || return 1
  [ -n "$expected_host" ] || return 0
  host="${url#https://}"
  host="${host%%/*}"
  host="${host%%:*}"
  [ "$(printf '%s' "$host" | tr '[:upper:]' '[:lower:]')" = \
    "$(printf '%s' "$expected_host" | tr '[:upper:]' '[:lower:]')" ]
}
