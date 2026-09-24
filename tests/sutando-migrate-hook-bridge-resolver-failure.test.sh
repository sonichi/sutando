#!/bin/bash
# #4309 review round 13 (keweichen): a resolver FAILURE (claude-sutando-config-dir
# returning empty) was laundered the same way the round-12 installer-failure fix
# just closed -- the `else` branch at the empty-_new_ccd check only printed
# "skipped" and never set `_hook_bridge_failed`, so `--commit` still reported
# success while installing zero hooks.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
FAILURES=0

check() {  # check <name> <condition-exit> <detail>
    if [ "$2" -eq 0 ]; then echo "  ok   $1"; else
        echo "  FAIL $1 ${3:-}"; FAILURES=$((FAILURES + 1))
    fi
}

PC="$(mktemp -d -t migrate-resolver-failure-XXXXXX)"
trap 'rm -rf "$PC"' EXIT
mkdir -p "$PC/repo/scripts" "$PC/repo/src" "$PC/ws/state" "$PC/home" \
         "$PC/src/a" "$PC/src/b" "$PC/src/c"
cp "$REPO/scripts/sutando-migrate.sh"       "$PC/repo/scripts/"
cp "$REPO/scripts/sutando-config-hooks.sh"  "$PC/repo/scripts/"
cp "$REPO/scripts/python-binary.sh"         "$PC/repo/scripts/"
cp "$REPO/src/sutando_config.py"            "$PC/repo/src/"
cp "$REPO/src/install-claude-hooks.sh"      "$PC/repo/src/"
cp "$REPO/sutando.config.json"              "$PC/repo/"
touch "$PC/repo/CLAUDE.md" \
      "$PC/repo/src/archive-transcript.sh" "$PC/repo/src/session-handoff.sh" \
      "$PC/repo/src/check-pending-tasks.sh" "$PC/repo/src/turn-start.sh"
printf '{"workspace": {"path": "%s"}}\n' "$PC/ws" > "$PC/repo/sutando.config.local.json"

# A shim that fails ONLY claude-sutando-config-dir (empty stdout, rc 9) and
# delegates every other subcommand to a real copy resolved WITHIN this
# isolated repo (never the outer dev checkout, or DEST_REAL etc. resolve
# against the wrong config context entirely).
cp "$REPO/scripts/sutando-config.sh" "$PC/repo/scripts/sutando-config-real.sh"
cat > "$PC/repo/scripts/sutando-config.sh" << 'SHIM'
#!/bin/bash
if [ "${1:-}" = "claude-sutando-config-dir" ]; then
    exit 9
fi
exec bash "$(dirname "$0")/sutando-config-real.sh" "$@"
SHIM
chmod +x "$PC/repo/scripts/sutando-config.sh"

echo "sutando-migrate: an unresolvable claude-sutando-config-dir must fail the whole commit"

set +e
OUT="$(HOME="$PC/home" \
       SUTANDO_MIGRATE_SRC_A="$PC/src/a" \
       SUTANDO_MIGRATE_SRC_B="$PC/src/b" \
       SUTANDO_MIGRATE_SRC_C="$PC/src/c" \
       bash "$PC/repo/scripts/sutando-migrate.sh" --commit --no-confirm --no-claude-import 2>&1)"
RC=$?
set -e

check "the resolver really did fail this fire (fixture sanity)" \
    "$(grep -q "couldn't resolve claude-sutando-config-dir" <<<"$OUT" && echo 0 || echo 1)"

check "THE POINT: --commit exits non-zero on an unresolvable required destination" \
    "$([ "$RC" -ne 0 ] && echo 0 || echo 1)" "got rc=$RC"

check "THE POINT: the durable retry marker is left" \
    "$([ -f "$PC/ws/state/.hook-bridge-retry-needed" ] && echo 0 || echo 1)"

check "the hook bridge line reads FAILED, not skipped" \
    "$(grep -q "hook bridge: FAILED" <<<"$OUT" && echo 0 || echo 1)"

printf '\n%s\n' "$OUT" | tail -6

echo
if [ "$FAILURES" -ne 0 ]; then echo "FAILED ($FAILURES)"; exit 1; fi
echo "All resolver-failure propagation checks passed."
