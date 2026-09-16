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

# Fixture setup below needs a REAL git, not whatever a bare `git` resolves to
# on this box (the macOS CLT stub included) -- go through the same resolver
# the hook itself uses, so the suite is not exposed to the exact failure mode
# it tests for.
. "$REPO/scripts/git-binary.sh"
TEST_GIT="$(resolve_git)"
if [ -z "$TEST_GIT" ]; then
  echo "FAIL: no usable git found to build test fixtures with."
  exit 1
fi

# --- Build and PIN an isolated workspace before resolving anything ----------
TMPWS="$(mktemp -d "${TMPDIR:-/tmp}/sutando-hooktest.XXXXXX")"

# The hook now also refuses a turn that ends with no message and no recorded
# no-send. These cases assert the TASK gate, so satisfy the turn gate first or
# they measure the wrong refusal.
record_delivery() {
  "$(bash "$REPO/scripts/sutando-config.sh" python-bin 2>/dev/null || echo python3)" \
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

# 6. THE REJECTION PATH. When the resolver refuses an interpreter the hook must
# not execute a bare `python3` — that is the CLT stub the resolver just declined.
# scripts/git-binary.sh is COPIED IN and a recording `git` stub proves it ran,
# so this is a witness to activation, not just to output shape.
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
# A recording git stub, so git-binary.sh's resolve_git() actually has something
# to resolve to (and its two call sites something to record) rather than the
# whole block quietly no-opping on an absent/rejected git. Both probes echo
# the SAME real, existing directory ($REJ), so CWD/REPO canonicalize equal --
# a deliberate "same identity" case, so the block falls through normally
# (never skips) into the interpreter-rejection logic this case actually tests.
printf '#!/bin/bash\necho "GIT_CALLED $*" >> "%s/git-calls.log"\necho "%s"\n' "$REJ" "$REJ" \
  > "$REJ/git"
chmod +x "$REJ/git"
printf '#!/bin/sh\nexit 2\n' > "$REJ/xcode-select"
chmod +x "$REJ/xcode-select"
# Run from a NON-Git cwd (never this checkout) so the guest carve-out's own
# fall-through ("cannot prove different, proceed as core") is what lets
# execution continue into the interpreter-rejection logic below it, rather
# than either a real git identity here OR a short-circuiting guest exit
# masking whether that logic ran at all.
REJ_CWD="$(mktemp -d)"
REJ_ERR="$(cd "$REJ_CWD" && OSTYPE=darwin25 PATH="$REJ:$PATH" bash "$REJ/src/$(basename "$HOOK")" 2>&1 >/dev/null)"
rm -f "$REJ/git-calls.log"
REJ_OUT="$(cd "$REJ_CWD" && OSTYPE=darwin25 PATH="$REJ:$PATH" bash "$REJ/src/$(basename "$HOOK")" 2>/dev/null)"
case "$REJ_ERR" in
  *FALLBACK_INVOKED*) bad "a refused interpreter is not worked around" "the bare python3 fallback ran" ;;
  *) ok "a refused interpreter is not worked around" ;;
esac
# Not "no stderr at all" -- the hook's own deliberate interpreter-refusal
# message belongs there. Specifically absent: the symptom of git-binary.sh
# failing to source (a missing file, or calling the then-undefined resolve_git).
case "$REJ_ERR" in
  *"No such file or directory"*|*"command not found"*)
    bad "no source/command-not-found noise (git-binary.sh actually sourced, not silently missing)" "got: ${REJ_ERR:0:160}" ;;
  *) ok "no source/command-not-found noise (git-binary.sh actually sourced, not silently missing)" ;;
esac
case "$REJ_OUT" in
  '{}') ok "a refused interpreter still emits valid JSON" ;;
  *) bad "a refused interpreter still emits valid JSON" "got: ${REJ_OUT:0:120}" ;;
esac
if [ -f "$REJ/git-calls.log" ] && [ "$(wc -l < "$REJ/git-calls.log")" -ge 2 ]; then
  ok "the git stub was actually invoked (git-binary.sh's resolve_git activated, not skipped)"
