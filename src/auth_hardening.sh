#!/usr/bin/env bash
# auth_hardening.sh — harden per-host install-state secrets under state/auth/.
#
# Sourced by src/startup.sh so the logic can be unit-tested directly
# (tests/auth-hardening.test.sh) instead of driving the whole boot sequence.
#
# state/auth/ holds cloud-auth.json / device.json / ag2space.json — per-host
# auth + identity. Under the documented default workspace (<repo>/workspace/)
# these inherit the checkout's 0755/0644, NOT the 0700 that ~/Library/Application
# Support would give — so the owner-only protection has to be OURS, not an
# incidental filesystem default (2026-07-28).

# harden_auth_dir <workspace-dir>
# Tighten <workspace>/state/auth to 0700 and its *.json files to 0600.
# Idempotent + fail-safe: only ever tightens (never widens), a missing dir is a
# no-op, and every error is swallowed so a perms quirk can't block boot.
harden_auth_dir() {
  local ws="$1"
  [ -n "$ws" ] || return 0
  local auth_dir="$ws/state/auth"
  [ -d "$auth_dir" ] || return 0
  chmod 700 "$auth_dir" 2>/dev/null || true
  find "$auth_dir" -maxdepth 1 -type f -name '*.json' -exec chmod 600 {} + 2>/dev/null || true
  return 0
}
