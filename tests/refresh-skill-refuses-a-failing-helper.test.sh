#!/usr/bin/env bash
# A config helper that exists but fails must stop the script with a named
# failure, never fall back to ~/.claude/skills and report the skill NOT INSTALLED.
set -eu
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
fails=0
ok()   { echo "  ok  $1"; }
fail() { echo "FAIL: $1"; fails=$((fails+1)); }
SCRIPT="${REFRESH_SKILL_UNDER_TEST:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)/skills/refresh-skill.sh}"
[ -f "$SCRIPT" ] || { echo "cannot find refresh-skill.sh"; exit 1; }

REPO="$TMP/repo"; DST="$TMP/skills-dst"; SRC="$TMP/skill-src/demo"
mkdir -p "$REPO/scripts" "$REPO/skills" "$DST" "$SRC"
cp "$SCRIPT" "$REPO/skills/refresh-skill.sh"
echo "# demo" > "$SRC/SKILL.md"; ln -s "$SRC" "$DST/demo"
helper_ok()   { printf '#!/usr/bin/env bash\n[ "${1:-}" = "claude-home-path" ] && { echo "%s"; exit 0; }\nexit 1\n' "$DST" > "$REPO/scripts/sutando-config.sh"; }
helper_fail() { printf '#!/usr/bin/env bash\necho "config: no workspace resolves from here" >&2\nexit 1\n' > "$REPO/scripts/sutando-config.sh"; }
helper_empty() { printf '#!/usr/bin/env bash\nexit 0\n' > "$REPO/scripts/sutando-config.sh"; }
# env -u: a caller's SUTANDO_REPO_DIR must not leak into the cases that test its absence.
run() { (cd "$TMP" && env -u SUTANDO_REPO_DIR REFRESH_SKILL_SETTLE_S=0 HOME="$TMP/home" "$@" bash "$REPO/skills/refresh-skill.sh" demo 2>&1); }
mkdir -p "$TMP/home/.claude/skills"

# --- the defect: helper present and failing -----------------------------------
helper_fail
set +e; out="$(run env)"; rc=$?; set -e
[ "$rc" -eq 3 ] && ok "a failing helper exits 3" || fail "a failing helper exited $rc, expected 3. Got: $out"
case "$out" in *"$REPO/scripts/sutando-config.sh"*"failed"*) ok "the failure names the helper" ;; *) fail "the failure does not name the helper. Got: $out" ;; esac
case "$out" in *"no workspace resolves from here"*) ok "the helper's own stderr is shown" ;; *) fail "the helper's stderr was swallowed. Got: $out" ;; esac
case "$out" in *"NOT INSTALLED"*) fail "still reported NOT INSTALLED — it guessed a directory. Got: $out" ;; *) ok "no NOT INSTALLED from a guessed directory" ;; esac

# --- the other half of the same guard: helper succeeds, says nothing ----------
# `|| [ -z "$SKILLS_DST" ]` is what covers this, and nothing above exercises it:
# with only the exit-1 case, removing that clause leaves the suite fully green
# while the empty value falls through to $HOME/.claude/skills and reports the
# skill NOT INSTALLED -- the exact defect this file exists to prevent.
helper_empty
set +e; out="$(run env)"; rc=$?; set -e
[ "$rc" -eq 3 ] && ok "a helper that exits 0 with no output exits 3" || fail "an empty-output helper exited $rc, expected 3. Got: $out"
case "$out" in *"$REPO/scripts/sutando-config.sh"*"failed"*) ok "the empty-output failure names the helper" ;; *) fail "the failure does not name the helper. Got: $out" ;; esac
case "$out" in *"NOT INSTALLED"*) fail "still reported NOT INSTALLED — it guessed a directory. Got: $out" ;; *) ok "no NOT INSTALLED from an empty resolution" ;; esac

# --- SUTANDO_REPO_DIR pointing at a checkout with no helper --------------------
helper_ok
set +e; out="$(run env SUTANDO_REPO_DIR="$TMP/not-a-repo")"; rc=$?; set -e
[ "$rc" -eq 3 ] && ok "SUTANDO_REPO_DIR without a helper exits 3" || fail "exited $rc, expected 3. Got: $out"
case "$out" in *"SUTANDO_REPO_DIR=$TMP/not-a-repo has no scripts/sutando-config.sh"*) ok "the refusal names the env var and the path" ;; *) fail "refusal did not name SUTANDO_REPO_DIR. Got: $out" ;; esac

# --- positive control: a working helper refreshes and says where it looked ----
set +e; out="$(run env)"; rc=$?; set -e
[ "$rc" -eq 0 ] && ok "a working helper exits 0" || fail "control exited $rc. Got: $out"
case "$out" in *"skills dir $DST"*) ok "the resolved directory is printed" ;; *) fail "resolved directory not printed. Got: $out" ;; esac
case "$out" in *"refreshed demo"*) ok "the symlinked skill is refreshed from the resolved dir" ;; *) fail "demo not refreshed. Got: $out" ;; esac

# --- pre-revamp control: no helper anywhere → ~/.claude/skills, said aloud -----
rm -f "$REPO/scripts/sutando-config.sh"
set +e; out="$(run env)"; rc=$?; set -e
[ "$rc" -eq 0 ] && ok "no helper at all still exits 0" || fail "pre-revamp fallback exited $rc. Got: $out"
case "$out" in *"skills dir $TMP/home/.claude/skills"*) ok "the pre-revamp fallback is printed, not silent" ;; *) fail "fallback dir not printed. Got: $out" ;; esac

if [ "$fails" -ne 0 ]; then echo "refresh-skill-refuses-a-failing-helper: $fails failure(s)"; exit 1; fi
echo "refresh-skill-refuses-a-failing-helper: all ok"
