#!/usr/bin/env bash
# The build_log snapshot must not clobber a per-host copy it can DETECT — ownership
# is provenance, never mtime. An already-open descriptor is not detectable.
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
fails=0
check() { if eval "$2"; then echo "  ok: $1"; else echo "  FAIL: $1"; fails=$((fails+1)); fi; }

SB="$(mktemp -d)"
trap 'rm -rf "$SB"' EXIT
export WORKSPACE_DIR="$SB/ws"
mkdir -p "$WORKSPACE_DIR/hosts/testhost"
# Stub SCRIPT_PARENT so the function's config-dir resolution succeeds.
export SCRIPT_PARENT="$SB/parent"
mkdir -p "$SCRIPT_PARENT/scripts" "$SB/cfg"
# The stub must answer `python-bin` faithfully: the durable-publish path now
# resolves its interpreter through it, and a wrong answer fails the fsync closed.
printf '#!/bin/sh\ncase "$1" in python-bin) echo "%s" ;; *) echo "%s" ;; esac\n' \
    "$(command -v python3)" "$SB/cfg" > "$SCRIPT_PARENT/scripts/sutando-config.sh"
chmod +x "$SCRIPT_PARENT/scripts/sutando-config.sh"

# Load ONLY the function under test, plus a _host stub.
_host() { echo testhost; }
eval "$(sed -n '/^_snapshot_per_host_config() {/,/^}$/p' "$REPO/scripts/sync-workspace.sh")"

# Load the REAL logging trio: production log() writes only to $LOG and emits
# nothing, so a substituted echo makes a stdout grep pass and prove nothing.
LOG="$SB/sync-workspace.log"; : > "$LOG"
eval "$(sed -n '/^log() {/,/^}$/p'           "$REPO/scripts/sync-workspace.sh")"
eval "$(sed -n '/^warn_operator() {/,/^}$/p' "$REPO/scripts/sync-workspace.sh")"
eval "$(sed -n '/^color_warn() {/,/^}$/p'    "$REPO/scripts/sync-workspace.sh")"

# Control: the real log() must be SILENT on stdout+stderr, or every "loud"
# assertion below is satisfied by the logger rather than by the refusal.
check "the real log() is silent on stdout/stderr (control)" \
      '[ -z "$(log "control probe" 2>&1)" ]'
check "...and is durable in \$LOG (so the control is not silence-by-breakage)" \
      'grep -q "control probe" "$LOG"'

echo "1. reviewer's control: per-host written independently, stale root TOUCHED LATER"
echo "stale relic" > "$WORKSPACE_DIR/build_log.md"
echo "per-host live entry" > "$WORKSPACE_DIR/hosts/testhost/build_log.md"
sleep 1
touch "$WORKSPACE_DIR/build_log.md"   # root now strictly NEWER than the live per-host copy
_snapshot_per_host_config
check "independent per-host writer survives a later-touched root" \
      '[ "$(cat "$WORKSPACE_DIR/hosts/testhost/build_log.md")" = "per-host live entry" ]'
check "the refusal reaches the OPERATOR on stderr, not just the log" \
      '_snapshot_per_host_config 2>&1 >/dev/null | grep -q "independent writer"'
check "and it is still recorded durably in \$LOG" \
      'grep -q "independent writer" "$LOG"'

echo "2. reverse ordering: per-host newer than root — still refused, still loud"
touch "$WORKSPACE_DIR/hosts/testhost/build_log.md"
_snapshot_per_host_config
check "mtime ordering is irrelevant to the refusal" \
      '[ "$(cat "$WORKSPACE_DIR/hosts/testhost/build_log.md")" = "per-host live entry" ]'

echo "3. seed: absent per-host copy is created and its provenance recorded"
rm "$WORKSPACE_DIR/hosts/testhost/build_log.md" "$WORKSPACE_DIR/hosts/testhost/.build_log.snapshot-sha" 2>/dev/null
_snapshot_per_host_config
check "first snapshot seeds the per-host copy" \
      '[ -f "$WORKSPACE_DIR/hosts/testhost/build_log.md" ]'
check "provenance sha recorded beside it" \
      '[ -s "$WORKSPACE_DIR/hosts/testhost/.build_log.snapshot-sha" ]'

