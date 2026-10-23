#!/usr/bin/env bash
# Control for tests/sync-workspace.test.sh's real-clone tripwire (#5085 review):
# the suite used to take rcg_snapshot under the INHERITED git config and run
# rcg_assert, from its EXIT trap, under the private GIT_CONFIG_GLOBAL it exports
# later. The guard digests `git ls-files --others --exclude-standard`, so an
# operator's core.excludesFile alone moved the measurement: a globally ignored
# untracked file in the real clone was absent before and present after, and a
# fully green run exited 1 on a tripwire no test had earned.
#
# This runs the real suite against a stand-in "real clone" holding exactly that
# file, under an inherited global config that ignores it, and requires a quiet
# exit 0. The stand-in itself is built under a private config and must have a
# verified HEAD: built under the inherited one (which also demands a signature
# no signer can give) it had none, and two failed rev-parses compared equal.
# Hermetic: the stand-in is a temp repo; the suite's own deny-by-default still
# points every fixture at its denied dir.
#
# Run: bash tests/sync-workspace-guard-config.test.sh
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pass=0; fail=0
check() { if [ "$1" = "0" ]; then echo "  ok  $2"; pass=$((pass+1)); else echo "  FAIL $2"; fail=$((fail+1)); fi; }

T="$(mktemp -d -t sync-ws-guard-config.XXXXXX)"
trap 'rm -rf "$T"' EXIT
CLONE="$T/real-clone"; CFG="$T/cfg"
mkdir -p "$CLONE" "$CFG"
# The fixture is built under a private git config: an inherited commit.gpgsign
# or templateDir must not shape it, and a seed commit that fails must be loud.
fixture_git() { GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null git -c commit.gpgsign=false -c user.email=t@invalid -c user.name=t "$@"; }
fixture_git -C "$CLONE" init -q
fixture_git -C "$CLONE" commit -q --allow-empty -m seed
if ! head_before="$(fixture_git -C "$CLONE" rev-parse --verify HEAD 2>&1)"; then
  echo "  FAIL the stand-in clone has no seed commit: $head_before"
  exit 1
fi
check 0 "the stand-in clone has a seed commit (HEAD ${head_before:0:12})"
: > "$CLONE/ignored-by-global"
printf 'ignored-by-global\n' > "$CFG/excludes"
# The inherited view also demands signing with no signer: a fixture built under
# it would have no commit, and a guard with no HEAD must not be able to pass.
printf '[core]\n\texcludesFile = %s\n[commit]\n\tgpgsign = true\n[gpg]\n\tprogram = false\n' "$CFG/excludes" > "$CFG/gitconfig"

# The axis is live: the file is invisible under the inherited config and
# visible under a private one -- the two views the suite used to mix.
inherited="$(GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL="$CFG/gitconfig" git -C "$CLONE" status --porcelain -uall)"
private="$(GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null git -C "$CLONE" status --porcelain -uall)"
[ -z "$inherited" ] && [ "$private" = "?? ignored-by-global" ]
check $? "the stand-in clone's untracked file is hidden by the inherited excludesFile only (inherited='$inherited', private='$private')"

out="$T/suite.out"
GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL="$CFG/gitconfig" SUTANDO_MEMORY_SYNC_DIR="$CLONE" \
  bash "$REPO/tests/sync-workspace.test.sh" > "$out" 2>&1
rc=$?
total="$(sed -n 's/^Total: .*fail: \([0-9]*\).*/\1/p' "$out" | tail -1)"
[ "$rc" = "0" ]
check $? "the suite exits 0 with a globally ignored untracked file in the real clone (rc=$rc, failed checks: ${total:-<no total line>})"
if grep -q 'TRIPWIRE' "$out"; then
  check 1 "no tripwire fired: $(sed -n '/TRIPWIRE/,$p' "$out" | head -5 | tr '\n' ' ')"
else
  check 0 "no tripwire fired"
fi
[ "$(fixture_git -C "$CLONE" rev-parse --verify HEAD 2>/dev/null)" = "$head_before" ] && [ -f "$CLONE/ignored-by-global" ] \
  && [ -z "$(GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL="$CFG/gitconfig" git -C "$CLONE" status --porcelain -uall)" ]
check $? "the stand-in clone is as it was: same HEAD, the one ignored file, nothing else"

echo
if [ "$fail" -eq 0 ]; then echo "PASS — $pass checks green"; else echo "FAIL — $fail failed, $pass passed"; exit 1; fi
