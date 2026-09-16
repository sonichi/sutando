#!/bin/bash
# The Stop hook must watch the WORKSPACE queue, not <repo>/tasks/.
#
# THE DEFECT. src/check-pending-tasks.sh resolved its queue as
# `$(dirname "$0")/../tasks` — the repo root. Every producer (voice, Discord,
# Telegram, Slack, chat) and every consumer writes <workspace>/tasks/. On a
# default install those are different directories, so the hook read an empty
# one, emitted `{}` on every Stop, and never blocked on anything. It is the
# fail-silent shape: a broken hook and an empty queue emit the same `{}`, and
# the queue is empty most of the time anyone looks.
#
# ISOLATION (@john-the-dev's blocker on d1767d43). The first version of this
# test resolved the workspace from the host config and wrote its probe into the
# caller's REAL queue — the directory `watch-tasks-stream.sh` is concurrently
# watching. The watcher could claim and execute the probe as a live owner task
# before the EXIT trap removed it, and cleanup cannot retract a reply already
# delivered. A synthetic filename lowers collision odds; it does not isolate a
# live consumer.
#
# So this test builds its own workspace and pins it: `SUTANDO_TEST_MODE=1` plus
# `SUTANDO_WORKSPACE` is the repo's supported escape hatch (src/sutando_config.py
# ~line 336 — the env var alone is ignored post-v0.8/#1440). Passing a temp dir
# is NOT isolation on its own, so the resolved path is ASSERTED before anything
# is written: if the hatch ever regresses, this aborts instead of quietly
# writing to the live queue.
#
# WHY BOTH DIRECTIONS ARE PINNED. "Blocks when a task exists" is satisfied by a
# hook that watches EITHER directory, and "stays quiet when empty" is satisfied
# by the broken version. The case that separates them is the legacy one: a file
# in <repo>/tasks/ must NOT block, because reading that directory is the bug.
# Without it, a fix watching both paths passes — and reintroduces the defect in
# the other direction, since a stale legacy file would then block forever.
#
# Run: bash tests/check-pending-tasks-workspace.test.sh
set -u

REPO="$(cd "$(dirname "$0")/.." && pwd)"
HOOK="$REPO/src/check-pending-tasks.sh"

# Fixture setup needs a REAL git, not whatever bare `git` resolves to here --
# go through the same resolver the hook uses, or the suite risks its own failure mode.
. "$REPO/scripts/git-binary.sh"
TEST_GIT="$(resolve_git)"
if [ -z "$TEST_GIT" ]; then
  echo "FAIL: no usable git found to build test fixtures with."
  exit 1
fi

# Same reasoning, same fix, for python3 -- a bare `|| echo python3` fallback is
# exactly the CLT-stub risk this suite exists to catch, just in its own setup.
. "$REPO/scripts/python-binary.sh"
TEST_PY="$(resolve_python "$REPO")"
if [ -z "$TEST_PY" ]; then
  echo "FAIL: no usable python3 found to build test fixtures with."
  exit 1
fi

# set -u doesn't catch a command failure -- a silent init/commit failure would
# leave a non-repo directory and every case below would pass for the wrong reason.
_git_fixture_repo() {
  "$TEST_GIT" -C "$1" init -q \
    && "$TEST_GIT" -C "$1" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init \
    || { echo "FAIL: fixture git init/commit failed in $1 -- aborting rather than run a vacuous suite"; exit 1; }
}

# --- Build and PIN an isolated workspace before resolving anything ----------
TMPWS="$(mktemp -d "${TMPDIR:-/tmp}/sutando-hooktest.XXXXXX")"

# The hook now also refuses a turn that ends with no message and no recorded
# no-send. These cases assert the TASK gate, so satisfy the turn gate first or
# they measure the wrong refusal.
record_delivery() {
  "$TEST_PY" \
    "$REPO/src/turn_ledger.py" --workspace "$TMPWS" no-send "hook unit test" >/dev/null 2>&1 || true
}