echo "4. root-live host: untouched snapshot refreshes when root changes"
echo "fresh root entries" > "$WORKSPACE_DIR/build_log.md"
_snapshot_per_host_config
check "pure snapshot (matching recorded sha) is refreshed from root" \
      '[ "$(cat "$WORKSPACE_DIR/hosts/testhost/build_log.md")" = "fresh root entries" ]'

echo "5. independent edit AFTER seeding is never overwritten"
echo "per-host went live" > "$WORKSPACE_DIR/hosts/testhost/build_log.md"
echo "root moved on too" > "$WORKSPACE_DIR/build_log.md"
_snapshot_per_host_config
check "post-seed independent writer wins regardless of root changes" \
      '[ "$(cat "$WORKSPACE_DIR/hosts/testhost/build_log.md")" = "per-host went live" ]'

echo "5b. upgrade bootstrap: pre-provenance byte-identical snapshot is adopted"
rm -f "$WORKSPACE_DIR/hosts/testhost/.build_log.snapshot-sha"
echo "same bytes both sides" > "$WORKSPACE_DIR/build_log.md"
cp "$WORKSPACE_DIR/build_log.md" "$WORKSPACE_DIR/hosts/testhost/build_log.md"
_snapshot_per_host_config
check "provenance recorded for the inherited equal snapshot" \
      '[ -s "$WORKSPACE_DIR/hosts/testhost/.build_log.snapshot-sha" ]'
echo "root moved after upgrade" > "$WORKSPACE_DIR/build_log.md"
_snapshot_per_host_config
check "root-live refresh works after the upgrade bootstrap" \
      '[ "$(cat "$WORKSPACE_DIR/hosts/testhost/build_log.md")" = "root moved after upgrade" ]'

echo "5c. pre-provenance DIVERGED copy stays refused (no adoption on difference)"
rm -f "$WORKSPACE_DIR/hosts/testhost/.build_log.snapshot-sha"
echo "independent content" > "$WORKSPACE_DIR/hosts/testhost/build_log.md"
_snapshot_per_host_config
check "diverged unrecorded copy is preserved, not adopted or overwritten" \
      '[ "$(cat "$WORKSPACE_DIR/hosts/testhost/build_log.md")" = "independent content" ]'
check "no-provenance refusal names the ACTUAL condition, not a second writer" \
      '_snapshot_per_host_config 2>&1 >/dev/null | grep -q "provenance record"'
check "...and does NOT tell the operator to archive a copy (the message that did)" \
      '! _snapshot_per_host_config 2>&1 >/dev/null | grep -q "pick ONE writer"'

# [ -z "$_rec" ] is ALSO true for an EMPTY or unreadable sig file, so the
# branch must not claim the file is merely missing.
: > "$WORKSPACE_DIR/hosts/testhost/.build_log.snapshot-sha"     # exists, but empty
check "an EMPTY provenance file takes the same no-usable-record branch" \
      '_snapshot_per_host_config 2>&1 >/dev/null | grep -q "NO USABLE provenance record"'
check "...and an empty record is still not called an independent writer" \
      '! _snapshot_per_host_config 2>&1 >/dev/null | grep -q "pick ONE writer"'

echo "5d. present-but-MALFORMED signature is not usable provenance either"
# A 63-char partial (torn write, stray copy, manual edit) satisfies a bare
# equality test; any non-sha256 content must take the guarded branch.
printf '%063d' 0 > "$WORKSPACE_DIR/hosts/testhost/.build_log.snapshot-sha"
check "a 63-char partial signature takes the no-usable-record branch" \
      '_snapshot_per_host_config 2>&1 >/dev/null | grep -q "NO USABLE provenance record"'
check "...and is NOT read as an independent writer" \
      '! _snapshot_per_host_config 2>&1 >/dev/null | grep -q "pick ONE writer"'
printf 'not hex at all\n' > "$WORKSPACE_DIR/hosts/testhost/.build_log.snapshot-sha"
check "arbitrary foreign content in the sig file is equally unusable" \
      '_snapshot_per_host_config 2>&1 >/dev/null | grep -q "NO USABLE provenance record"'
