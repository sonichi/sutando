#!/bin/bash
# #4309 review round 12 (keweichen): the hook bridge in `commit_main()` turned
# a nonzero primary-installer exit into a successful `echo` via `cmd || echo
# ... >&2`, so `--commit` reported success (rc=0) even when hooks were never
# installed. src/startup.sh writes its migration-complete sentinel from THAT
# exit status alone (`if bash sutando-migrate.sh --commit; then ...`), so an
# unattended install could mark migration complete with no core hooks active.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
FAILURES=0

check() {  # check <name> <condition-exit> <detail>
    if [ "$2" -eq 0 ]; then echo "  ok   $1"; else
        echo "  FAIL $1 ${3:-}"; FAILURES=$((FAILURES + 1))
    fi
}

# Same isolated-repo-skeleton pattern as the delegation test beside this one.
PC="$(mktemp -d -t migrate-hook-failure-XXXXXX)"
trap 'rm -rf "$PC"' EXIT
mkdir -p "$PC/repo/scripts" "$PC/repo/src" "$PC/ws/state" "$PC/home" \
         "$PC/src/a" "$PC/src/b" "$PC/src/c"
cp "$REPO/scripts/sutando-migrate.sh"       "$PC/repo/scripts/"
cp "$REPO/scripts/sutando-config.sh"        "$PC/repo/scripts/"
cp "$REPO/scripts/sutando-config-hooks.sh"  "$PC/repo/scripts/"
cp "$REPO/scripts/python-binary.sh"         "$PC/repo/scripts/"
cp "$REPO/src/sutando_config.py"            "$PC/repo/src/"
cp "$REPO/src/install-claude-hooks.sh"      "$PC/repo/src/"
cp "$REPO/sutando.config.json"              "$PC/repo/"
touch "$PC/repo/CLAUDE.md" \
      "$PC/repo/src/archive-transcript.sh" "$PC/repo/src/session-handoff.sh" \
      "$PC/repo/src/check-pending-tasks.sh" "$PC/repo/src/turn-start.sh"
printf '{"workspace": {"path": "%s"}}\n' "$PC/ws" > "$PC/repo/sutando.config.local.json"

CORE_CCD="$(HOME="$PC/home" bash "$PC/repo/scripts/sutando-config.sh" claude-sutando-config-dir 2>/dev/null)"
mkdir -p "$CORE_CCD"
# Malform the installer's TARGET settings.json so install-claude-hooks.sh's
# own jq edit fails (its documented exit 1: "settings.json malformed").
printf '{not json' > "$CORE_CCD/settings.json"

echo "sutando-migrate: a failed hook bridge must fail the whole commit"

set +e
OUT="$(HOME="$PC/home" \
       SUTANDO_MIGRATE_SRC_A="$PC/src/a" \
       SUTANDO_MIGRATE_SRC_B="$PC/src/b" \
       SUTANDO_MIGRATE_SRC_C="$PC/src/c" \
       bash "$PC/repo/scripts/sutando-migrate.sh" --commit --no-confirm --no-claude-import 2>&1)"
RC=$?
set -e

check "install-claude-hooks.sh really did fail (fixture sanity)" \
    "$(grep -q 'hook install:.*failed' <<<"$OUT" && echo 0 || echo 1)" "installer did not report a failure"

check "THE POINT: --commit itself exits non-zero when the hook bridge failed" \
    "$([ "$RC" -ne 0 ] && echo 0 || echo 1)" "got rc=$RC -- a caller's 'if ... --commit; then' would wrongly treat this as success"

check "THE POINT: a durable retry marker is left for an operator who doesn't check exit codes" \
    "$([ -f "$PC/ws/state/.hook-bridge-retry-needed" ] && echo 0 || echo 1)" \
    "no marker at $PC/ws/state/.hook-bridge-retry-needed"

check "commit output still reports the failure in plain text" \
    "$(grep -q 'COMMIT reporting FAILURE' <<<"$OUT" && echo 0 || echo 1)"

printf '\n%s\n' "$OUT" | tail -6

echo
if [ "$FAILURES" -ne 0 ]; then echo "FAILED ($FAILURES)"; exit 1; fi
echo "All hook-bridge failure-propagation checks passed."
