#!/usr/bin/env bash
# Protected state is never readable before its mode policy holds: temp born at
# the intersection, parents narrowed before any leaf, verify re-checks the chain.
set -u
cd "$(dirname "$0")/.."
fails=0
check() { if [ "$2" = "$3" ]; then echo "  ok  $1"; else echo "FAIL  $1 — got '$2', want '$3'"; fails=$((fails+1)); fi; }
mode_of() { stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1" 2>/dev/null; }
MIGRATE="$PWD/scripts/sutando-migrate.sh"
REL="state/slack-allowed-recipients.json"   # the shipped union-json-array rule
tmp="$(mktemp -d -t migrate-pub.XXXXXX)"
trap 'rm -rf "$tmp"' EXIT
umask 022

# --- control 1: the union temp is never observed non-empty and wider than the intersection ---

# The production python block runs unchanged under a shim that records the
# temp's (mode, size) at every later audit event; fchmod itself emits none.
REALPY="$(command -v python3)"
mkdir -p "$tmp/shim"
cat > "$tmp/shim/audit.py" <<'PYS'
import os, sys
log_fd = os.open(os.environ["MIG_AUDIT_LOG"], os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
seen = []
def hook(ev, args):
    if ev not in ("open", "os.chmod", "os.utime", "os.rename", "os.replace"):
        return
    p = args[0] if args else None
    if isinstance(p, (str, bytes, os.PathLike)):
        p = os.fsdecode(p)
        if ".tmp." in p and p not in seen:
            seen.append(p)
    for t in seen:
        try:
            st = os.stat(t)
        except OSError:
            continue
        os.write(log_fd, f"{ev}\t{t}\t{st.st_mode & 0o7777:o}\t{st.st_size}\n".encode())
if sys.argv[1] != "-":
    os.execv(sys.argv[0].replace("audit.py", "") and os.environ["MIG_REAL_PY"], [os.environ["MIG_REAL_PY"]] + sys.argv[1:])
sys.addaudithook(hook)
code = sys.stdin.read()
sys.argv = sys.argv[1:]
exec(compile(code, "<stdin>", "exec"), {"__name__": "__main__"})
PYS
printf '#!/bin/bash\nexec "$MIG_REAL_PY" "%s" "$@"\n' "$tmp/shim/audit.py" > "$tmp/shim/python3"
chmod 0755 "$tmp/shim/python3"

U="$tmp/u1"; mkdir -p "$U/A/state" "$U/dest/state"
# Large enough that buffered writes reach the disk before the file is closed.
python3 - "$U/A/$REL" "$U/dest/$REL" <<'PYS'
import json, sys
for path, tag in ((sys.argv[1], "a"), (sys.argv[2], "b")):
    with open(path, "w") as fh:
        json.dump({"allow": [f"{tag}{i}@example.org" for i in range(4000)]}, fh)
PYS
chmod 0600 "$U/A/$REL" "$U/dest/$REL"; chmod 0755 "$U/A/state" "$U/dest/state"
touch -t 202601020000 "$U/A/$REL"; touch -t 202601010000 "$U/dest/$REL"
: > "$tmp/audit.log"
_rc=0; MIG_AUDIT_LOG="$tmp/audit.log" MIG_REAL_PY="$REALPY" SUTANDO_PY="$tmp/shim/python3" \
  SUTANDO_MIGRATE_SRC_A="$U/A" SUTANDO_MIGRATE_DEST="$U/dest" \
  bash "$MIGRATE" commit --source A --no-confirm >"$tmp/u1.out" 2>&1 || _rc=$?
check "union commit succeeds under the audit shim" "$_rc" "0"
check "the union merged (control: the production block ran)" \
  "$(grep -c 'a1@example.org' "$U/dest/$REL" | tr -d ' ')" "1"
check "the shim observed the temp's life (control: the hook can see)" \
  "$([ "$(wc -l < "$tmp/audit.log" | tr -d ' ')" -ge 1 ] && echo yes || echo no)" "yes"
first="$(head -1 "$tmp/audit.log")"
check "first observation of the temp: empty (no byte precedes the mode)" "$(printf '%s' "$first" | cut -f4)" "0"
check "first observation of the temp: born at the 0600 intersection" "$(printf '%s' "$first" | cut -f3)" "600"
wide="$(awk -F'\t' '$4 > 0 && $3 != "600" { n++ } END { print n+0 }' "$tmp/audit.log")"
check "the temp is never non-empty while wider than the intersection" "$wide" "0"
check "the published leaf keeps the intersection" "$(mode_of "$U/dest/$REL")" "600"

# --- control 2: a failed parent enforcement leaves the pre-union destination untouched ---
union_fixture() {   # $1 = root; 0700 source parent, 0755 destination parent, divergent leaves
    mkdir -p "$1/A/state" "$1/dest/state"
    printf '{"allow": ["a@example.org"]}\n' > "$1/A/$REL";    chmod 0644 "$1/A/$REL"
    printf '{"allow": ["b@example.org"]}\n' > "$1/dest/$REL"; chmod 0644 "$1/dest/$REL"
    chmod 0700 "$1/A/state"; chmod 0755 "$1/dest/state"
    touch -t 202601020000 "$1/A/$REL"; touch -t 202601010000 "$1/dest/$REL"
}
# Positive control first, on its own fixture: the same inputs commit and narrow.
U="$tmp/u2b"; union_fixture "$U"
_rc=0; SUTANDO_MIGRATE_SRC_A="$U/A" SUTANDO_MIGRATE_DEST="$U/dest" \
  bash "$MIGRATE" commit --source A --no-confirm >"$tmp/u2b.out" 2>&1 || _rc=$?
check "control: without the shim the union commits" "$_rc" "0"
check "control: and the parent takes the 0700 intersection" "$(mode_of "$U/dest/state")" "700"
check "control: and the leaf merged" "$(grep -c 'a@example.org' "$U/dest/$REL" | tr -d ' ')" "1"
# A chmod shim fails ONLY the destination parent, exactly the review's probe.
# The script chmods the RESOLVED path, so the shim must compare against it.
U="$tmp/u2"; union_fixture "$U"
dest_state_real="$(cd "$U/dest/state" && pwd -P)"
mkdir -p "$tmp/chmod-shim"
cat > "$tmp/chmod-shim/chmod" <<EOS
#!/bin/bash
for a in "\$@"; do [ "\$a" = "$dest_state_real" ] && exit 97; done
exec /bin/chmod "\$@"
EOS
chmod 0755 "$tmp/chmod-shim/chmod"
before_sha="$(shasum -a 256 "$U/dest/$REL" | cut -d' ' -f1)"
_rc=0; PATH="$tmp/chmod-shim:$PATH" SUTANDO_MIGRATE_SRC_A="$U/A" SUTANDO_MIGRATE_DEST="$U/dest" \
  bash "$MIGRATE" commit --source A --no-confirm >"$tmp/u2.out" 2>&1 || _rc=$?
check "failed parent enforcement: commit fails" "$([ "$_rc" -ne 0 ] && echo failed || echo ok)" "failed"
check "failed parent enforcement: the shim fired (control: the parent stayed 0755)" "$(mode_of "$U/dest/state")" "755"
check "failed parent enforcement: the pre-union leaf is byte-identical" \
  "$(shasum -a 256 "$U/dest/$REL" | cut -d' ' -f1)" "$before_sha"
check "failed parent enforcement: no union temp is left behind" \
  "$(ls "$U/dest/state"/*.tmp.* 2>/dev/null | wc -l | tr -d ' ')" "0"
check "failed parent enforcement: no union manifest certifies the leaf" \
  "$(ls "$U/dest/state"/.migration-union-scalars-*.json 2>/dev/null | wc -l | tr -d ' ')" "0"

# --- control 3: an identity drop with NO copy still narrows the governed parents ---
S="$tmp/s3"; mkdir -p "$S/C/hosts/Test-Host" "$S/dest/hosts/Test-Host"
printf 'private per-host rules\n' > "$S/C/hosts/Test-Host/PERSONAL_CLAUDE.md"
cp "$S/C/hosts/Test-Host/PERSONAL_CLAUDE.md" "$S/dest/hosts/Test-Host/PERSONAL_CLAUDE.md"
chmod 0644 "$S/C/hosts/Test-Host/PERSONAL_CLAUDE.md" "$S/dest/hosts/Test-Host/PERSONAL_CLAUDE.md"
chmod 0700 "$S/C/hosts" "$S/C/hosts/Test-Host"; chmod 0755 "$S/dest/hosts" "$S/dest/hosts/Test-Host"
touch -t 202601010000 "$S/C/hosts/Test-Host/PERSONAL_CLAUDE.md"
touch -t 202601020000 "$S/dest/hosts/Test-Host/PERSONAL_CLAUDE.md"   # dest newer: no copy
ino_before="$(stat -c '%i %Y' "$S/dest/hosts/Test-Host/PERSONAL_CLAUDE.md" 2>/dev/null || stat -f '%i %m' "$S/dest/hosts/Test-Host/PERSONAL_CLAUDE.md")"
_rc=0; SUTANDO_MIGRATE_SRC_C="$S/C" SUTANDO_MIGRATE_DEST="$S/dest" \
  bash "$MIGRATE" commit --source C --no-confirm >"$tmp/s3.out" 2>&1 || _rc=$?
check "structural identity drop (dest newer): commit succeeds" "$_rc" "0"
check "structural identity drop: reported as identical-drop" \
  "$(grep -c 'identical-drop' "$tmp/s3.out" | tr -d ' ')" "1"
check "structural identity drop: the leaf was NOT copied (inode + mtime unchanged)" \
  "$(stat -c '%i %Y' "$S/dest/hosts/Test-Host/PERSONAL_CLAUDE.md" 2>/dev/null || stat -f '%i %m' "$S/dest/hosts/Test-Host/PERSONAL_CLAUDE.md")" "$ino_before"
check "structural identity drop: hosts/Test-Host takes the 0700 intersection" "$(mode_of "$S/dest/hosts/Test-Host")" "700"
check "structural identity drop: hosts takes the 0700 intersection" "$(mode_of "$S/dest/hosts")" "700"
# The union class has the same no-copy branch.
U="$tmp/u3"; mkdir -p "$U/A/state" "$U/dest/state"
printf '{"allow": ["a@example.org"]}\n' > "$U/A/$REL"; cp "$U/A/$REL" "$U/dest/$REL"
chmod 0644 "$U/A/$REL" "$U/dest/$REL"; chmod 0700 "$U/A/state"; chmod 0755 "$U/dest/state"
touch -t 202601010000 "$U/A/$REL"; touch -t 202601020000 "$U/dest/$REL"
_rc=0; SUTANDO_MIGRATE_SRC_A="$U/A" SUTANDO_MIGRATE_DEST="$U/dest" \
  bash "$MIGRATE" commit --source A --no-confirm >"$tmp/u3.out" 2>&1 || _rc=$?
check "union identity drop (dest newer): commit succeeds" "$_rc" "0"
check "union identity drop: state takes the 0700 intersection" "$(mode_of "$U/dest/state")" "700"

# --- control 4: verify checks the governed parent chain, not only the leaf ---
_v=0; SUTANDO_MIGRATE_SRC_C="$S/C" SUTANDO_MIGRATE_DEST="$S/dest" \
  bash "$MIGRATE" verify --source C >"$tmp/s4.out" 2>&1 || _v=$?
check "verify passes on the narrowed chain" "$_v" "0"
chmod 0755 "$S/dest/hosts/Test-Host"
_w=0; SUTANDO_MIGRATE_SRC_C="$S/C" SUTANDO_MIGRATE_DEST="$S/dest" \
  bash "$MIGRATE" verify --source C >"$tmp/s4w.out" 2>&1 || _w=$?
check "verify FAILS once a governed parent is widened after commit" \
  "$([ "$_w" -ne 0 ] && echo failed || echo certified)" "failed"
check "verify names the widened parent" "$(grep -c 'parent is wider' "$tmp/s4w.out" | tr -d ' ')" "1"
chmod 0700 "$S/dest/hosts/Test-Host"
_v2=0; SUTANDO_MIGRATE_SRC_C="$S/C" SUTANDO_MIGRATE_DEST="$S/dest" \
  bash "$MIGRATE" verify --source C >"$tmp/s4r.out" 2>&1 || _v2=$?
check "control: verify passes again once the parent is restored" "$_v2" "0"

if [ "$fails" -gt 0 ]; then echo "$fails FAILURE(S)"; exit 1; fi
echo "ALL PASS"