# Positive control for the validator: a VALID 64-hex stale sha must still be
# read as a genuine independent writer — validation must not widen the guard.
printf '%064d' 1 > "$WORKSPACE_DIR/hosts/testhost/.build_log.snapshot-sha"
check "a valid-shape stale sha is STILL an independent writer (control)" \
      '_snapshot_per_host_config 2>&1 >/dev/null | grep -q "pick ONE writer"'
# Pinned leniency: the writer itself emits a trailing newline, so newlines
# after the sha must stay usable. Trailing SPACES stay invalid.
printf '%064d\n\n\n' 1 > "$WORKSPACE_DIR/hosts/testhost/.build_log.snapshot-sha"
check "a valid sha with trailing blank lines is still USABLE provenance" \
      '_snapshot_per_host_config 2>&1 >/dev/null | grep -q "pick ONE writer"'
printf '%064d ' 1 > "$WORKSPACE_DIR/hosts/testhost/.build_log.snapshot-sha"
check "...but a trailing SPACE is malformed, not a writer" \
      '_snapshot_per_host_config 2>&1 >/dev/null | grep -q "NO USABLE provenance record"'

echo "5e. NUL damage must be judged on the on-disk bytes, not post-\$() text"
# Shell substitution strips NULs, so a NUL-damaged record collapses to 64
# clean hex chars; the validator must reject the bytes before that happens.
printf 'root stale\n' > "$WORKSPACE_DIR/build_log.md"
printf 'per-host live entry\n' > "$WORKSPACE_DIR/hosts/testhost/build_log.md"
_live_sha="$(shasum -a 256 "$WORKSPACE_DIR/hosts/testhost/build_log.md" | cut -d' ' -f1)"
printf '%s\0' "$_live_sha" > "$WORKSPACE_DIR/hosts/testhost/.build_log.snapshot-sha"
check "a trailing-NUL signature is NOT usable provenance" \
      '_snapshot_per_host_config 2>&1 >/dev/null | grep -q "NO USABLE provenance record"'
check "...and the live destination survives (no overwrite authority)" \
      'grep -q "per-host live entry" "$WORKSPACE_DIR/hosts/testhost/build_log.md"'
check "...and NUL damage is not an independent writer" \
      '! _snapshot_per_host_config 2>&1 >/dev/null | grep -q "pick ONE writer"'
printf '%.32s\0%.32s' "$_live_sha" "${_live_sha:32}" \
    > "$WORKSPACE_DIR/hosts/testhost/.build_log.snapshot-sha"
check "an embedded-NUL signature is equally unusable" \
      '_snapshot_per_host_config 2>&1 >/dev/null | grep -q "NO USABLE provenance record"'
check "...and the live destination still survives" \
      'grep -q "per-host live entry" "$WORKSPACE_DIR/hosts/testhost/build_log.md"'
rm -f "$WORKSPACE_DIR/hosts/testhost/.build_log.snapshot-sha"
echo "keep it that way" >> "$WORKSPACE_DIR/build_log.md"
echo "per-host went live" > "$WORKSPACE_DIR/hosts/testhost/build_log.md"

echo "6. absent root is a no-op"
rm "$WORKSPACE_DIR/build_log.md"
_snapshot_per_host_config
check "no root file leaves the per-host log untouched" \
      '[ "$(cat "$WORKSPACE_DIR/hosts/testhost/build_log.md")" = "per-host went live" ]'

echo "7. TOCTOU: a per-host write landing AFTER the ownership check is not clobbered"
# Seed a clean, owned snapshot (dst == root, sha recorded), then advance root.
printf 'root baseline\n' > "$WORKSPACE_DIR/build_log.md"
rm -f "$WORKSPACE_DIR/hosts/testhost/build_log.md" \
      "$WORKSPACE_DIR/hosts/testhost/.build_log.snapshot-sha"
