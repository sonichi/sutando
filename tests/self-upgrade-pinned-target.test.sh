#!/usr/bin/env bash
# The gate decided about one resolution of $REMOTE/$BRANCH and `git pull` then
# fetched a second one: a remote that moves between the two activates a head the
# gate never saw (#3717, keweichen P1-2). Real git, real upgrade.sh, real
# witness_owed.py; the vault sync is stubbed and moves the remote mid-run so the
# window is deterministic. The control is the pre-fix check+pull pair.
set -eu
REPO="$(cd "$(dirname "$0")/.." && pwd -P)"
HEAD_SCRIPT="$REPO/skills/self-upgrade/scripts/upgrade.sh"
TMPROOT="$(mktemp -d "${TMPDIR:-/tmp}/self-upgrade-pinned.XXXXXX")"
trap 'rm -rf "$TMPROOT"' EXIT
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
pass=0; fail=0
ok()  { echo "  OK: $1"; pass=$((pass+1)); }
bad() { echo "  FAIL: $1"; fail=$((fail+1)); }

# Control = the shipped script with the two pre-fix lines substituted back, and
# asserted to differ: a no-op substitution would test nothing.
CONTROL_SCRIPT="$TMPROOT/upgrade-control.sh"
python3 - "$HEAD_SCRIPT" "$CONTROL_SCRIPT" <<'PY'
import sys
src, dst = sys.argv[1], sys.argv[2]
s = open(src).read()
out = s.replace('check --ref "$TARGET_SHA"', 'check --ref "$REMOTE/$BRANCH"', 1)
out = out.replace('git merge --ff-only "$TARGET_SHA"',
                  'git pull --ff-only "$REMOTE" "$BRANCH"', 1)
open(dst, "w").write(out)
sys.exit(0 if out.count('"$REMOTE/$BRANCH"') > s.count('"$REMOTE/$BRANCH"') else 1)
PY
ok "control built: the pre-fix 'check the mutable ref, then pull again' pair, and it differs"

# A fixture checkout at upgrade.sh's real depth. T1 is the target the gate sees;
# T2 is the head the remote moves to mid-run, and T2 is the one that is owed.
make_fixture() { # root script
  local root="$1" script="$2" remote="$1.remote/owner/repo.git"
  mkdir -p "$(dirname "$remote")" "$root/skills/self-upgrade/scripts" "$root/src" \
           "$root/scripts" "$root/workspace/hosts/testhost/witness-owed"
  cp "$REPO/src/witness_owed.py" "$root/src/witness_owed.py"
  cp "$script" "$root/skills/self-upgrade/scripts/upgrade.sh"
  cp "$REPO/skills/self-upgrade/manifest.json" "$root/skills/self-upgrade/manifest.json"
  cat > "$root/scripts/sutando-config.sh" <<EOF
#!/bin/bash
case "\${1:-}" in
  workspace) printf '%s\n' "$root/workspace" ;;
  python-bin) command -v python3 ;;
  host-label) printf '%s\n' testhost ;;
  vault-enabled) printf '%s\n' true ;;
esac
EOF
  # The stub sync is the deterministic stand-in for "the remote moved while the
  # gate was running": it succeeds, and it advances origin/main from T1 to T2.
  cat > "$root/scripts/sync-workspace.sh" <<EOF
#!/bin/bash
[ -f "$root.advance" ] && { git -C "$remote" update-ref refs/heads/main "\$(cat "$root.t2")"; rm -f "$root.advance"; }
exit 0
EOF
  chmod +x "$root/scripts/sutando-config.sh" "$root/scripts/sync-workspace.sh"
  printf '#!/bin/bash\nexit 0\n' > "$root/src/restart.sh"; chmod +x "$root/src/restart.sh"
  printf 'workspace/\n__pycache__/\n' > "$root/.gitignore"   # as the real repo ignores them
  git init -q -b main "$root"
  ( cd "$root" && git add -A && git commit -qm init && git remote add origin "$remote" )
  git init -q -b main --bare "$remote"
  ( cd "$root" && git push -q -u origin main )
  local work="$root.pusher"
  git clone -q -b main "$remote" "$work"
  ( cd "$work" && echo one > CHANGELOG && git add -A && git commit -qm "benign doc change" && git push -q origin main )
  T1="$(git -C "$work" rev-parse HEAD)"
  ( cd "$work" && echo two > CHANGELOG && git add -A && git commit -qm "live-path change (#77)" )
  T2="$(git -C "$work" rev-parse HEAD)"
  git -C "$work" push -q origin HEAD:refs/heads/staged   # present in the remote, NOT on main
  rm -rf "$work"
  printf '%s' "$T2" > "$root.t2"; : > "$root.advance"
  # The hold is on T2 and already local, so nothing about this test depends on
  # propagation: the only question is which object the run activates.
  python3 "$root/src/witness_owed.py" --workspace "$root/workspace" open "owner/repo#77" \
    --head "$T2" --host testhost --reason "live path; no supervised job here" --by testhost >/dev/null 2>&1
  python3 "$root/src/witness_owed.py" --workspace "$root/workspace" publish --host testhost >/dev/null
}