else
  bad "the git stub was actually invoked (git-binary.sh's resolve_git activated, not skipped)" \
    "$([ -f "$REJ/git-calls.log" ] && cat "$REJ/git-calls.log" || echo "no log file -- git never ran")"
fi
rm -rf "$REJ_CWD"
# 7. THE GUEST CARVE-OUT. A cwd inside a worktree of an UNRELATED repo (its own
# git-common-dir) must not be held hostage by the core's queue, even with a
# real pending task sitting in it.
printf 'id: probe\ntask: guest-worktree-probe\n' > "$WS/tasks/$PROBE"
GUEST_REPO="$(mktemp -d)"
(cd "$GUEST_REPO" && "$TEST_GIT" init -q && \
   "$TEST_GIT" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init)
GUEST_OUT="$(cd "$GUEST_REPO" && bash "$HOOK" 2>&1)"
case "$GUEST_OUT" in
  '{}') ok "unrelated-repo worktree is not blocked by the core's queue" ;;
  *) bad "unrelated-repo worktree is not blocked by the core's queue" "got: ${GUEST_OUT:0:120}" ;;
esac
rm -rf "$GUEST_REPO"

# 8. CONTROL FOR CASE 7. A cwd inside a worktree OF THIS SAME REPO shares its
# git-common-dir, so it is still the core, not a guest — the same pending task
# must still block there. Without this, a carve-out keyed on "any worktree"
# rather than "a DIFFERENT repo's worktree" would pass case 7 by disabling the
# gate for every worktree, this repo's own included.
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
BUNDLE_PY="$(bash "$REPO/scripts/sutando-config.sh" python-bin 2>/dev/null || echo python3)"
printf '#!/bin/bash\ncase "$1" in\n  workspace) echo "%s/workspace"; exit 0 ;;\n  python-bin) echo "%s"; exit 0 ;;\nesac\nexit 1\n' \
  "$BUNDLE" "$BUNDLE_PY" > "$BUNDLE/scripts/sutando-config.sh"
chmod +x "$BUNDLE/scripts/sutando-config.sh"
printf 'id: probe\ntask: bundle-matrix-probe\n' > "$BUNDLE/workspace/tasks/$PROBE"

# 9. non-Git bundle + a genuinely foreign Git cwd -> must SKIP ({}).
BUNDLE_FOREIGN="$(mktemp -d)"
(cd "$BUNDLE_FOREIGN" && "$TEST_GIT" init -q && \
   "$TEST_GIT" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init)
BF_OUT="$(cd "$BUNDLE_FOREIGN" && bash "$BUNDLE/src/$(basename "$HOOK")" 2>&1)"
case "$BF_OUT" in
  '{}') ok "non-Git bundle + foreign Git cwd -> skip (guest carve-out applies)" ;;
  *) bad "non-Git bundle + foreign Git cwd -> skip (guest carve-out applies)" "got: ${BF_OUT:0:120}" ;;
esac
rm -rf "$BUNDLE_FOREIGN"

# 10. non-Git bundle + a NON-Git cwd -> cannot prove different, fail closed
# (still gate/block) -- the control that proves case 9 is keyed on "a
# different repo", not on "the bundle has no .git" alone.
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
FP_PY="$(bash "$REPO/scripts/sutando-config.sh" python-bin 2>/dev/null || echo python3)"
printf '#!/bin/bash\ncase "$1" in\n  workspace) echo "%s/workspace"; exit 0 ;;\n  python-bin) echo "%s"; exit 0 ;;\nesac\nexit 1\n' \
  "$FAILPROBE" "$FP_PY" > "$FAILPROBE/scripts/sutando-config.sh"
chmod +x "$FAILPROBE/scripts/sutando-config.sh"
printf 'id: probe\ntask: failed-probe-matrix\n' > "$FAILPROBE/workspace/tasks/$PROBE"
FAILPROBE_FOREIGN="$(mktemp -d)"
(cd "$FAILPROBE_FOREIGN" && "$TEST_GIT" init -q && \
   "$TEST_GIT" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init)