_snapshot_per_host_config
printf 'root advanced\n' > "$WORKSPACE_DIR/build_log.md"
# Injects once, right after the ownership hash. This covers only the check->replace
# window; an fd opened BEFORE the snapshot and written after is out of its reach.
rm -f "$SB/.injected"
shasum() {
    local _o; _o="$(command shasum "$@")"
    if [ ! -e "$SB/.injected" ] && printf '%s' "$*" \
            | grep -q "hosts/testhost/build_log.md$"; then
        printf 'per-host live append\n' >> "$WORKSPACE_DIR/hosts/testhost/build_log.md"
        : > "$SB/.injected"
    fi
    printf '%s\n' "$_o"
}
_out7="$(_snapshot_per_host_config 2>&1)"
unset -f shasum
check "a per-host write in the check->replace window survives" \
      'grep -q "per-host live append" "$WORKSPACE_DIR/hosts/testhost/build_log.md"'
check "the racing replace is refused loudly" \
      'printf "%s" "$_out7" | grep -q "changed between check and replace"'

echo "8. self-heal: equal content with a stale provenance sha repairs itself"
printf 'converged\n' > "$WORKSPACE_DIR/build_log.md"
printf 'converged\n' > "$WORKSPACE_DIR/hosts/testhost/build_log.md"
printf 'deadbeefstale\n' > "$WORKSPACE_DIR/hosts/testhost/.build_log.snapshot-sha"
_snapshot_per_host_config
check "equal content adopts the true sha over a stale one" \
      '[ "$(cat "$WORKSPACE_DIR/hosts/testhost/.build_log.snapshot-sha")" = "$(shasum -a 256 "$WORKSPACE_DIR/hosts/testhost/build_log.md" | cut -d" " -f1)" ]'
printf 'root moved after self-heal\n' > "$WORKSPACE_DIR/build_log.md"
_snapshot_per_host_config
check "root-live refresh works once provenance self-healed" \
      '[ "$(cat "$WORKSPACE_DIR/hosts/testhost/build_log.md")" = "root moved after self-heal" ]'

echo "9. a FAILED atomic replace must clean up and say so (hosts/*/ is a carried vault path)"
printf 'root advanced\n' > "$WORKSPACE_DIR/build_log.md"
printf 'ours\n' > "$WORKSPACE_DIR/hosts/testhost/build_log.md"
shasum -a 256 "$WORKSPACE_DIR/hosts/testhost/build_log.md" | cut -d" " -f1 \
    > "$WORKSPACE_DIR/hosts/testhost/.build_log.snapshot-sha"
_sig_before="$(cat "$WORKSPACE_DIR/hosts/testhost/.build_log.snapshot-sha")"
mv() { return 1; }   # the swap fails (full disk, EXDEV, immutable dest, ...)
_out9="$(_snapshot_per_host_config 2>&1)"
unset -f mv
check "no orphan temp is left behind for the vault to carry" \
      '[ -z "$(ls "$WORKSPACE_DIR/hosts/testhost"/build_log.md.snap.* 2>/dev/null)" ]'
# Log-only BY DESIGN: the temp was removed and nothing lost, so it is a
# diagnostic, not an operator action. Asserted against $LOG for that reason.
check "the failed replace is recorded in \$LOG, not swallowed" \
      'grep -qi "atomic replace of" "$LOG"'
check "and it stays OUT of the operator stream (not every failure is an action)" \
      '[ -z "$(printf %s "$_out9")" ]'
check "provenance is not stamped for a swap that did not happen" \
      '[ "$(cat "$WORKSPACE_DIR/hosts/testhost/.build_log.snapshot-sha")" = "$_sig_before" ]'


# --- control 6: an INTERRUPTED stage must not survive into the next tick ---
# Two guards: the leftover is never STAGED (vault harm) and is SWEPT (disk harm).
printf 'root advanced again\n' > "$WORKSPACE_DIR/build_log.md"
printf 'ours2\n' > "$WORKSPACE_DIR/hosts/testhost/build_log.md"
_orphan="$WORKSPACE_DIR/hosts/testhost/build_log.md.snap.AB12cd"
printf 'a full build-log copy left by a killed process\n' > "$_orphan"
check "precondition: the orphan exists before the next tick" '[ -f "$_orphan" ]'

# A FRESH leftover must SURVIVE — a concurrent sync may be mid-write. Without
# this the sweep would be indistinguishable from an unconditional rm.
_snapshot_per_host_config
check "a FRESH reserved temp is left alone (a concurrent writer may own it)" \
      '[ -f "$_orphan" ]'

