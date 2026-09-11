#!/usr/bin/env bash
# The witness-owed gate calls the vault sync to refresh the fleet's records. The
# default tick returns the PUSH's rc, so a REFUSED PULL exits 0 and the gate
# reads "records are current" over a view that never updated — and activates the
# very head an owed record was holding (#3717, keweichen + john-the-dev).
# Real git, real upgrade.sh, real witness_owed.py; the vault sync is stubbed so
# nothing touches a real vault. The control is the pre-fix call line.
set -eu
REPO="$(cd "$(dirname "$0")/.." && pwd -P)"
HEAD_SCRIPT="$REPO/skills/self-upgrade/scripts/upgrade.sh"
TMPROOT="$(mktemp -d "${TMPDIR:-/tmp}/self-upgrade-strict.XXXXXX")"
trap 'rm -rf "$TMPROOT"' EXIT
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
pass=0; fail=0
ok()  { echo "  OK: $1"; pass=$((pass+1)); }
bad() { echo "  FAIL: $1"; fail=$((fail+1)); }

# The control is the pre-fix call built FROM the shipped script, and asserted to
# differ from it: a no-op substitution would silently test nothing.
CONTROL_SCRIPT="$TMPROOT/upgrade-control.sh"
python3 - "$HEAD_SCRIPT" "$CONTROL_SCRIPT" <<'PY'
import re, sys
src, dst = sys.argv[1], sys.argv[2]
s = open(src).read()
pre = ('  bash "$REPO/scripts/sync-workspace.sh" || { echo "self-upgrade: ABORT — vault sync failed, '
       'so the fleet\'s witness-owed records cannot be called fresh" >&2; exit 4; }\n')
out = re.sub(r'  GATE_SYNC_RC=0\n.*?\n  fi\n', pre, s, count=1, flags=re.S)
open(dst, "w").write(out)
sys.exit(0 if out != s else 1)
PY
ok "control built: the pre-fix bare default-tick call, and it differs from HEAD's"

# A fixture checkout: upgrade.sh at its real depth, the real gate helper, a
# stub sutando-config.sh, a stub restart.sh (never reached: --no-restart).
make_fixture() { # root sync_mode(refused|healthy|pushfail) script
  # The remote's path ends in owner/repo so upgrade.sh derives the same repo key
  # the record carries; a record for another project must not scope in.
  local root="$1" mode="$2" script="$3" remote="$1.remote/owner/repo.git"
  mkdir -p "$(dirname "$remote")"
  mkdir -p "$root/skills/self-upgrade/scripts" "$root/src" "$root/scripts" \
           "$root/workspace/hosts/testhost/witness-owed" "$root/peer-store/hosts/peerhost/witness-owed"
  cp "$REPO/src/witness_owed.py" "$root/src/witness_owed.py"
  cp "$script" "$root/skills/self-upgrade/scripts/upgrade.sh"
  cat > "$root/scripts/sutando-config.sh" <<EOF
#!/bin/bash
case "\${1:-}" in
  workspace) printf '%s\n' "$root/workspace" ;;
  python-bin) command -v python3 ;;
  host-label) printf '%s\n' testhost ;;
  vault-enabled) printf '%s\n' true ;;
esac
EOF
  # Stub vault sync with the REAL contract: default returns the push's rc, so a
  # refused pull still exits 0; --pull-strict lets the pull leg answer.
  cat > "$root/scripts/sync-workspace.sh" <<EOF
#!/bin/bash
mode="$mode"; strict=0; [ "\${1:-}" = "--pull-strict" ] && strict=1
pull_rc=0; push_rc=0
case "\$mode" in
  refused)  echo "sync-workspace: REFUSING pull — peer deleted 18 of 22 files." >&2; pull_rc=1 ;;
  pushfail) push_rc=1 ;;
esac
# A successful pull is what delivers the peer's records; a refused one does not.
[ "\$pull_rc" = "0" ] && cp -R "$root/peer-store/hosts/." "$root/workspace/hosts/"
[ "\$strict" = "1" ] && [ "\$pull_rc" != "0" ] && { echo "sync-workspace: STRICT FAILURE — pull leg exited \$pull_rc" >&2; exit 3; }
exit "\$push_rc"
EOF
  chmod +x "$root/scripts/sutando-config.sh" "$root/scripts/sync-workspace.sh"
  printf '#!/bin/bash\nexit 0\n' > "$root/src/restart.sh"; chmod +x "$root/src/restart.sh"
  printf 'workspace/\npeer-store/\n' > "$root/.gitignore"
  git init -q -b main "$root"
  ( cd "$root" && git add -A && git commit -qm init && git remote add origin "$remote" )
  git init -q -b main --bare "$remote"
  ( cd "$root" && git push -q -u origin main )
  # One upstream commit: the target the gate decides about.
  local work="$root.pusher"
  git clone -q -b main "$remote" "$work"
  ( cd "$work" && echo change > CHANGELOG && git add -A && git commit -qm "upstream live-path change (#77)" && git push -q origin main )
  TARGET_SHA="$(git -C "$work" rev-parse HEAD)"
  rm -rf "$work"
  # The peer host opened a hold on that exact head. It reaches this host ONLY
  # through a successful pull — that is the whole point of the freshness gate.
  python3 "$root/src/witness_owed.py" --workspace "$root/peer-store" open "owner/repo#77" \
    --head "$TARGET_SHA" --host peerhost --reason "live path; no supervised job here" --by peerhost >/dev/null
  python3 "$root/src/witness_owed.py" --workspace "$root/peer-store" publish --host peerhost >/dev/null
}