export SUTANDO_TEST_MODE=1
export SUTANDO_WORKSPACE="$TMPWS"

LIVE_WS="$(env -u SUTANDO_TEST_MODE -u SUTANDO_WORKSPACE \
             bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)"
WS="$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)"

# Compare resolved paths, not the strings we passed in — mktemp may hand back a
# symlinked prefix (/tmp -> /private/tmp on macOS).
_real() { (cd "$1" 2>/dev/null && pwd -P) || echo "$1"; }
if [ "$(_real "$WS")" != "$(_real "$TMPWS")" ]; then
  echo "FAIL: workspace did not resolve to the test dir — refusing to run."
  echo "      wanted: $TMPWS"
  echo "      got:    $WS"
  echo "      The SUTANDO_TEST_MODE escape hatch (src/sutando_config.py) has changed."
  rm -rf "$TMPWS"
  exit 1
fi
if [ -n "$LIVE_WS" ] && [ "$(_real "$WS")" = "$(_real "$LIVE_WS")" ]; then
  echo "FAIL: test workspace is the live workspace — refusing to run."
  rm -rf "$TMPWS"
  exit 1
fi

# The legacy probe (case 4) must go where the BUG reads, so it is the one write
# outside the temp tree. It is safe: watch-tasks-stream.sh resolves its
# TASKS_DIR from `sutando-config.sh workspace` (line 38), so <repo>/tasks/ has
# no consumer — and the guard below refuses if the two are ever the same dir.
PROBE="task-zz-hooktest-$$.txt"
LEGACY_DIR="$REPO/tasks"

cleanup() { rm -rf "$TMPWS"; rm -f "$LEGACY_DIR/$PROBE"; }
trap cleanup EXIT

FAILED=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n     %s\n' "$1" "$2"; FAILED=1; }

mkdir -p "$WS/tasks" "$WS/results"

# 1. A task in the workspace queue must block. FAILS against the old path.
printf 'id: probe\ntask: probe\n' > "$WS/tasks/$PROBE"
OUT="$(bash "$HOOK" 2>&1)"
case "$OUT" in
  *'"decision":"block"'*) ok "workspace task blocks" ;;
  *) bad "workspace task blocks" "got: ${OUT:0:120}" ;;
esac

# 2. ...and the payload must name the task, not just block generically.
case "$OUT" in
  *"$PROBE"*) ok "block payload names the task" ;;
  *) bad "block payload names the task" "payload omits $PROBE" ;;
esac

# 3. A task with a matching result is already handled — must not block.
printf 'done\n' > "$WS/results/$PROBE"
OUT="$(bash "$HOOK" 2>&1)"
case "$OUT" in
  '{}') ok "result present suppresses the block" ;;
  *) bad "result present suppresses the block" "got: ${OUT:0:120}" ;;
esac
rm -f "$WS/tasks/$PROBE" "$WS/results/$PROBE"

# 4. THE DIRECTION CASE. A file in the legacy <repo>/tasks/ must NOT block.
if [ "$(_real "$LEGACY_DIR")" = "$(_real "$WS/tasks")" ]; then
  printf '  skip legacy dir IS the resolved queue here; direction is not decidable\n'
else
  mkdir -p "$LEGACY_DIR"
  printf 'id: probe\ntask: legacy\n' > "$LEGACY_DIR/$PROBE"
  record_delivery
  OUT="$(bash "$HOOK" 2>&1)"
  case "$OUT" in
    '{}') ok "legacy <repo>/tasks/ does not block" ;;
    *) bad "legacy <repo>/tasks/ does not block" "hook still reads the repo: ${OUT:0:120}" ;;
  esac
  rm -f "$LEGACY_DIR/$PROBE"
fi

