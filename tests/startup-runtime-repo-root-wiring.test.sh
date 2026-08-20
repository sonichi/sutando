#!/usr/bin/env bash
# Wiring: startup-runtime.sh must answer "which repo supplies my code" through
# the shared resolver, so the packaged bundle layout reaches durable .env.
set -uo pipefail

REPO_UNDER_TEST="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pass=0; fail=0
ok()  { pass=$((pass+1)); echo "  ok   $1"; }
bad() { fail=$((fail+1)); echo "  FAIL $1"; }

BOX="$(mktemp -d)"
trap 'rm -rf "$BOX"' EXIT
mkdir -p "$BOX/durable/repo/src" "$BOX/durable/repo/scripts" "$BOX/bundle/repo"
cp "$REPO_UNDER_TEST/src/repo_root.sh" "$BOX/durable/repo/src/"
cp "$REPO_UNDER_TEST/src/startup-runtime.sh" "$BOX/durable/repo/src/"
printf 'DOTENV_SENTINEL=from-durable\n' > "$BOX/durable/repo/.env"
ln -s "$BOX/durable/repo/src" "$BOX/bundle/repo/src"
DURABLE="$(cd -P "$BOX/durable/repo" && pwd -P)"

echo "== every _repo site resolves durable when sourced via the bundle =="
# Each site calls sutando_repo_root; asserting the resolver's answer under the
# production-style env covers all five without invoking their side effects.
got="$(REPO="$BOX/bundle/repo" bash -c '
  . "$1" 2>/dev/null || true
  sutando_repo_root' _ "$BOX/bundle/repo/src/startup-runtime.sh" 2>/dev/null)"
[ "$got" = "$DURABLE" ] && ok "sourcing startup-runtime.sh exposes a resolver that answers durable" \
                        || bad "resolver via startup-runtime.sh gave '$got', want '$DURABLE'"

echo "== configure_startup_runtime actually LOADS the durable .env =="
out="$(REPO="$BOX/bundle/repo" bash -c '
  . "$1" 2>/dev/null || true
  configure_startup_runtime >/dev/null 2>&1 || true
  printf "%s" "${DOTENV_SENTINEL:-<unset>}"' _ "$BOX/bundle/repo/src/startup-runtime.sh" 2>/dev/null)"
[ "$out" = "from-durable" ] && ok "durable .env is sourced through the bundle path" \
                            || bad "DOTENV_SENTINEL='$out', want 'from-durable'"

echo "== control: the pre-fix lexical form does NOT reach it (probe can fail) =="
lex="$(cd "$BOX/bundle/repo/src/.." && pwd)"
[ -f "$lex/.env" ] && bad "control inert — lexical parent already has .env" \
                   || ok "lexical parent '$lex' has no .env, as in the reported bug"

echo "== no lexical copies left in the adapter =="
if grep -q '/\.\. && pwd)}"' "$REPO_UNDER_TEST/src/startup-runtime.sh"; then
  bad "startup-runtime.sh still hand-rolls the lexical repo root"
else
  ok "startup-runtime.sh has no hand-rolled lexical repo root"
fi

echo "== every suite that STAGES startup-runtime.sh also stages its sibling =="
# The source is a hard dependency: a suite that copies one without the other dies
# on FATAL. Enumerate rather than wait for CI to find them one at a time.
missing=""
for f in "$REPO_UNDER_TEST"/tests/*.test.sh "$REPO_UNDER_TEST"/tests/*.test.py; do
  [ -f "$f" ] || continue
  grep -q 'cp .*src/startup-runtime\.sh' "$f" 2>/dev/null || continue
  grep -q 'repo_root\.sh' "$f" 2>/dev/null || missing="$missing $(basename "$f")"
done
[ -z "$missing" ] && ok "no suite copies startup-runtime.sh without repo_root.sh" \
                  || bad "these stage it alone and will hit FATAL:$missing"

echo "== a missing sibling resolver fails LOUDLY, never silently-empty =="
T="$(mktemp -d)"; mkdir -p "$T/src"; cp "$REPO_UNDER_TEST/src/startup-runtime.sh" "$T/src/"
err="$(bash -c '. "$1"' _ "$T/src/startup-runtime.sh" 2>&1 >/dev/null)"
bash -c '. "$1"' _ "$T/src/startup-runtime.sh" >/dev/null 2>&1; rc=$?
case "$err" in *"FATAL: src/repo_root.sh not found"*) ok "missing helper prints a FATAL line" ;;
                *) bad "no FATAL line (got: ${err:0:60})" ;; esac
[ "$rc" -ne 0 ] && ok "sourcing returns non-zero" || bad "sourcing returned 0 with the helper missing"
rm -rf "$T"

echo
echo "passed $pass, failed $fail"
[ "$fail" -eq 0 ]