touch -t 202601010000 "$_orphan"
_snapshot_per_host_config
check "an AGED reserved temp is swept on the next tick" '[ ! -f "$_orphan" ]'

# --- control 7: the sweep must not reach past the temp this function OWNS ---
# A bare *.snap.?????? also matches unrelated user files; aged, so grace can't spare it.
_bystander="$WORKSPACE_DIR/hosts/testhost/report.snap.AB12cd"
printf 'a user file that merely resembles the reserved temp\n' > "$_bystander"
touch -t 202601010000 "$_bystander"
_snapshot_per_host_config
check "an aged UNRELATED file is NOT swept (sweep scoped to the owned temp)" \
      '[ -f "$_bystander" ]'

# The staging guard, independent of the sweep: a leftover inside the grace
# window must still be denied by the composed exclude set.
_excl="$(sed -n '/^_compose_exclude_content()/,/^}/p' "$REPO/scripts/sync-workspace.sh")"
check "the composed exclude set denies the reserved snapshot temp" \
      'printf %s "$_excl" | grep -q "build_log\.md\.snap\.??????"'
# The deny must be ANCHORED to the owned name. A bare *.snap.?????? would also
# hide unrelated user files from the vault, so the un-anchored form must be absent.
check "control: the exclude set does NOT carry an un-anchored *.snap pattern" \
      '! printf %s "$_excl" | grep -q "echo \"\*\.snap\.??????\""'
# Control: the same probe must be able to say NO, or it is matching noise.
check "control: the exclude set does NOT deny an unrelated invented pattern" \
      '! printf %s "$_excl" | grep -q "snap\.ZZZZZZZ"'

# --- control 12: the ignore rule must be BEHAVIOURALLY scoped to hosts/*/ ---
# A no-slash gitignore pattern matches at EVERY depth, so source-text greps pass.
_gi="$(mktemp -d)"; ( cd "$_gi" && git init -q . )
mkdir -p "$_gi/hosts/testhost" "$_gi/notes"
: > "$_gi/hosts/testhost/build_log.md.snap.AB12cd"
: > "$_gi/notes/build_log.md.snap.AB12cd"
sed -n '/^_compose_exclude_content()/,/^}/p' "$REPO/scripts/sync-workspace.sh" > "$_gi/compose.sh"
# shellcheck disable=SC1090
( . "$_gi/compose.sh"; _compose_exclude_content 2>/dev/null ) | grep 'build_log.md.snap' > "$_gi/.git/info/exclude" 2>/dev/null || true
check "the composed rule DOES ignore a per-host snapshot temp" \
      '( cd "$_gi" && git check-ignore -q hosts/testhost/build_log.md.snap.AB12cd )'
check "the composed rule does NOT ignore an unrelated notes/ file of the same name" \
      '( cd "$_gi" && ! git check-ignore -q notes/build_log.md.snap.AB12cd )'
rm -rf "$_gi"

echo "10. an interrupted publish is recoverable at EVERY boundary, and fails closed"
# The publish is intent -> mv -> promote. Each boundary is exercised by leaving
# the on-disk state that boundary produces, then running the production function
# and asserting what the NEXT tick does with it.
_SIG="$WORKSPACE_DIR/hosts/testhost/.build_log.snapshot-sha"
_INT="$WORKSPACE_DIR/hosts/testhost/.build_log.snapshot-sha.next"
_DST="$WORKSPACE_DIR/hosts/testhost/build_log.md"

# (a) crash AFTER the swap, BEFORE the promote: dest already holds the new bytes
# and the intent describes them -> the next tick completes the publish.
printf 'published content\n' > "$_DST"
printf '%s\n' "$(shasum -a 256 "$_DST" | cut -d' ' -f1)" > "$_INT"
rm -f "$_SIG"
_snapshot_per_host_config >/dev/null 2>&1
check "a) post-swap interrupt: the intent is promoted to the signature" \
      '[ ! -f "$_INT" ] && [ -f "$_SIG" ]'
_dst_sha="$(shasum -a 256 "$_DST" | cut -d' ' -f1)"
check "...and the promoted signature matches the destination on disk" \
      '[ "$(cat "$_SIG")" = "$_dst_sha" ]'

