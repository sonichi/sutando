#!/usr/bin/env bash
# The in-place replacer must take the SAME <file>.lock an appender takes: a lock on
# the destination's own descriptor excludes nobody, so an append is erased mid-rewrite.
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
fails=0
check() { if eval "$2"; then echo "  ok: $1"; else echo "  FAIL: $1"; fails=$((fails+1)); fi; }

SB="$(mktemp -d)"
trap 'rm -rf "$SB"' EXIT
export SCRIPT_PARENT="$SB/parent"
mkdir -p "$SCRIPT_PARENT/scripts"
printf '#!/bin/sh\ncase "$1" in python-bin) echo "%s" ;; *) echo "%s" ;; esac\n' \
    "$(command -v python3)" "$SB/cfg" > "$SCRIPT_PARENT/scripts/sutando-config.sh"
chmod +x "$SCRIPT_PARENT/scripts/sutando-config.sh"

# Load the SHIPPED helper. Do NOT de-indent: the function is indented but its
# heredoc body is not, so stripping the margin breaks the embedded Python.
eval "$(sed -n '/^    _replace_in_place() {/,/^    }$/p' "$REPO/scripts/sync-workspace.sh")"
type _replace_in_place >/dev/null 2>&1 || { echo "  FAIL: could not load _replace_in_place"; exit 1; }

DST="$SB/build_log.md"
SRC="$SB/new.txt"
printf 'original\n' > "$DST"
printf 'replacement\n' > "$SRC"
CUR="$(shasum -a 256 "$DST" | cut -d' ' -f1)"

# Hold <dst>.lock for `$1` seconds, signalling readiness through a file.
hold_lock() {
  python3 - "$DST.lock" "$1" "$SB/held" <<'PYEOF' &
import fcntl, sys, time
lock, secs, flag = sys.argv[1], float(sys.argv[2]), sys.argv[3]
f = open(lock, "a+")
fcntl.flock(f.fileno(), fcntl.LOCK_EX)
open(flag, "w").write("held")
time.sleep(secs)
PYEOF
  HOLDER=$!
  for _ in $(seq 1 50); do [ -f "$SB/held" ] && break; sleep 0.1; done
}

# 1. The replacer must BLOCK while the shared lock is held.
rm -f "$SB/held"; hold_lock 3
_replace_in_place --replace "$SRC" "$DST" "$CUR" & REPL=$!
sleep 1
if kill -0 "$REPL" 2>/dev/null; then _blocked=yes; else _blocked=no; fi
check "replacer BLOCKS while <dst>.lock is held (blocked=$_blocked)" '[ "$_blocked" = yes ]'
wait "$REPL" 2>/dev/null; wait "$HOLDER" 2>/dev/null

# 2. Control: with no lock held it completes, so case 1 measured the lock and
#    not a broken invocation.
printf 'original\n' > "$DST"
_replace_in_place --replace "$SRC" "$DST" "$CUR"; _rc=$?
check "control: unlocked replace completes (rc=$_rc)" '[ "$_rc" -eq 0 ]'
check "control: destination actually replaced" '[ "$(cat "$DST")" = replacement ]'

# 3. An append that lands while the appender holds the lock is NOT erased: the
#    replacer waits, then sees changed bytes and refuses.
printf 'original\n' > "$DST"
CUR2="$(shasum -a 256 "$DST" | cut -d' ' -f1)"
python3 - "$DST" <<'PYEOF'
import fcntl, sys
dst = sys.argv[1]
with open(dst + ".lock", "a+") as lk:
    fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
    with open(dst, "a") as f:
        f.write("appended by a concurrent writer\n")
PYEOF
_replace_in_place --replace "$SRC" "$DST" "$CUR2"; _rc3=$?
check "replace REFUSES after a locked append (rc=$_rc3, want 2)" '[ "$_rc3" -eq 2 ]'
check "the appended line survived" 'grep -q "appended by a concurrent writer" "$DST"'

# 4. DISCRIMINATOR — the pre-fix implementation, inlined, must NOT block. Without
#    this the suite would pass against the very code it exists to reject.
_replace_pre_fix() {
  python3 - "$@" <<'PYEOF' 2>/dev/null
import fcntl, hashlib, os, sys
mode, src, dst = sys.argv[1:4]
expected = sys.argv[4] if len(sys.argv) > 4 else None
with open(src, "rb") as f:
    data = f.read()
fd = os.open(dst, os.O_RDWR | os.O_CREAT, 0o644)
try:
    fcntl.flock(fd, fcntl.LOCK_EX)
    with open(fd, "rb", closefd=False) as f:
        cur = f.read()
    if hashlib.sha256(cur).hexdigest() != expected and not (expected == "" and not cur):
        sys.exit(2)
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]
    os.fsync(fd)
finally:
    os.close(fd)
PYEOF
}
printf 'original\n' > "$DST"
CUR4="$(shasum -a 256 "$DST" | cut -d' ' -f1)"
rm -f "$SB/held"; hold_lock 3
_replace_pre_fix --replace "$SRC" "$DST" "$CUR4" & PRE=$!
sleep 1
if kill -0 "$PRE" 2>/dev/null; then _pre_blocked=yes; else _pre_blocked=no; fi
check "discriminator: PRE-FIX replacer does NOT block (blocked=$_pre_blocked)" '[ "$_pre_blocked" = no ]'
wait "$PRE" 2>/dev/null; wait "$HOLDER" 2>/dev/null

# 5. The sidecar must be denied from the carrier set, or the fix vaults a lock file.
_excl="$(sed -n '/^_compose_exclude_content()/,/^}/p' "$REPO/scripts/sync-workspace.sh")"
check "carrier set denies *.lock" 'printf "%s" "$_excl" | grep -q "\*\.lock"'

echo ""
if [ "$fails" -eq 0 ]; then echo "ALL PASS — sync replace shares the writer lock (7 checks)"; else echo "$fails FAILURE(S)"; fi
exit $([ "$fails" -eq 0 ] && echo 0 || echo 1)
