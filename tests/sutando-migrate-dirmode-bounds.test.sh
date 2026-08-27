#!/usr/bin/env bash
# The dir-mode walk is held only by basename agreement, so with matching
# ancestors it climbs past the dest root and chmods paths outside the rollback.
set -u
cd "$(dirname "$0")/.."
fails=0
check() { if [ "$2" = "$3" ]; then echo "  ok  $1"; else echo "FAIL  $1 — got '$2', want '$3'"; fails=$((fails+1)); fi; }

MIGRATE="$PWD/scripts/sutando-migrate.sh"
tmp="$(mktemp -d -t migrate-dirmode.XXXXXX)"
trap 'rm -rf "$tmp"' EXIT

# Ancestors agree ABOVE both roots ('shared', then 'workspace'), which is what
# lets the walk leave the destination — the real legacy/canonical pair does too.
SRC="$tmp/left/shared/workspace"
DEST="$tmp/right/shared/workspace"
OUTSIDE="$tmp/right/shared"          # parent of DEST_REAL: outside target AND backup
mkdir -p "$SRC/hosts/H" "$DEST/hosts/H"
printf 'secret\n' > "$SRC/hosts/H/PERSONAL_CLAUDE.md"
# The SOURCE-side outside ancestor must be NARROWER, or the intersection is a
# no-op and the test passes with or without the bound (measured: it did).
chmod 0700 "$SRC" "$SRC/hosts" "$SRC/hosts/H" "$tmp/left/shared"
chmod 0755 "$OUTSIDE"

before_outside="$(stat -c %a "$OUTSIDE" 2>/dev/null || stat -f %Lp "$OUTSIDE")"
SUTANDO_MIGRATE_SRC_C="$SRC" SUTANDO_MIGRATE_DEST="$DEST" \
    bash "$MIGRATE" commit --source C --no-confirm >"$tmp/out.txt" 2>&1 || true
after_outside="$(stat -c %a "$OUTSIDE" 2>/dev/null || stat -f %Lp "$OUTSIDE")"

check "a path ABOVE the dest root is never chmodded" "$after_outside" "$before_outside"

# Positive control: without it this passes just as well when mirroring is dead.
inner="$DEST/hosts/H"
inner_mode="$(stat -c %a "$inner" 2>/dev/null || stat -f %Lp "$inner")"
check "control: a dir INSIDE the dest root still takes the intersection" "$inner_mode" "700"
check "control: the file actually migrated (else nothing was exercised)" \
      "$([ -f "$DEST/hosts/H/PERSONAL_CLAUDE.md" ] && echo yes || echo no)" "yes"

[ "$fails" -eq 0 ] && echo "ALL PASS" || echo "$fails FAILED"
exit "$fails"
