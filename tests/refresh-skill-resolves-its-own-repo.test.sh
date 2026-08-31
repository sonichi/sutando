#!/usr/bin/env bash
# The fake repo's helper emits a UNIQUE temp dir, so this still discriminates on
# a host that happens to have ~/Desktop/sutando.
set -eu

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
fails=0
ok()   { echo "  ok  $1"; }
fail() { echo "FAIL: $1"; fails=$((fails+1)); }

REAL_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)/skills/refresh-skill.sh"
[ -f "$REAL_SCRIPT" ] || { echo "cannot find refresh-skill.sh"; exit 1; }

# --- a fake repo at a path that is NOT ~/Desktop/sutando ----------------------
REPO="$TMP/some/other/place/sutando"
mkdir -p "$REPO/scripts" "$REPO/skills"
cp "$REAL_SCRIPT" "$REPO/skills/refresh-skill.sh"

DST="$TMP/unique-skills-dst"        # only reachable via the fake repo's helper
mkdir -p "$DST"
cat > "$REPO/scripts/sutando-config.sh" <<EOF
#!/usr/bin/env bash
[ "\${1:-}" = "claude-home-path" ] && { echo "$DST"; exit 0; }
exit 1
EOF
# deliberately NOT chmod +x: it is invoked via \`bash\`, so a missing exec bit
# must not silently disable resolution.

SRC="$TMP/skill-src/demo"
mkdir -p "$SRC"; echo "# demo" > "$SRC/SKILL.md"
ln -s "$SRC" "$DST/demo"

out="$(cd "$TMP" && REFRESH_SKILL_SETTLE_S=0 bash "$REPO/skills/refresh-skill.sh" demo 2>&1 || true)"

case "$out" in
  *"refreshed demo"*) ok "resolves the helper from its own repo, not a hardcoded path" ;;
  *"not a symlink"*|*"NOT INSTALLED"*)
      fail "fell back to the wrong skills dir — a symlinked skill read as absent. Got: $out" ;;
  *)  fail "unexpected output: $out" ;;
esac

[ -L "$DST/demo" ] || fail "the symlink must survive a refresh"
[ "$(readlink "$DST/demo")" = "$SRC" ] || fail "symlink must still point at its source"
ok "symlink restored and still points at its source"

# --- an ABSENT skill must not be reported as a protective refusal -------------
out2="$(cd "$TMP" && REFRESH_SKILL_SETTLE_S=0 bash "$REPO/skills/refresh-skill.sh" nosuchskill 2>&1 || true)"
case "$out2" in
  *"NOT INSTALLED"*) ok "an absent skill says NOT INSTALLED, not 'won't clobber a local install'" ;;
  *"not a symlink"*) fail "absent skill misreported as a copy-install refusal: $out2" ;;
  *) fail "unexpected output for absent skill: $out2" ;;
esac

# --- a REAL directory is still protected -------------------------------------
mkdir -p "$DST/copyinstall"
out3="$(cd "$TMP" && REFRESH_SKILL_SETTLE_S=0 bash "$REPO/skills/refresh-skill.sh" copyinstall 2>&1 || true)"
case "$out3" in
  *"not a symlink"*) ok "a real directory is still refused (copy install protected)" ;;
  *) fail "a real dir must still be refused, got: $out3" ;;
esac
[ -d "$DST/copyinstall" ] || fail "copy install must not be removed"

if [ "$fails" -eq 0 ]; then echo "OK — refresh-skill resolution tests passed"; else
  echo "$fails failure(s)"; exit 1; fi