# 5. Empty queue stays quiet — the control that proves case 1 measured something.
record_delivery
OUT="$(bash "$HOOK" 2>&1)"
case "$OUT" in
  '{}') ok "empty queue emits {}" ;;
  *) bad "empty queue emits {}" "got: ${OUT:0:120}" ;;
esac

# 6. THE REJECTION PATH. A refused interpreter must not fall back to bare
# `python3`. A recording `git` stub witnesses resolve_git actually ran.
REJ="$(mktemp -d)"
mkdir -p "$REJ/scripts" "$REJ/src" "$REJ/workspace/tasks" "$REJ/workspace/results"
printf '#!/bin/bash\n[ "$1" = "workspace" ] && { echo "%s/workspace"; exit 0; }\nexit 1\n' "$REJ" \
  > "$REJ/scripts/sutando-config.sh"
chmod +x "$REJ/scripts/sutando-config.sh"
cp "$HOOK" "$REJ/src/"
cp "$REPO/scripts/git-binary.sh" "$REJ/scripts/"
printf 'id: probe\ntask: rejected-interpreter\n' > "$REJ/workspace/tasks/$PROBE"
# A recording shim: if the hook falls back to PATH python this fires.
printf '#!/bin/bash\necho FALLBACK_INVOKED >&2\nexit 79\n' > "$REJ/python3"
chmod +x "$REJ/python3"
# A recording stub, so resolve_git has something to resolve to. Both probes
# echo the SAME dir ($REJ) -- same identity, so it falls through to the case below.
printf '#!/bin/bash\necho "GIT_CALLED $*" >> "%s/git-calls.log"\necho "%s"\n' "$REJ" "$REJ" \
  > "$REJ/git"
chmod +x "$REJ/git"
printf '#!/bin/sh\nexit 2\n' > "$REJ/xcode-select"
chmod +x "$REJ/xcode-select"
# A NON-Git cwd (never this checkout) so the guest fall-through, not a real
# identity or a short-circuiting guest exit, is what reaches the logic below.
REJ_CWD="$(mktemp -d)"
REJ_ERR="$(cd "$REJ_CWD" && OSTYPE=darwin25 PATH="$REJ:$PATH" bash "$REJ/src/$(basename "$HOOK")" 2>&1 >/dev/null)"
rm -f "$REJ/git-calls.log"
REJ_OUT="$(cd "$REJ_CWD" && OSTYPE=darwin25 PATH="$REJ:$PATH" bash "$REJ/src/$(basename "$HOOK")" 2>/dev/null)"
case "$REJ_ERR" in
  *FALLBACK_INVOKED*) bad "a refused interpreter is not worked around" "the bare python3 fallback ran" ;;
  *) ok "a refused interpreter is not worked around" ;;
esac
# EXACT equality, not a substring -- extra noise alongside the good message
# (git-binary.sh failing to source, a stray command-not-found) must not pass.
if [ "$REJ_ERR" = "check-pending-tasks: no usable interpreter; queue not reported" ]; then
  ok "stderr is exactly the one deliberate rejection message, nothing else"
else
  bad "stderr is exactly the one deliberate rejection message, nothing else" "got: ${REJ_ERR:0:160}"
fi
case "$REJ_OUT" in
  '{}') ok "a refused interpreter still emits valid JSON" ;;
  *) bad "a refused interpreter still emits valid JSON" "got: ${REJ_OUT:0:120}" ;;
esac
# EXACT whole-file equality, not grep -qF -- a substring search would also pass
# a call carrying EXTRA argv beyond what's expected on the same log line.
GIT_CALLS_EXPECTED="$(printf 'GIT_CALLED rev-parse --path-format=absolute --git-common-dir\nGIT_CALLED -C %s rev-parse --path-format=absolute --git-common-dir\n' "$REJ")"
if [ -f "$REJ/git-calls.log" ] && [ "$(cat "$REJ/git-calls.log")" = "$GIT_CALLS_EXPECTED" ]; then
  ok "the git stub was invoked exactly twice, with the two expected argv records"
