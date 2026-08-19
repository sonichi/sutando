#!/usr/bin/env bash
# The durable repo that supplies the running src/ — pinned against the packaged
# bundle layout, a normal checkout, and an explicit cross-checkout selection.
set -uo pipefail

REPO_UNDER_TEST="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pass=0; fail=0
ok()  { pass=$((pass+1)); echo "  ok   $1"; }
bad() { fail=$((fail+1)); echo "  FAIL $1"; }
is()  { [ "$2" = "$3" ] && ok "$1" || bad "$1 (want '$3', got '$2')"; }

BOX="$(mktemp -d)"
trap 'rm -rf "$BOX"' EXIT

# durable checkout owns .env and the real src/; the bundle only symlinks src/.
mkdir -p "$BOX/durable/repo/src" "$BOX/bundle/repo" "$BOX/other/repo/src"
printf 'X=1\n' > "$BOX/durable/repo/.env"
cp "$REPO_UNDER_TEST/src/repo_root.sh" "$BOX/durable/repo/src/repo_root.sh"
cp "$REPO_UNDER_TEST/src/repo_root.sh" "$BOX/other/repo/src/repo_root.sh"
ln -s "$BOX/durable/repo/src" "$BOX/bundle/repo/src"

# Physical paths: on macOS /tmp is a symlink to /private/tmp, so the resolver's
# pwd -P output would never equal a lexical $BOX path.
DURABLE="$(cd -P "$BOX/durable/repo" && pwd -P)"
OTHER="$(cd -P "$BOX/other/repo" && pwd -P)"

run() {  # run() <REPO value or ''> -> prints resolved root, sourcing via the BUNDLE path
  local repo_env="$1"
  if [ -n "$repo_env" ]; then
    REPO="$repo_env" bash -c '. "$1"; sutando_repo_root' _ "$BOX/bundle/repo/src/repo_root.sh"
  else
    bash -c 'unset REPO; . "$1"; sutando_repo_root' _ "$BOX/bundle/repo/src/repo_root.sh"
  fi
}

echo "== 1) packaged bundle: production-style REPO=bundle/repo with a symlinked src/ =="
is "explicit bundle REPO normalizes to the durable checkout" "$(run "$BOX/bundle/repo")" "$DURABLE"
is "no REPO, sourced via the bundle path, still lands durable" "$(run '')" "$DURABLE"

echo "== 2) controls =="
is "normal checkout resolves to itself" \
   "$(REPO="$BOX/durable/repo" bash -c '. "$1"; sutando_repo_root' _ "$BOX/durable/repo/src/repo_root.sh")" \
   "$DURABLE"
is "explicit OTHER checkout is preserved, not rewritten to the running one" \
   "$(REPO="$BOX/other/repo" bash -c '. "$1"; sutando_repo_root' _ "$BOX/bundle/repo/src/repo_root.sh")" \
   "$OTHER"

echo "== 3) the defect this replaces: .env must be reachable from the resolved root =="
root="$(run "$BOX/bundle/repo")"
[ -f "$root/.env" ] && ok ".env found under the resolved root" || bad ".env NOT found under '$root'"
lexical="$(cd "$BOX/bundle/repo/src/.." && pwd)"
[ -f "$lexical/.env" ] && bad "control is inert: the OLD lexical form already found .env" \
                       || ok "control: the old lexical form does NOT find .env (probe can fail)"

echo "== 4) degradation: a candidate with no src/ still yields a path, never empty =="
mkdir -p "$BOX/nosrc"
out="$(REPO="$BOX/nosrc" bash -c '. "$1"; sutando_repo_root' _ "$BOX/durable/repo/src/repo_root.sh")"
is "unresolvable candidate falls back to the candidate itself" "$out" "$BOX/nosrc"

echo
echo "passed $pass, failed $fail"
[ "$fail" -eq 0 ]
