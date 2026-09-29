#!/usr/bin/env bash
# Bash _host() precedence test — the bash-side mirror of
# tests/host-label-scutil.test.py. Guards: env wins (scutil not consulted),
# scutil LocalHostName over a drifting hostname, and — the py/bash parity fix —
# an exit-0-but-EMPTY scutil output falls back to hostname rather than winning.
#
#   bash tests/host-label-scutil-bash.test.sh
set -u

SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/scripts/sync-workspace.sh"
# Source ONLY the _host() definition — avoids running the script's main body.
eval "$(sed -n '/^_host()/,/^}/p' "$SCRIPT")"

fail=0
check() { # desc expected actual
    if [ "$2" = "$3" ]; then printf 'ok   - %s\n' "$1"
    else printf 'FAIL - %s (want=%q got=%q)\n' "$1" "$2" "$3"; fail=1; fi
}

# Stub bin: scutil reads $FAKE_LHN / $FAKE_RC; hostname reads $FAKE_HOSTNAME.
BIN="$(mktemp -d)"
cat > "$BIN/scutil" <<'EOF'
#!/usr/bin/env bash
[ "${FAKE_RC:-0}" -ne 0 ] && exit "$FAKE_RC"
printf '%s\n' "${FAKE_LHN-}"
EOF
cat > "$BIN/hostname" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "${FAKE_HOSTNAME:-box.local}"
EOF
chmod +x "$BIN/scutil" "$BIN/hostname"
export PATH="$BIN:$PATH"

out="$(SUTANDO_HOST_LABEL=Pinned FAKE_LHN=ShouldNotWin _host)"
check "env SUTANDO_HOST_LABEL wins (scutil not consulted)" "Pinned" "$out"

out="$(unset SUTANDO_HOST_LABEL; SUTANDO_HOST_OVERRIDE=Legacy _host)"
check "legacy SUTANDO_HOST_OVERRIDE honored" "Legacy" "$out"

out="$(unset SUTANDO_HOST_LABEL SUTANDO_HOST_OVERRIDE; FAKE_LHN=Chis-MacBook-Pro FAKE_HOSTNAME=Chis-MBP.hsd1.wa.comcast.net _host)"
check "scutil LocalHostName over drifting hostname" "Chis-MacBook-Pro" "$out"

out="$(unset SUTANDO_HOST_LABEL SUTANDO_HOST_OVERRIDE; FAKE_LHN= FAKE_HOSTNAME=fallback.local _host)"
check "scutil exit-0 EMPTY falls back to hostname" "fallback" "$out"

out="$(unset SUTANDO_HOST_LABEL SUTANDO_HOST_OVERRIDE; FAKE_RC=1 FAKE_HOSTNAME=slow.local _host)"
check "scutil nonzero falls back to hostname" "slow" "$out"

# The config-first branch. The five cases above are its negative control: with
# SCRIPT_PARENT unset the function falls through, it does not abort under set -u.
ROOT="$(mktemp -d)"; mkdir -p "$ROOT/scripts"
cat > "$ROOT/scripts/sutando-config.sh" <<'EOF'
#!/usr/bin/env bash
[ "${1-}" = host-label ] && printf '  %s  \n' "${FAKE_CFG_LABEL-}"
EOF
chmod +x "$ROOT/scripts/sutando-config.sh"

out="$(SCRIPT_PARENT="$ROOT" SUTANDO_HOST_LABEL=EnvLoses FAKE_CFG_LABEL=FromConfig FAKE_LHN=ShouldNotWin _host)"
check "configured label wins over env and scutil (whitespace trimmed)" "FromConfig" "$out"

out="$(SCRIPT_PARENT="$ROOT" SUTANDO_HOST_LABEL=Pinned FAKE_CFG_LABEL= FAKE_LHN=ShouldNotWin _host)"
check "an empty configured label falls through to the env pin" "Pinned" "$out"

rm -rf "$ROOT"
rm -rf "$BIN"
[ "$fail" -eq 0 ] && echo "PASS (7 cases)" || echo "FAILED"
exit $fail