run_upgrade() { # root -> RC, OUT, HEAD_AFTER
  local root="$1"
  set +e
  OUT="$(cd "$root" && SUTANDO_WITNESS_MAX_AGE=86400 \
         bash "$root/skills/self-upgrade/scripts/upgrade.sh" --no-restart 2>&1)"
  RC=$?
  set -e
  HEAD_AFTER="$(git -C "$root" rev-parse HEAD)"
}

echo "-- the fixture really does move the remote mid-run --"
CTL="$TMPROOT/ctl"; make_fixture "$CTL" "$CONTROL_SCRIPT"; CTL_T1="$T1"; CTL_T2="$T2"
git -C "$CTL" fetch -q origin
[ "$(git -C "$CTL" rev-parse origin/main)" = "$CTL_T1" ] \
  && ok "before the run, the remote-tracking ref is T1 (${CTL_T1:0:8})" || bad "fixture: origin/main is not T1"
python3 "$CTL/src/witness_owed.py" --workspace "$CTL/workspace" check --ref "$CTL_T1" \
  --repo-root "$CTL" --repo owner/repo --host testhost >/dev/null 2>&1 \
  && ok "T1 is NOT owed — the gate is entitled to pass it" || bad "fixture: T1 already blocked"
set +e
python3 "$CTL/src/witness_owed.py" --workspace "$CTL/workspace" check --ref "$CTL_T2" --current "$CTL_T1" \
  --repo-root "$CTL" --repo owner/repo --host testhost >/dev/null 2>&1; T2_RC=$?
set -e
[ "$T2_RC" = "3" ] && ok "T2 IS owed — the production helper refuses it (exit 3)" || bad "fixture: T2 not blocked (rc=$T2_RC)"

echo "-- the control activates the head the gate never checked --"
run_upgrade "$CTL"; CTL_RC=$RC; CTL_HEAD="$HEAD_AFTER"
[ "$CTL_RC" = "0" ] && [ "$CTL_HEAD" = "$CTL_T2" ] \
  && ok "CONTROL (pre-fix): rc=0 and HEAD is T2 (${CTL_T2:0:8}) — the owed head, never gated" \
  || bad "control did not reproduce the race (rc=$CTL_RC head=${CTL_HEAD:0:8} want ${CTL_T2:0:8})"

echo "-- THE POINT: HEAD fast-forwards to exactly the object it checked --"
FIX="$TMPROOT/fix"; make_fixture "$FIX" "$HEAD_SCRIPT"; FIX_T1="$T1"; FIX_T2="$T2"
run_upgrade "$FIX"; FIX_RC=$RC; FIX_HEAD="$HEAD_AFTER"; FIX_OUT="$OUT"
[ "$FIX_RC" = "0" ] && [ "$FIX_HEAD" = "$FIX_T1" ] \
  && ok "HEAD: rc=0 and HEAD is the PINNED T1 (${FIX_T1:0:8}) on the identical fixture" \
  || bad "HEAD activated something else (rc=$FIX_RC head=${FIX_HEAD:0:8} want ${FIX_T1:0:8}): $(echo "$FIX_OUT" | tail -2)"
[ "$FIX_HEAD" != "$FIX_T2" ] && ok "a stale local ref with a newer remote did NOT pass the owed T2 through" \
  || bad "the owed T2 was activated"
echo "$FIX_OUT" | grep -q "target=${FIX_T1:0:8}" && ok "the run names the object it pinned" || bad "the pinned target is not reported"
[ "$(git -C "$FIX.remote/owner/repo.git" rev-parse refs/heads/main)" = "$FIX_T2" ] \
  && ok "control on the control: the remote really is at T2 while HEAD stopped at T1" \
  || bad "the remote never advanced, so this scenario proved nothing"

echo "-- nothing is lost: the next pass gates T2 on its own --"
run_upgrade "$FIX"; NEXT_RC=$RC; NEXT_HEAD="$HEAD_AFTER"; NEXT_OUT="$OUT"
[ "$NEXT_RC" = "4" ] && [ "$NEXT_HEAD" = "$FIX_T1" ] && echo "$NEXT_OUT" | grep -q "owner/repo#77" \
  && ok "second pass: rc=4 on owner/repo#77 and HEAD still T1" \
  || bad "second pass did not block on the owed record (rc=$NEXT_RC): $(echo "$NEXT_OUT" | tail -2)"

echo "-- the wiring itself --"
grep -q 'git pull --ff-only' "$HEAD_SCRIPT" && bad "upgrade.sh still re-fetches with git pull" \
  || ok "upgrade.sh no longer re-fetches between the gate and the activation"
grep -q 'check --ref "\$TARGET_SHA"' "$HEAD_SCRIPT" && ok "the gate checks the pinned SHA" || bad "the gate does not check the pinned SHA"

echo "$pass passed, $fail failed"
[ "$fail" = "0" ]