else
  bad "the git stub was invoked exactly twice, with the two expected argv records" \
    "$([ -f "$REJ/git-calls.log" ] && cat "$REJ/git-calls.log" || echo "no log file -- git never ran")"
fi

# 6b. THE READINESS-CHECK GAP. A TRULY result-only queue must reach the
# turn-ledger gate, not stop earlier -- both PYBIN call sites must guard.
rm -f "$REJ/workspace/tasks/$PROBE"
RESULT_PROBE="task-zz-hooktest-readiness-$$.txt"
printf 'id: probe\ntask: readiness-gap-probe\n' > "$REJ/workspace/tasks/$RESULT_PROBE"
printf 'a real, well-formed reply\n' > "$REJ/workspace/results/$RESULT_PROBE"
RG_ERR="$(cd "$REJ_CWD" && OSTYPE=darwin25 PATH="$REJ:$PATH" bash "$REJ/src/$(basename "$HOOK")" 2>&1 >/dev/null)"
RG_OUT="$(cd "$REJ_CWD" && OSTYPE=darwin25 PATH="$REJ:$PATH" bash "$REJ/src/$(basename "$HOOK")" 2>/dev/null)"
case "$RG_ERR" in
  *"command not found"*|*": : "*)
    bad "a truly result-only queue with no interpreter produces no stray command-not-found noise" "got: ${RG_ERR:0:160}" ;;
  *) ok "a truly result-only queue with no interpreter produces no stray command-not-found noise" ;;
esac
case "$RG_OUT" in
  '{}') ok "a truly result-only queue with no interpreter still emits valid JSON" ;;
  *) bad "a truly result-only queue with no interpreter still emits valid JSON" "got: ${RG_OUT:0:120}" ;;
esac
# BEHAVIORAL, not source text (a regex passes on a dead/commented guard) --
# `bash -x` traces the actual command; an empty PYBIN execs as `+ '' ...`.
(cd "$REJ_CWD" && OSTYPE=darwin25 PATH="$REJ:$PATH" bash -x "$REJ/src/$(basename "$HOOK")") >/dev/null 2>"$REJ/xtrace.log"
if grep -qE "^\+ '' " "$REJ/xtrace.log"; then
  bad "neither PYBIN call site ever execs an empty command" \
    "$(grep -E "^\+ '' " "$REJ/xtrace.log" | head -1)"
else
  ok "neither PYBIN call site ever execs an empty command"
fi
rm -f "$REJ/workspace/tasks/$RESULT_PROBE" "$REJ/workspace/results/$RESULT_PROBE" "$REJ/xtrace.log"

# 6c. EXISTENCE IS NOT READINESS (delivery/readiness.py's own contract) -- a
# GENUINELY EMPTY result file, with no interpreter to check it, must NOT be
# silently read as done. It must stay UNPROCESSED and surface the honest
# no-interpreter warning, not vanish into a quiet {}.
EMPTY_PROBE="task-zz-hooktest-emptyresult-$$.txt"
printf 'id: probe\ntask: empty-result-probe\n' > "$REJ/workspace/tasks/$EMPTY_PROBE"
: > "$REJ/workspace/results/$EMPTY_PROBE"
ER_ERR="$(cd "$REJ_CWD" && OSTYPE=darwin25 PATH="$REJ:$PATH" bash "$REJ/src/$(basename "$HOOK")" 2>&1 >/dev/null)"
if [ "$ER_ERR" = "check-pending-tasks: no usable interpreter; queue not reported" ]; then
  ok "a genuinely empty result with no interpreter is NOT silently treated as done"
else
  bad "a genuinely empty result with no interpreter is NOT silently treated as done" \
    "expected the no-interpreter warning; got: ${ER_ERR:0:160}"
