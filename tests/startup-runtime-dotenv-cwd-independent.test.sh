#!/bin/bash
# configure_startup_runtime() must find .env from any cwd: the app bundle invokes
# startup from its own directory, where a bare `.env` silently resolves to nothing.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pass=0
fail=0

ok()   { echo "  PASS: $1"; pass=$((pass + 1)); }
bad()  { echo "  FAIL: $1${2:+ — $2}" >&2; fail=$((fail + 1)); }

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
mkdir -p "$stage/src"
cp "$REPO/src/startup-runtime.sh" "$stage/src/"
cp "$REPO/src/repo_root.sh" "$stage/src/"
printf 'SUTANDO_DOTENV_PROBE=loaded\n' > "$stage/.env"

probe() {   # $1 = cwd to run from
  (
    cd "$1" || exit 1
    # REPO unset on purpose: the fallback must resolve from BASH_SOURCE.
    unset REPO SUTANDO_DOTENV_PROBE
    source "$stage/src/startup-runtime.sh"
    configure_startup_runtime >/dev/null 2>&1
    echo "${SUTANDO_DOTENV_PROBE:-<UNSET>}"
  )
}

got="$(probe "$stage")"
[ "$got" = "loaded" ] && ok "loads .env when cwd IS the repo" \
  || bad "cwd==repo" "got '$got'"

# The regression: this is the app-bundle invocation, and it is the whole point.
got="$(probe /tmp)"
[ "$got" = "loaded" ] && ok "loads .env from a FOREIGN cwd (app-bundle case)" \
  || bad "cwd!=repo" "got '$got' — credentials would silently vanish"

# Absent .env must still degrade, not error.
rm -f "$stage/.env"
out="$( (cd /tmp && unset REPO; source "$stage/src/startup-runtime.sh"; configure_startup_runtime 2>&1 | head -1) )"
case "$out" in
  *"not found"*) ok "missing .env still degrades with the credential-free notice" ;;
  *) bad "missing .env" "got '$out'" ;;
esac

echo
echo "Results: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