FPO_OUT="$(cd "$FAILPROBE_FOREIGN" && bash "$FAILPROBE/src/$(basename "$HOOK")" 2>&1)"
case "$FPO_OUT" in
  *'"decision":"block"'*) ok "real checkout + failed repo-side git probe -> still gates (ambiguity fails closed)" ;;
  *) bad "real checkout + failed repo-side git probe -> still gates (ambiguity fails closed)" "got: ${FPO_OUT:0:120}" ;;
esac
rm -rf "$FAILPROBE_FOREIGN" "$FAILPROBE"

# 12. A REAL checkout with NO .git of its OWN (a subdir of a real repo) but a
# RESOLVED identity equal to the cwd's must still be core -- marker-absence
# must never override a known, matching identity. The hook tree lives in an
# INNER child with NO .git of its own; the .git lives only in the OUTER
# parent. (The prior version of this case ran `git init` directly in the
# hook's own REPO_DIR, which gave it a `.git` after all and made the case
# indistinguishable from the ordinary same-repo case -- it passed under the
# OLD marker-first code too, so it was not exercising the new behavior.)
OUTER_REPO="$(mktemp -d)"
(cd "$OUTER_REPO" && "$TEST_GIT" init -q && "$TEST_GIT" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init)
NESTED_REPO="$OUTER_REPO/child"
mkdir -p "$NESTED_REPO/src" "$NESTED_REPO/scripts" "$NESTED_REPO/workspace/tasks" "$NESTED_REPO/workspace/results"
cp "$REPO/src/check-pending-tasks.sh" "$NESTED_REPO/src/"
cp "$REPO/scripts/git-binary.sh" "$NESTED_REPO/scripts/"
NR_PY="$(bash "$REPO/scripts/sutando-config.sh" python-bin 2>/dev/null || echo python3)"
printf '#!/bin/bash\ncase "$1" in\n  workspace) echo "%s/workspace"; exit 0 ;;\n  python-bin) echo "%s"; exit 0 ;;\nesac\nexit 1\n' \
  "$NESTED_REPO" "$NR_PY" > "$NESTED_REPO/scripts/sutando-config.sh"
chmod +x "$NESTED_REPO/scripts/sutando-config.sh"
printf 'id: probe\ntask: nested-subdir-probe\n' > "$NESTED_REPO/workspace/tasks/$PROBE"
if [ -e "$NESTED_REPO/.git" ]; then
  bad "no-own-.git subdir of a real repo, matching cwd identity -> still core, still blocks" \
    "fixture bug: $NESTED_REPO/.git exists, this case tests nothing"
else
  # Run from the OUTER repo's root, not the child -- same repo, different dir,
  # so REPO_COMMON_DIR (resolved by walking up from the child) must equal
  # CWD_COMMON_DIR (resolved directly at the outer root).
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
DL_PY="$(bash "$REPO/scripts/sutando-config.sh" python-bin 2>/dev/null || echo python3)"
printf '#!/bin/bash\ncase "$1" in\n  workspace) echo "%s/workspace"; exit 0 ;;\n  python-bin) echo "%s"; exit 0 ;;\nesac\nexit 1\n' \
  "$DANGLING" "$DL_PY" > "$DANGLING/scripts/sutando-config.sh"
chmod +x "$DANGLING/scripts/sutando-config.sh"
printf 'id: probe\ntask: dangling-symlink-probe\n' > "$DANGLING/workspace/tasks/$PROBE"
ln -s "/nonexistent-target-$$" "$DANGLING/.git"
DANGLING_FOREIGN="$(mktemp -d)"
(cd "$DANGLING_FOREIGN" && "$TEST_GIT" init -q && "$TEST_GIT" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init)
DL_OUT="$(cd "$DANGLING_FOREIGN" && bash "$DANGLING/src/$(basename "$HOOK")" 2>&1)"
case "$DL_OUT" in
  *'"decision":"block"'*) ok "a dangling .git symlink is marker-present, ambiguous -> still gates" ;;
  *) bad "a dangling .git symlink is marker-present, ambiguous -> still gates" "got: ${DL_OUT:0:120}" ;;