fi
rm -f "$REJ/workspace/tasks/$EMPTY_PROBE" "$REJ/workspace/results/$EMPTY_PROBE"
rm -f "$REJ/workspace/tasks/$RESULT_PROBE" "$REJ/workspace/results/$RESULT_PROBE"
rm -rf "$REJ_CWD"
# 7. THE GUEST CARVE-OUT. A worktree of an UNRELATED repo must not be held
# hostage by the core's queue, even with a real pending task sitting in it.
printf 'id: probe\ntask: guest-worktree-probe\n' > "$WS/tasks/$PROBE"
GUEST_REPO="$(mktemp -d)"
_git_fixture_repo "$GUEST_REPO"
GUEST_OUT="$(cd "$GUEST_REPO" && bash "$HOOK" 2>&1)"
case "$GUEST_OUT" in
  '{}') ok "unrelated-repo worktree is not blocked by the core's queue" ;;
  *) bad "unrelated-repo worktree is not blocked by the core's queue" "got: ${GUEST_OUT:0:120}" ;;
esac
rm -rf "$GUEST_REPO"

# 8. CONTROL FOR CASE 7. A worktree of THIS repo shares its git-common-dir --
# still core, still blocks -- proving case 7 is keyed on a DIFFERENT repo, not "any worktree".
OWN_WT="$REPO/.claude/worktrees/hooktest-$$"
if "$TEST_GIT" -C "$REPO" worktree add -q --detach "$OWN_WT" HEAD 2>/dev/null; then
  OWN_WT_OUT="$(cd "$OWN_WT" && bash "$HOOK" 2>&1)"
  case "$OWN_WT_OUT" in
    *'"decision":"block"'*) ok "this repo's own worktree is still the core, still blocks" ;;
    *) bad "this repo's own worktree is still the core, still blocks" "got: ${OWN_WT_OUT:0:120}" ;;
  esac
  "$TEST_GIT" -C "$REPO" worktree remove --force "$OWN_WT" 2>/dev/null || rm -rf "$OWN_WT"
else
  printf '  skip own-repo worktree control; could not create one here\n'
fi
rm -f "$WS/tasks/$PROBE"

# 9/10. THE PACKAGED-BUNDLE DEPLOYMENT MATRIX. A shipped app bundle has no
# .git at all, so REPO_COMMON_DIR is empty by design -- pin both adjacent cases.
BUNDLE="$(mktemp -d)"
mkdir -p "$BUNDLE/src" "$BUNDLE/scripts" "$BUNDLE/workspace/tasks" "$BUNDLE/workspace/results"
cp "$REPO/src/check-pending-tasks.sh" "$BUNDLE/src/"
cp "$REPO/scripts/git-binary.sh" "$BUNDLE/scripts/"
BUNDLE_PY="$TEST_PY"
printf '#!/bin/bash\ncase "$1" in\n  workspace) echo "%s/workspace"; exit 0 ;;\n  python-bin) echo "%s"; exit 0 ;;\nesac\nexit 1\n' \
  "$BUNDLE" "$BUNDLE_PY" > "$BUNDLE/scripts/sutando-config.sh"
chmod +x "$BUNDLE/scripts/sutando-config.sh"
printf 'id: probe\ntask: bundle-matrix-probe\n' > "$BUNDLE/workspace/tasks/$PROBE"

# 9. non-Git bundle + a genuinely foreign Git cwd -> must SKIP ({}).
BUNDLE_FOREIGN="$(mktemp -d)"
_git_fixture_repo "$BUNDLE_FOREIGN"
BF_OUT="$(cd "$BUNDLE_FOREIGN" && bash "$BUNDLE/src/$(basename "$HOOK")" 2>&1)"
case "$BF_OUT" in
  '{}') ok "non-Git bundle + foreign Git cwd -> skip (guest carve-out applies)" ;;
  *) bad "non-Git bundle + foreign Git cwd -> skip (guest carve-out applies)" "got: ${BF_OUT:0:120}" ;;