# (b) crash AFTER the intent, BEFORE the swap: the dest is still the OLD bytes,
# so the intent describes content that is not there and must grant nothing.
printf 'old destination\n' > "$_DST"
printf '%s\n' "$(printf 'content that never landed\n' | shasum -a 256 | cut -d' ' -f1)" > "$_INT"
rm -f "$_SIG"
_snapshot_per_host_config >/dev/null 2>&1
check "b) pre-swap interrupt: the stale intent is discarded" '[ ! -f "$_INT" ]'
_never_sha="$(printf 'content that never landed\n' | shasum -a 256 | cut -d' ' -f1)"
check "...and it never becomes the signature" \
      '[ ! -f "$_SIG" ] || [ "$(cat "$_SIG")" != "$_never_sha" ]'

# (c) a malformed intent is not provenance — same fail-closed rule as the sig.
printf 'old destination\n' > "$_DST"
printf 'not a sha\n' > "$_INT"
rm -f "$_SIG"
_snapshot_per_host_config >/dev/null 2>&1
check "c) a malformed intent is discarded, not promoted" \
      '[ ! -f "$_INT" ] && { [ ! -f "$_SIG" ] || [ "$(cat "$_SIG")" != "not a sha" ]; }'

# (d) idempotent: recovery runs on every tick, so running it twice must be a
# no-op rather than a second, different outcome.
printf 'published content\n' > "$_DST"
printf '%s\n' "$(shasum -a 256 "$_DST" | cut -d' ' -f1)" > "$_INT"
rm -f "$_SIG"
_snapshot_per_host_config >/dev/null 2>&1
_sig_once="$(cat "$_SIG" 2>/dev/null)"
_snapshot_per_host_config >/dev/null 2>&1
check "d) a second recovery pass changes nothing" \
      '[ "$(cat "$_SIG" 2>/dev/null)" = "$_sig_once" ] && [ ! -f "$_INT" ]'
rm -f "$_INT" "$_SIG"

echo "11. INJECTED write/fsync/rename failures: the chain fails closed, never half-published"
# keweichen on #3198: a best-effort chain lets the destination be renamed with no
# durable intent. Each injection asserts the destination and the signature are
# left CONSISTENT — never a new destination with stale/absent provenance.
_SIG="$WORKSPACE_DIR/hosts/testhost/.build_log.snapshot-sha"
_INT="$WORKSPACE_DIR/hosts/testhost/.build_log.snapshot-sha.next"
_DST="$WORKSPACE_DIR/hosts/testhost/build_log.md"

# Inject a durability failure without a seam in production code: the fsync
# helper runs `python3 - <path>`, so a shim that refuses exactly that path makes
# the fsync fail while every other python3 call still works.
# PATH no longer reaches the interpreter (section 12), so inject through
# SYNC_PY — the seam the resolver itself honours.
_fsync_shim() {
    _fs="$(mktemp -d)"
    cat > "$_fs/python3" <<SHIM
#!/bin/sh
case "\$2" in *"$1"*) exit 1 ;; esac
exec "$(command -v python3)" "\$@"
SHIM
    chmod +x "$_fs/python3"
    printf '%s' "$_fs"
}

_arm_owned_snapshot() {
    # a clean, owned snapshot: dest == root and the signature records it
    printf 'owned baseline\n' > "$WORKSPACE_DIR/build_log.md"
    printf 'owned baseline\n' > "$_DST"
    shasum -a 256 "$_DST" | cut -d' ' -f1 > "$_SIG"
    rm -f "$_INT"
    printf 'root moved on\n' > "$WORKSPACE_DIR/build_log.md"
}

# (a) the INTENT cannot be made durable -> the swap must not happen at all.
_arm_owned_snapshot
_before="$(cat "$_DST")"
_fs="$(_fsync_shim ".snapshot-sha.next")"
SYNC_PY="$_fs/python3" _snapshot_per_host_config >/dev/null 2>&1
rm -rf "$_fs"
check "a) intent not durable: the destination is NOT replaced" \
      '[ "$(cat "$_DST")" = "$_before" ]'
check "...and no intent is left behind" '[ ! -f "$_INT" ]'