esac
rm -rf "$DANGLING_FOREIGN" "$DANGLING"

# 14. RESOLVER-EMPTY, POISON-STUB INTEGRATION CASE. `resolve_git` must refuse
# every candidate on PATH (a symlink to the real system git, classified as the
# CLT stub when developer tools are absent) -- so GIT_BIN stays "" and the hook
# takes the no-runnable-git fail-closed path, never a raw `git` lookup. This is
# the case a `GIT_BIN="$(resolve_git)"` -> `GIT_BIN=git` bypass slips past: PATH
# still resolves plain `git` to a WORKING binary (via the symlink), so a bypass
# would successfully compare identities across two real repos and (wrongly)
# skip, while the correct hook -- unable to use that candidate -- can't compare
# and must block instead. The two differ only in whether GIT_BIN honors the
# refusal, which is exactly the assignment line the mutation targets.
POISON="$(mktemp -d)"
mkdir -p "$POISON/src" "$POISON/scripts" "$POISON/workspace/tasks" "$POISON/workspace/results"
cp "$REPO/src/check-pending-tasks.sh" "$POISON/src/"
cp "$REPO/scripts/git-binary.sh" "$POISON/scripts/"
PS_PY="$(bash "$REPO/scripts/sutando-config.sh" python-bin 2>/dev/null || echo python3)"
printf '#!/bin/bash\ncase "$1" in\n  workspace) echo "%s/workspace"; exit 0 ;;\n  python-bin) echo "%s"; exit 0 ;;\nesac\nexit 1\n' \
  "$POISON" "$PS_PY" > "$POISON/scripts/sutando-config.sh"
chmod +x "$POISON/scripts/sutando-config.sh"
printf 'id: probe\ntask: resolver-empty-probe\n' > "$POISON/workspace/tasks/$PROBE"
(cd "$POISON" && "$TEST_GIT" init -q && "$TEST_GIT" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init)
POISON_FOREIGN="$(mktemp -d)"
(cd "$POISON_FOREIGN" && "$TEST_GIT" init -q && "$TEST_GIT" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init)
STUBDIR="$(mktemp -d)"
ln -s /usr/bin/git "$STUBDIR/git"
XCS_LOG="$STUBDIR/xcode-select-calls.log"
printf '#!/bin/sh\necho "$@" >> %s\nexit 2\n' "$XCS_LOG" > "$STUBDIR/xcode-select"
chmod +x "$STUBDIR/xcode-select"
PS_OUT="$(cd "$POISON_FOREIGN" && OSTYPE=darwin25 PATH="$STUBDIR:/usr/bin:/bin" bash "$POISON/src/$(basename "$HOOK")" 2>&1)"
case "$PS_OUT" in
  *'"decision":"block"'*) ok "resolver-empty (poison stub on PATH): GIT_BIN stays unset, hook still gates" ;;
  *) bad "resolver-empty (poison stub on PATH): GIT_BIN stays unset, hook still gates" "got: ${PS_OUT:0:160} -- a GIT_BIN=git bypass would see this PATH's git as usable and wrongly skip" ;;
esac
if [ -f "$XCS_LOG" ] && [ "$(wc -l < "$XCS_LOG")" -ge 1 ]; then
  ok "the developer-tools probe actually ran (resolve_git was exercised, not bypassed)"
else
  bad "the developer-tools probe actually ran (resolve_git was exercised, not bypassed)" "xcode-select was never invoked"
fi
rm -rf "$POISON" "$POISON_FOREIGN" "$STUBDIR"

if [ "$FAILED" -eq 0 ]; then echo "PASS"; else echo "FAIL"; fi
exit "$FAILED"