esac
rm -rf "$BUNDLE_FOREIGN"

# 10. non-Git bundle + a NON-Git cwd -> fail closed -- the control proving
# case 9 is keyed on "a different repo", not on "the bundle has no .git".
BUNDLE_CLEAN_CWD="$(mktemp -d)"
BC_OUT="$(cd "$BUNDLE_CLEAN_CWD" && bash "$BUNDLE/src/$(basename "$HOOK")" 2>&1)"
case "$BC_OUT" in
  *'"decision":"block"'*) ok "non-Git bundle + non-Git cwd -> still gates (fail closed)" ;;
  *) bad "non-Git bundle + non-Git cwd -> still gates (fail closed)" "got: ${BC_OUT:0:120}" ;;
esac
rm -rf "$BUNDLE_CLEAN_CWD" "$BUNDLE"

# 11. A REAL checkout whose repo-side git probe FAILS must still GATE -- same
# empty REPO_COMMON_DIR as a packaged bundle, opposite cause, opposite answer.
FAILPROBE="$(mktemp -d)"
mkdir -p "$FAILPROBE/src" "$FAILPROBE/scripts" "$FAILPROBE/workspace/tasks" "$FAILPROBE/workspace/results" "$FAILPROBE/.git"
cp "$REPO/src/check-pending-tasks.sh" "$FAILPROBE/src/"
cp "$REPO/scripts/git-binary.sh" "$FAILPROBE/scripts/"
FP_PY="$TEST_PY"
printf '#!/bin/bash\ncase "$1" in\n  workspace) echo "%s/workspace"; exit 0 ;;\n  python-bin) echo "%s"; exit 0 ;;\nesac\nexit 1\n' \
  "$FAILPROBE" "$FP_PY" > "$FAILPROBE/scripts/sutando-config.sh"
chmod +x "$FAILPROBE/scripts/sutando-config.sh"
printf 'id: probe\ntask: failed-probe-matrix\n' > "$FAILPROBE/workspace/tasks/$PROBE"
FAILPROBE_FOREIGN="$(mktemp -d)"
_git_fixture_repo "$FAILPROBE_FOREIGN"
# A silently-broken init would also leave this empty, blocking via "no identity
# at all" rather than the specific failed-probe ambiguity this case names.
FPF_IDENTITY="$("$TEST_GIT" -C "$FAILPROBE_FOREIGN" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)"
if [ -z "$FPF_IDENTITY" ]; then
  bad "real checkout + failed repo-side git probe -> still gates (ambiguity fails closed)" \
    "fixture bug: FAILPROBE_FOREIGN has no resolvable git identity, this case tests nothing"
else
  FPO_OUT="$(cd "$FAILPROBE_FOREIGN" && bash "$FAILPROBE/src/$(basename "$HOOK")" 2>&1)"
  case "$FPO_OUT" in
    *'"decision":"block"'*) ok "real checkout + failed repo-side git probe -> still gates (ambiguity fails closed)" ;;
    *) bad "real checkout + failed repo-side git probe -> still gates (ambiguity fails closed)" "got: ${FPO_OUT:0:120}" ;;
  esac
fi
rm -rf "$FAILPROBE_FOREIGN" "$FAILPROBE"

# 12. A REAL checkout with NO .git of its OWN (an inner child with the .git
# only in the OUTER parent) but a RESOLVED identity equal to the cwd's must still be core.
OUTER_REPO="$(mktemp -d)"
_git_fixture_repo "$OUTER_REPO"
NESTED_REPO="$OUTER_REPO/child"
mkdir -p "$NESTED_REPO/src" "$NESTED_REPO/scripts" "$NESTED_REPO/workspace/tasks" "$NESTED_REPO/workspace/results"
cp "$REPO/src/check-pending-tasks.sh" "$NESTED_REPO/src/"
cp "$REPO/scripts/git-binary.sh" "$NESTED_REPO/scripts/"
NR_PY="$TEST_PY"
printf '#!/bin/bash\ncase "$1" in\n  workspace) echo "%s/workspace"; exit 0 ;;\n  python-bin) echo "%s"; exit 0 ;;\nesac\nexit 1\n' \
  "$NESTED_REPO" "$NR_PY" > "$NESTED_REPO/scripts/sutando-config.sh"