# (b) the DESTINATION cannot be confirmed durable -> the signature must NOT be
# promoted; the intent survives so the next tick can verify and finish.
_arm_owned_snapshot
_sig_before="$(cat "$_SIG")"
_fs="$(_fsync_shim "/build_log.md")"
SYNC_PY="$_fs/python3" _snapshot_per_host_config > "$SB/b.out" 2>&1
rm -rf "$_fs"
check "b) destination not confirmed durable: the signature is NOT promoted" \
      '[ "$(cat "$_SIG")" = "$_sig_before" ]'
check "...and the intent is left for recovery" '[ -f "$_INT" ]'

# ...and that leftover state is exactly what recovery is for: the next tick
# verifies the destination's own bytes and completes the publish.
_snapshot_per_host_config >/dev/null 2>&1
_dst_now="$(shasum -a 256 "$_DST" | cut -d' ' -f1)"
check "...so the NEXT tick completes it from the intent" \
      '[ ! -f "$_INT" ] && [ "$(cat "$_SIG")" = "$_dst_now" ]'

# (c) a non-atomic in-place signature write must not exist as a fallback: with
# the promote rename failing, the signature must stay UNCHANGED rather than be
# rewritten in place.
_arm_owned_snapshot
_sig_before="$(cat "$_SIG")"
_shim="$(mktemp -d)"
cat > "$_shim/mv" <<'SHIM'
#!/bin/sh
for a in "$@"; do
  case "$a" in *.snapshot-sha) exit 1 ;; esac
done
exec /bin/mv "$@"
SHIM
chmod +x "$_shim/mv"
PATH="$_shim:$PATH" _snapshot_per_host_config >/dev/null 2>&1
check "c) promote rename fails: the signature is not rewritten in place" \
      '[ "$(cat "$_SIG")" = "$_sig_before" ]'
check "...and the intent survives for the next tick" '[ -f "$_INT" ]'
rm -rf "$_shim"
rm -f "$_INT"; rm -f "$_SIG"

# ---- 12. the durable-publish path uses the REPO'S verified interpreter -------
# A bare `python3` can be the Xcode-CLT stub: it "exists", fails on exec, and
# raises an install dialog every interval while the snapshot stays stale.
echo "== 12. verified interpreter, not bare python3 =="

check "no bare python3 survives in the script (comments aside)" \
      '! grep -nE "(^|[^-[:alnum:]_/])python3\\b" "$REPO/scripts/sync-workspace.sh" | grep -v "^[0-9]*:[[:space:]]*#" | grep -qv SYNC_PY'
check "the interpreter is resolved ONCE, through the repo's own cascade" \
      'grep -q "python-bin" "$REPO/scripts/sync-workspace.sh"'
check "the durability path FAILS CLOSED with no interpreter (never a silent no-fsync)" \
      'grep -q "no durability guarantee" "$REPO/scripts/sync-workspace.sh"'

# The live control: a PATH whose python3 is a failing stub, plus a good
# SUTANDO_PY. The resolved binary must run and the stub must never be reached.
_pyd="$(mktemp -d)"
cat > "$_pyd/python3" <<'SHIM'
#!/bin/sh
echo called >> "$PYSHIM_LOG"
exit 127
SHIM
chmod +x "$_pyd/python3"
_pylog="$_pyd/called"; _pytarget="$_pyd/t"; echo x > "$_pytarget"
_resolved="$(SUTANDO_PY="$(command -v python3)" PATH="$_pyd:/usr/bin:/bin" \
    bash "$REPO/scripts/sutando-config.sh" python-bin 2>/dev/null || true)"
check "the resolver refuses the stub and returns a working interpreter" \
      '[ -n "$_resolved" ] && [ "$_resolved" != "$_pyd/python3" ]'
PYSHIM_LOG="$_pylog" PATH="$_pyd:/usr/bin:/bin" "$_resolved" -c "import fcntl" 2>/dev/null
check "...and it actually runs under the poisoned PATH" '[ "$?" -eq 0 ]'
check "...without the stub ever being invoked" '[ ! -f "$_pylog" ]'
rm -rf "$_pyd"

[ "$fails" -eq 0 ] && { echo "ALL PASS"; exit 0; }
echo "$fails FAILURE(S)"; exit 1