run_upgrade() { # root -> RC, OUT, HEAD_AFTER
  local root="$1"
  set +e
  OUT="$(cd "$root" && SUTANDO_TEST_MODE=1 SUTANDO_WITNESS_MAX_AGE=86400 \
         bash "$root/skills/self-upgrade/scripts/upgrade.sh" --no-restart 2>&1)"
  RC=$?
  set -e
  HEAD_AFTER="$(git -C "$root" rev-parse HEAD)"
}

echo "-- scenario 1: the pull is refused (keweichen's repro) --"
CTL="$TMPROOT/ctl"; make_fixture "$CTL" refused "$CONTROL_SCRIPT"; CTL_BEFORE="$(git -C "$CTL" rev-parse HEAD)"
run_upgrade "$CTL"; CTL_RC=$RC; CTL_HEAD="$HEAD_AFTER"; CTL_OUT="$OUT"
echo "$CTL_OUT" | grep -q "REFUSING pull" && ok "control: the stub's pull really was refused" || bad "control: no refusal in the stub output"
[ "$CTL_RC" = "0" ] && [ "$CTL_HEAD" != "$CTL_BEFORE" ] \
  && ok "CONTROL (pre-fix): rc=0 and HEAD ADVANCED past the owed #77 — the gate was bypassed" \
  || bad "control did not reproduce the bypass (rc=$CTL_RC, head_advanced=$([ "$CTL_HEAD" != "$CTL_BEFORE" ] && echo true || echo false))"

FIX="$TMPROOT/fix"; make_fixture "$FIX" refused "$HEAD_SCRIPT"; FIX_BEFORE="$(git -C "$FIX" rev-parse HEAD)"
run_upgrade "$FIX"; FIX_RC=$RC; FIX_HEAD="$HEAD_AFTER"; FIX_OUT="$OUT"
[ "$FIX_RC" = "4" ] && [ "$FIX_HEAD" = "$FIX_BEFORE" ] \
  && ok "THE POINT (HEAD): rc=4 and HEAD UNMOVED on the identical fixture" \
  || bad "HEAD did not abort (rc=$FIX_RC, head_advanced=$([ "$FIX_HEAD" != "$FIX_BEFORE" ] && echo true || echo false)): $(echo "$FIX_OUT" | tail -2)"
echo "$FIX_OUT" | grep -q "PULL leg failed" \
  && ok "the abort names the PULL leg, not 'vault sync failed'" || bad "the abort does not name the pull leg"

echo "-- scenario 2: a healthy sync still upgrades (no false alarm) --"
OKF="$TMPROOT/okf"; make_fixture "$OKF" healthy "$HEAD_SCRIPT"; OK_BEFORE="$(git -C "$OKF" rev-parse HEAD)"
run_upgrade "$OKF"; OK_RC=$RC; OK_HEAD="$HEAD_AFTER"; OK_OUT="$OUT"
# With the pull working, the peer's hold DOES arrive — and then the gate blocks
# on the record itself (rc 4), which is the gate working, not the seam failing.
[ "$OK_RC" = "4" ] && echo "$OK_OUT" | grep -q "owner/repo#77" \
  && ok "healthy pull: the peer's hold arrived and the gate blocked on owner/repo#77" \
  || bad "healthy pull: expected a record-based block, got rc=$OK_RC: $(echo "$OK_OUT" | tail -2)"
echo "$OK_OUT" | grep -q "PULL leg failed" && bad "healthy pull wrongly reported a pull failure" || ok "healthy pull reports no sync failure"
[ "$OK_HEAD" = "$OK_BEFORE" ] && ok "healthy pull: HEAD unmoved while the record is open" || bad "healthy pull advanced HEAD over an open record"

echo "-- scenario 3: the push leg fails; the pull succeeded --"
PF="$TMPROOT/pf"; make_fixture "$PF" pushfail "$HEAD_SCRIPT"
run_upgrade "$PF"
[ "$RC" = "4" ] && echo "$OUT" | grep -q "push leg" \
  && ok "a failed push still aborts, named as the push leg (exit 1, not 3)" \
  || bad "push-leg failure not reported: rc=$RC: $(echo "$OUT" | tail -2)"

echo "-- the wiring itself --"
grep -q 'sync-workspace.sh" --pull-strict' "$HEAD_SCRIPT" \
  && ok "upgrade.sh asks for --pull-strict" || bad "upgrade.sh no longer asks for --pull-strict"
grep -q -- "--pull-strict" "$REPO/scripts/sync-workspace.sh" \
  && ok "sync-workspace.sh owns the strict mode (no sync logic copied into upgrade.sh)" \
  || bad "sync-workspace.sh does not implement --pull-strict"

echo "$pass passed, $fail failed"
[ "$fail" = "0" ]