chmod +x "$NESTED_REPO/scripts/sutando-config.sh"
printf 'id: probe\ntask: nested-subdir-probe\n' > "$NESTED_REPO/workspace/tasks/$PROBE"
NR_REPO_ID="$("$TEST_GIT" -C "$NESTED_REPO" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)"
NR_CWD_ID="$("$TEST_GIT" -C "$OUTER_REPO" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)"
if [ -e "$NESTED_REPO/.git" ]; then
  bad "no-own-.git subdir of a real repo, matching cwd identity -> still core, still blocks" \
    "fixture bug: $NESTED_REPO/.git exists, this case tests nothing"
elif [ -z "$NR_REPO_ID" ] || [ -z "$NR_CWD_ID" ] || [ "$NR_REPO_ID" != "$NR_CWD_ID" ]; then
  bad "no-own-.git subdir of a real repo, matching cwd identity -> still core, still blocks" \
    "fixture bug: identities don't both resolve equal -- child='$NR_REPO_ID' outer='$NR_CWD_ID', this case tests nothing"
else
  # Run from the OUTER root, not the child -- REPO_COMMON_DIR (walked up from
  # the child) must equal CWD_COMMON_DIR (resolved directly at the outer root).
  NR_OUT="$(cd "$OUTER_REPO" && bash "$NESTED_REPO/src/$(basename "$HOOK")" 2>&1)"
  case "$NR_OUT" in
    *'"decision":"block"'*) ok "no-own-.git subdir of a real repo, matching cwd identity -> still core, still blocks" ;;
    *) bad "no-own-.git subdir of a real repo, matching cwd identity -> still core, still blocks" "got: ${NR_OUT:0:120}" ;;
  esac
fi
rm -rf "$OUTER_REPO"

# 13. A DANGLING `.git` SYMLINK is marker-PRESENT (ambiguous), not marker-absent
# -- `-e` alone would misread a broken checkout as an intentional bundle.
DANGLING="$(mktemp -d)"
mkdir -p "$DANGLING/src" "$DANGLING/scripts" "$DANGLING/workspace/tasks" "$DANGLING/workspace/results"
cp "$REPO/src/check-pending-tasks.sh" "$DANGLING/src/"
cp "$REPO/scripts/git-binary.sh" "$DANGLING/scripts/"
DL_PY="$TEST_PY"
printf '#!/bin/bash\ncase "$1" in\n  workspace) echo "%s/workspace"; exit 0 ;;\n  python-bin) echo "%s"; exit 0 ;;\nesac\nexit 1\n' \
  "$DANGLING" "$DL_PY" > "$DANGLING/scripts/sutando-config.sh"
chmod +x "$DANGLING/scripts/sutando-config.sh"
printf 'id: probe\ntask: dangling-symlink-probe\n' > "$DANGLING/workspace/tasks/$PROBE"
ln -s "/nonexistent-target-$$" "$DANGLING/.git"
DANGLING_FOREIGN="$(mktemp -d)"
_git_fixture_repo "$DANGLING_FOREIGN"
DLF_IDENTITY="$("$TEST_GIT" -C "$DANGLING_FOREIGN" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)"
if [ -z "$DLF_IDENTITY" ]; then
  bad "a dangling .git symlink is marker-present, ambiguous -> still gates" \
    "fixture bug: DANGLING_FOREIGN has no resolvable git identity, this case tests nothing"
else
  DL_OUT="$(cd "$DANGLING_FOREIGN" && bash "$DANGLING/src/$(basename "$HOOK")" 2>&1)"
  case "$DL_OUT" in
    *'"decision":"block"'*) ok "a dangling .git symlink is marker-present, ambiguous -> still gates" ;;
    *) bad "a dangling .git symlink is marker-present, ambiguous -> still gates" "got: ${DL_OUT:0:120}" ;;
  esac
fi
rm -rf "$DANGLING_FOREIGN" "$DANGLING"

# 14. RESOLVER-EMPTY, POISON-STUB INTEGRATION CASE. A symlink-to-system-git on
# PATH must leave GIT_BIN empty (fail closed), catching a `GIT_BIN=git` bypass.
POISON="$(mktemp -d)"
mkdir -p "$POISON/src" "$POISON/scripts" "$POISON/workspace/tasks" "$POISON/workspace/results"
cp "$REPO/src/check-pending-tasks.sh" "$POISON/src/"
cp "$REPO/scripts/git-binary.sh" "$POISON/scripts/"
PS_PY="$TEST_PY"
printf '#!/bin/bash\ncase "$1" in\n  workspace) echo "%s/workspace"; exit 0 ;;\n  python-bin) echo "%s"; exit 0 ;;\nesac\nexit 1\n' \
  "$POISON" "$PS_PY" > "$POISON/scripts/sutando-config.sh"
chmod +x "$POISON/scripts/sutando-config.sh"
printf 'id: probe\ntask: resolver-empty-probe\n' > "$POISON/workspace/tasks/$PROBE"
_git_fixture_repo "$POISON"
POISON_FOREIGN="$(mktemp -d)"
_git_fixture_repo "$POISON_FOREIGN"
PSF_IDENTITY="$("$TEST_GIT" -C "$POISON_FOREIGN" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)"
STUBDIR="$(mktemp -d)"
ln -s /usr/bin/git "$STUBDIR/git"
XCS_LOG="$STUBDIR/xcode-select-calls.log"
printf '#!/bin/sh\necho "$@" >> %s\nexit 2\n' "$XCS_LOG" > "$STUBDIR/xcode-select"
chmod +x "$STUBDIR/xcode-select"
if [ -z "$PSF_IDENTITY" ]; then
  bad "resolver-empty (poison stub on PATH): GIT_BIN stays unset, hook still gates" \
    "fixture bug: POISON_FOREIGN has no resolvable git identity, this case tests nothing"
else
  PS_OUT="$(cd "$POISON_FOREIGN" && OSTYPE=darwin25 PATH="$STUBDIR:/usr/bin:/bin" bash "$POISON/src/$(basename "$HOOK")" 2>&1)"
  case "$PS_OUT" in
    *'"decision":"block"'*) ok "resolver-empty (poison stub on PATH): GIT_BIN stays unset, hook still gates" ;;
    *) bad "resolver-empty (poison stub on PATH): GIT_BIN stays unset, hook still gates" "got: ${PS_OUT:0:160} -- a GIT_BIN=git bypass would see this PATH's git as usable and wrongly skip" ;;
  esac
fi
# EXACTLY 1, not >=1 -- and "never executed to decide" is proven once, generically,
# by tests/git-binary-sh.test.sh's stub-ran witness (same code path, any candidate).
if [ -f "$XCS_LOG" ] && [ "$(wc -l < "$XCS_LOG")" -eq 1 ]; then
  ok "the developer-tools probe ran exactly once (resolve_git was exercised, not bypassed)"
else
  bad "the developer-tools probe ran exactly once (resolve_git was exercised, not bypassed)" \
    "$([ -f "$XCS_LOG" ] && cat "$XCS_LOG" || echo "xcode-select was never invoked")"
fi
rm -rf "$POISON" "$POISON_FOREIGN" "$STUBDIR"

if [ "$FAILED" -eq 0 ]; then echo "PASS"; else echo "FAIL"; fi
exit "$FAILED"
