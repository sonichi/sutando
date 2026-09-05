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

echo "1. reviewer's control: a RECORDED copy edited independently, stale root TOUCHED LATER"
# The record is what makes a diverged copy evidence of a second writer; a copy
# with no record is adopted instead (5c-5f).
echo "stale relic" > "$WORKSPACE_DIR/build_log.md"
echo "per-host live entry" > "$WORKSPACE_DIR/hosts/testhost/build_log.md"
printf '%064d\n' 1 > "$WORKSPACE_DIR/hosts/testhost/.build_log.snapshot-sha"   # valid, does not match
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

# A copy with NO USABLE record (absent, empty, malformed, NUL-damaged) grants nothing by
# itself: writer direction comes from the append-only relationship, and ambiguity refuses.
_sha_of() { shasum -a 256 "$1" 2>/dev/null | cut -d' ' -f1; }
_SIGF="$WORKSPACE_DIR/hosts/testhost/.build_log.snapshot-sha"
adopts() {   # $1 = case label; root STRICTLY EXTENDS the copy (root-live lag) -> adopted
    printf 'root baseline\n' > "$WORKSPACE_DIR/hosts/testhost/build_log.md"
    printf 'root baseline\nroot moved on\n' > "$WORKSPACE_DIR/build_log.md"
    : > "$LOG"
    _out="$(_snapshot_per_host_config 2>&1 >/dev/null)"
    check "$1: root-live lag: per-host copy is refreshed from root in the SAME tick" \
          '[ "$(cat "$WORKSPACE_DIR/hosts/testhost/build_log.md")" = "$(printf "root baseline\nroot moved on")" ]'
    check "$1: provenance now records the refreshed copy" \
          '[ "$(tr -d "\n" < "$_SIGF")" = "$(_sha_of "$WORKSPACE_DIR/build_log.md")" ]'
    check "$1: the adoption is logged as such" \
          'grep -q "adopted hosts/testhost/build_log.md" "$LOG"'
    check "$1: nothing is refused and no copy is called an independent writer" \
          '[ -z "$_out" ] && ! grep -q "pick ONE writer" "$LOG"'
}
refuses() {  # $1 = label, $2 = root bytes, $3 = per-host bytes, $4 = expected refusal phrase
    _phrase="$4"
    printf '%b' "$2" > "$WORKSPACE_DIR/build_log.md"
    printf '%b' "$3" > "$WORKSPACE_DIR/hosts/testhost/build_log.md"
    _host_was="$(od -An -v -tx1 "$WORKSPACE_DIR/hosts/testhost/build_log.md" | tr -d " \n")"
    _sig_was="$(cat "$_SIGF" 2>/dev/null | od -An -v -tx1 | tr -d " \n")"
    : > "$LOG"
    _out="$(_snapshot_per_host_config 2>&1 >/dev/null)"
    check "$1: the per-host copy is left EXACTLY as it was (byte for byte)" \
          '[ "$(od -An -v -tx1 "$WORKSPACE_DIR/hosts/testhost/build_log.md" | tr -d " \n")" = "$_host_was" ]'
    check "$1: no provenance record is invented or repaired" \
          '[ "$(cat "$_SIGF" 2>/dev/null | od -An -v -tx1 | tr -d " \n")" = "$_sig_was" ]'
    check "$1: the refusal names the shape and reaches the operator" \
          'printf "%s" "$_out" | grep -q "not clobbering" && printf "%s" "$_out" | grep -q "$_phrase"'
    check "$1: nothing is adopted" '! grep -q "adopted hosts/testhost" "$LOG"'
}
# Both mirror cases, driven with no record; the second is the motivating deployment.
norecord_cases() {  # $1 = label, $2 = command re-arranging the unusable record (adoption stamps)
    eval "$2"; adopts "$1"
    eval "$2"; refuses "$1 / host-live" 'stale relic\n' 'stale relic\nper-host live entry\n' "stale relic"
    eval "$2"; refuses "$1 / diverged" 'root is the writer\n' 'independent content\n' "DIVERGED"
}

echo "5c. no record: root-live lag is adopted; per-host-live and diverged are REFUSED"
norecord_cases "absent record" 'rm -f "$_SIGF"'
norecord_cases "EMPTY record" ': > "$_SIGF"'
# Both mirror cases must also hold at the byte level: a shared prefix that ends
# mid-line is still an extension, and a one-byte edit inside root's span is divergence.
rm -f "$_SIGF"
refuses "absent record / host-live mid-line extension" 'stale re' 'stale relic\n' "stale relic"
refuses "absent record / diverged in-span edit" 'stale relic\n' 'stale relix\nmore\n' "DIVERGED"
refuses "absent record / EMPTY root beside a live copy" '' 'per-host live entry\n' "stale relic"

echo "5d. present-but-MALFORMED signature is not usable provenance either"
# A 63-char partial (torn write, stray copy, manual edit) satisfies a bare
# equality test; any non-sha256 content must take the no-record branch.
norecord_cases "63-char partial signature" 'printf "%063d" 0 > "$_SIGF"'
norecord_cases "arbitrary foreign content" 'printf "not hex at all\n" > "$_SIGF"'
norecord_cases "trailing SPACE after the sha" 'printf "%064d " 1 > "$_SIGF"'
# Positive control for the validator: a VALID 64-hex stale sha must still be
# read as a genuine independent writer — adoption must not widen past no-record.
echo "independent content" > "$WORKSPACE_DIR/hosts/testhost/build_log.md"
printf '%064d' 1 > "$WORKSPACE_DIR/hosts/testhost/.build_log.snapshot-sha"
check "a valid-shape stale sha is STILL an independent writer (control)" \
      '_snapshot_per_host_config 2>&1 >/dev/null | grep -q "pick ONE writer"'
check "...and its copy is preserved, not adopted" \
      '[ "$(cat "$WORKSPACE_DIR/hosts/testhost/build_log.md")" = "independent content" ]'
# Pinned leniency: the writer itself emits a trailing newline, so newlines
# after the sha must stay usable.
printf '%064d\n\n\n' 1 > "$WORKSPACE_DIR/hosts/testhost/.build_log.snapshot-sha"
check "a valid sha with trailing blank lines is still USABLE provenance" \
      '_snapshot_per_host_config 2>&1 >/dev/null | grep -q "pick ONE writer"'

echo "5e. NUL damage must be judged on the on-disk bytes, not post-\$() text"
# Shell substitution strips NULs, so a NUL-damaged record collapses to 64
# clean hex chars; the validator must reject the bytes before that happens.
printf 'per-host live entry\n' > "$WORKSPACE_DIR/hosts/testhost/build_log.md"
_live_sha="$(_sha_of "$WORKSPACE_DIR/hosts/testhost/build_log.md")"
norecord_cases "trailing-NUL signature" 'printf "%s\0" "$_live_sha" > "$_SIGF"'
norecord_cases "embedded-NUL signature" \
    'printf "%.32s\0%.32s" "$_live_sha" "${_live_sha:32}" > "$_SIGF"'

echo "5f. an adoption whose record cannot be made durable does NOT touch the copy"
rm -f "$WORKSPACE_DIR/hosts/testhost/.build_log.snapshot-sha"
printf 'root baseline\n' > "$WORKSPACE_DIR/hosts/testhost/build_log.md"
printf 'root baseline\nroot moved on\n' > "$WORKSPACE_DIR/build_log.md"
chmod 555 "$WORKSPACE_DIR/hosts/testhost"        # the stamp's temp cannot be staged
_out="$(_snapshot_per_host_config 2>&1 >/dev/null)"
chmod 755 "$WORKSPACE_DIR/hosts/testhost"
check "no durable record -> the lagging copy is left exactly as it was" \
      '[ "$(cat "$WORKSPACE_DIR/hosts/testhost/build_log.md")" = "root baseline" ]'
check "...and the operator hears it could not be adopted, not that it was refused as a writer" \
      'printf "%s" "$_out" | grep -q "could not be adopted this tick" && ! printf "%s" "$_out" | grep -q "pick ONE writer"'
check "...and no provenance record was invented" \
      '[ ! -e "$WORKSPACE_DIR/hosts/testhost/.build_log.snapshot-sha" ]'
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

echo "9. a FAILED in-place replace must clean up and say so (hosts/*/ is a carried vault path)"
printf 'root advanced\n' > "$WORKSPACE_DIR/build_log.md"
printf 'ours\n' > "$WORKSPACE_DIR/hosts/testhost/build_log.md"
shasum -a 256 "$WORKSPACE_DIR/hosts/testhost/build_log.md" | cut -d" " -f1 \
    > "$WORKSPACE_DIR/hosts/testhost/.build_log.snapshot-sha"
_sig_before="$(cat "$WORKSPACE_DIR/hosts/testhost/.build_log.snapshot-sha")"
# The replace runs through the resolved interpreter; fail it there (full disk, EIO, ...).
_rs="$(mktemp -d)"
printf '#!/bin/sh\ncase "$2" in --replace) exit 1 ;; esac\nexec "%s" "$@"\n' "$(command -v python3)" > "$_rs/python3"
chmod +x "$_rs/python3"
_out9="$(SYNC_PY="$_rs/python3" _snapshot_per_host_config 2>&1)"
rm -rf "$_rs"
check "no orphan temp is left behind for the vault to carry" \
      '[ -z "$(ls "$WORKSPACE_DIR/hosts/testhost"/build_log.md.snap.* 2>/dev/null)" ]'
# Log-only BY DESIGN: the temp was removed and nothing lost, so it is a
# diagnostic, not an operator action. Asserted against $LOG for that reason.
check "the failed replace is recorded in \$LOG, not swallowed" \
      'grep -qi "in-place replace of" "$LOG"'
check "and it stays OUT of the operator stream (not every failure is an action)" \
      '[ -z "$(printf %s "$_out9")" ]'
check "provenance is not stamped for a replace that did not happen" \
      '[ "$(cat "$WORKSPACE_DIR/hosts/testhost/.build_log.snapshot-sha")" = "$_sig_before" ]'
check "and the destination is untouched" \
      '[ "$(cat "$WORKSPACE_DIR/hosts/testhost/build_log.md")" = "ours" ]'


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
# The publish is intent -> mv -> promote. Each boundary is exercised by leaving the
# state it produces, then asserting what the NEXT tick does with it.
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
# A best-effort chain lets the destination be renamed with no durable intent. Each
# injection asserts destination and signature are left CONSISTENT.
_SIG="$WORKSPACE_DIR/hosts/testhost/.build_log.snapshot-sha"
_INT="$WORKSPACE_DIR/hosts/testhost/.build_log.snapshot-sha.next"
_DST="$WORKSPACE_DIR/hosts/testhost/build_log.md"

# Inject a durability failure through SYNC_PY, the seam the resolver honours. The argument
# is a full glob: `<dst>.snap.XXXXXX` is fsynced first, so a bare substring hits staging.
_fsync_shim() {
    _fs="$(mktemp -d)"
    cat > "$_fs/python3" <<SHIM
#!/bin/sh
case "\$2" in $1) exit 1 ;; esac
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
_fs="$(_fsync_shim "*.snapshot-sha.next")"
SYNC_PY="$_fs/python3" _snapshot_per_host_config >/dev/null 2>&1
rm -rf "$_fs"
check "a) intent not durable: the destination is NOT replaced" \
      '[ "$(cat "$_DST")" = "$_before" ]'
check "...and no intent is left behind" '[ ! -f "$_INT" ]'

# (b) the DESTINATION cannot be confirmed durable -> the signature must NOT be
# promoted; the intent survives so the next tick can verify and finish.
_arm_owned_snapshot
_sig_before="$(cat "$_SIG")"
_fs="$(_fsync_shim "*/build_log.md")"
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

# (c) a non-atomic in-place signature write must not exist as a fallback: with the
# promote rename failing, the signature must stay UNCHANGED.
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

# (d) the promote rename already CONSUMED the intent, so a non-durable signature must
# leave a fresh one. Anchor the shim on `.snapshot-sha`, not `.snapshot-sha.next`.
_sig_only_shim() {
    _fs="$(mktemp -d)"
    cat > "$_fs/python3" <<SHIM
#!/bin/sh
case "\$2" in *.snapshot-sha) exit 1 ;; esac
exec "$(command -v python3)" "\$@"
SHIM
    chmod +x "$_fs/python3"
    printf '%s' "$_fs"
}

_arm_owned_snapshot
_fs="$(_sig_only_shim)"
_logmark_d="$(wc -c < "$LOG")"
SYNC_PY="$_fs/python3" _snapshot_per_host_config >/dev/null 2>&1
rm -rf "$_fs"
check "d) signature not confirmed durable: a recovery intent is left behind" \
      '[ -f "$_INT" ]'
check "...and it records the sha that was being published" \
      '[ "$(cat "$_INT")" = "$(shasum -a 256 "$_DST" | cut -d" " -f1)" ]'
check "...and the log says the intent was re-created, not that it completed" \
      'tail -c "+$((_logmark_d+1))" "$LOG" | grep -q "intent re-created for the next tick"'
# ...and that leftover intent is recoverable: the next clean tick finishes it.
_snapshot_per_host_config >/dev/null 2>&1
check "...so the NEXT tick promotes it and clears the intent" \
      '[ ! -f "$_INT" ] && [ "$(cat "$_SIG")" = "$(shasum -a 256 "$_DST" | cut -d" " -f1)" ]'
rm -f "$_INT"; rm -f "$_SIG"

# (e) same asymmetry on the RECOVERY path: recovery renames the intent onto the
# signature, so a non-durable signature there must also re-create the intent.
printf 'recovered body\n' > "$_DST"
shasum -a 256 "$_DST" | cut -d' ' -f1 > "$_INT"
printf 'stale-signature-value\n' > "$_SIG"
cp "$_DST" "$WORKSPACE_DIR/build_log.md"
_fs="$(_sig_only_shim)"
_logmark_e="$(wc -c < "$LOG")"
SYNC_PY="$_fs/python3" _snapshot_per_host_config >/dev/null 2>&1
rm -rf "$_fs"
check "e) recovery with a non-durable signature re-creates the intent" \
      '[ -f "$_INT" ]'
check "...and does NOT log the publish as completed" \
      '! tail -c "+$((_logmark_e+1))" "$LOG" | grep -q "completed an interrupted publish"'
rm -f "$_INT"; rm -f "$_SIG"

# ---- 12. the durable-publish path uses the REPO'S verified interpreter -------
# A bare `python3` can be the Xcode-CLT stub: it exists, then fails on exec.
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

echo "14. the equal-content REPAIR obeys the durable publish contract"
# The repair branch (dest == root, signature missing or stale) used to write in place
# with its failure swallowed — the partial-signature risk rename-only avoids.

_arm_stale_sig_equal_content() {
    printf 'same on both sides\n' > "$WORKSPACE_DIR/build_log.md"
    printf 'same on both sides\n' > "$_DST"
    printf '%s\n' "0000000000000000000000000000000000000000000000000000000000000000" > "$_SIG"
    rm -f "$_INT"
}

# (a) happy path: a stale signature over equal content is repaired to the truth.
_arm_stale_sig_equal_content
_want="$(shasum -a 256 "$_DST" | cut -d' ' -f1)"
_snapshot_per_host_config >/dev/null 2>&1
check "a) a stale signature over equal content is repaired" \
      '[ "$(cat "$_SIG" 2>/dev/null)" = "$_want" ]'
check "...and no repair temp is left in the carried vault path" \
      '[ -z "$(ls "$WORKSPACE_DIR/hosts/testhost"/.build_log.snapshot-sha.repair.* 2>/dev/null)" ]'

# (b) the repair cannot be made durable BEFORE promotion -> the old record
#     stands. An in-place write would already have overwritten it here.
_arm_stale_sig_equal_content
_stale="$(cat "$_SIG")"
_fs="$(_fsync_shim "*.snapshot-sha.repair.*")"
SYNC_PY="$_fs/python3" _snapshot_per_host_config >/dev/null 2>&1
rm -rf "$_fs"
check "b) repair not durable: the previous signature is untouched" \
      '[ "$(cat "$_SIG" 2>/dev/null)" = "$_stale" ]'
check "...and the temp is cleaned up, not left to be committed" \
      '[ -z "$(ls "$WORKSPACE_DIR/hosts/testhost"/.build_log.snapshot-sha.repair.* 2>/dev/null)" ]'
check "...and the refusal is recorded durably rather than silently dropped" \
      'grep -q "signature repair not confirmed durable" "$LOG"'

# (c) the signature is never observed partial: whatever a reader finds is
#     either the old record or the new one, never a truncated write.
_arm_stale_sig_equal_content
_snapshot_per_host_config >/dev/null 2>&1
check "c) the promoted signature is a whole 64-hex record, never truncated" \
      '[ "$(od -An -v -tx1 "$_SIG" | tr -d " \n" | sed "s/0a*$//" | wc -c | tr -d " ")" = "128" ]'

echo "15. an appender's ALREADY-OPEN descriptor survives the refresh (inode preserved)"
# Opened before the snapshot, written after it returns: the bytes must land in the
# destination, not in a detached inode. The merge-base kept this; an inode swap loses it.
_arm_owned_snapshot
_ino_before="$(ls -i "$_DST" | awk "{print \$1}")"
exec 7>>"$_DST"
_snapshot_per_host_config >/dev/null 2>&1
printf 'late append through the old descriptor\n' >&7
exec 7>&-
check "the refresh itself landed" 'grep -q "root moved on" "$_DST"'
check "the late append is IN the destination (destination_append_count=1)" \
      '[ "$(grep -c "late append through the old descriptor" "$_DST")" = "1" ]'
check "the destination inode is unchanged" \
      '[ "$(ls -i "$_DST" | awk "{print \$1}")" = "$_ino_before" ]'
check "and provenance records the refreshed bytes, not the appended ones" \
      '[ "$(cat "$_SIG")" = "$(printf "root moved on\n" | shasum -a 256 | cut -d" " -f1)" ]'
# Control: the same descriptor pattern on the PRE-fix shape (rename over the inode)
# must FAIL this check, or the probe cannot discriminate.
_arm_owned_snapshot
exec 7>>"$_DST"
cp "$WORKSPACE_DIR/build_log.md" "$_DST.ctl" && /bin/mv -f "$_DST.ctl" "$_DST"
printf 'late append through the old descriptor\n' >&7
exec 7>&-
check "control: a rename-over replace DOES lose the late append" \
      '[ "$(grep -c "late append through the old descriptor" "$_DST")" = "0" ]'

echo "16. a SHORT WRITE after the truncate is rolled forward, never left partial"
# A one-block soft RLIMIT_FSIZE applied ONLY to the named python mode(s): staging (cp) and
# the fsync helper run unlimited, so the truncate happens and the write stops after one block.
_fsize_shim() {
    _fz="$(mktemp -d)"
    cat > "$_fz/python3" <<SHIM
#!/bin/bash
case "\$2" in $1) ulimit -S -f 1 ;; esac
exec "$(command -v python3)" "\$@"
SHIM
    chmod +x "$_fz/python3"
    printf '%s' "$_fz"
}
_arm_big_root() {
    # an owned snapshot whose root then grows well past one block
    printf 'owned baseline\n' > "$WORKSPACE_DIR/build_log.md"
    printf 'owned baseline\n' > "$_DST"
    shasum -a 256 "$_DST" | cut -d' ' -f1 > "$_SIG"
    rm -f "$_INT" "$_DST".snap.??????
    { printf 'owned baseline\n'
      awk 'BEGIN{for(i=0;i<200;i++)printf "root advanced line %04d ........................................\n", i}'
    } > "$WORKSPACE_DIR/build_log.md"
}
_root_sha() { shasum -a 256 "$WORKSPACE_DIR/build_log.md" | cut -d' ' -f1; }
_dst_is_strict_prefix_of_root() {
    _n="$(wc -c < "$_DST" | tr -d ' ')"
    [ "$_n" -gt 0 ] && [ "$_n" -lt "$(wc -c < "$WORKSPACE_DIR/build_log.md" | tr -d ' ')" ] &&
        head -c "$_n" "$WORKSPACE_DIR/build_log.md" | cmp -s - "$_DST"
}

# (a) same tick: --replace stops short, --rollforward (unlimited) finishes it.
_arm_big_root
_fz="$(_fsize_shim "--replace")"
SYNC_PY="$_fz/python3" _snapshot_per_host_config > "$SB/16a.out" 2>&1; _rc16a=$?
rm -rf "$_fz"
check "a) after a short write the destination holds the WHOLE root" \
      'cmp -s "$WORKSPACE_DIR/build_log.md" "$_DST"'
check "...provenance records the whole bytes" '[ "$(cat "$_SIG")" = "$(_root_sha)" ]'
check "...no staged copy or intent is left behind" \
      '[ ! -f "$_INT" ] && [ -z "$(ls "$_DST".snap.* 2>/dev/null)" ]'
check "...the function reports success (nothing for the tick to withhold)" '[ "$_rc16a" -eq 0 ]'
check "...and the same-tick roll-forward is recorded in \$LOG" \
      'grep -q "stopped short; rolled forward from the staged copy in the same tick" "$LOG"'

# (b) the roll-forward ALSO stops short: partial is reported, kept recoverable, withheld from the push.
_arm_big_root
_fz="$(_fsize_shim "--replace|--rollforward")"
SYNC_PY="$_fz/python3" _snapshot_per_host_config > "$SB/16b.out" 2>&1; _rc16b=$?
rm -rf "$_fz"
check "b) both writes short: the destination is a strict PREFIX of root" '_dst_is_strict_prefix_of_root'
check "...the signature still names the OLD bytes (nothing claimed that is not there)" \
      '[ "$(cat "$_SIG")" = "$(printf "owned baseline\n" | shasum -a 256 | cut -d" " -f1)" ]'
check "...the staged copy and the intent are KEPT for recovery" \
      '[ -f "$_INT" ] && [ -n "$(ls "$_DST".snap.* 2>/dev/null)" ]'
check "...the function returns 3 so the tick withholds the push" '[ "$_rc16b" -eq 3 ]'
check "...and the operator is told on stderr" 'grep -q "PARTIAL" "$SB/16b.out"'
# ...next tick, unlimited: recovery rolls forward from the kept copy and promotes.
_snapshot_per_host_config > "$SB/16b2.out" 2>&1; _rc16b2=$?
check "...the next tick rolls forward to the WHOLE root" 'cmp -s "$WORKSPACE_DIR/build_log.md" "$_DST"'
check "...promotes provenance" '[ "$(cat "$_SIG")" = "$(_root_sha)" ]'
check "...cleans up and reports success" \
      '[ ! -f "$_INT" ] && [ -z "$(ls "$_DST".snap.* 2>/dev/null)" ] && [ "$_rc16b2" -eq 0 ]'
check "...and the recovery is recorded in \$LOG" \
      'grep -q "rolled hosts/testhost/build_log.md forward from its staged copy" "$LOG"'

# (c) same as (b), but the next tick arrives past the stage grace window (scheduler = 900s):
# the sweep must not take the intent's source before recovery runs.
_arm_big_root
_fz="$(_fsize_shim "--replace|--rollforward")"
SYNC_PY="$_fz/python3" _snapshot_per_host_config > "$SB/16c.out" 2>&1; _rc16c=$?
rm -rf "$_fz"
check "c) aged: the staged copy and the intent are kept after the short writes" \
      '[ "$_rc16c" -eq 3 ] && [ -f "$_INT" ] && [ -n "$(ls "$_DST".snap.* 2>/dev/null)" ]'
touch -t 202601010000 "$_DST".snap.* "$_INT"
_snapshot_per_host_config > "$SB/16c2.out" 2>&1; _rc16c2=$?
check "...an aged next tick still rolls forward to the WHOLE root" 'cmp -s "$WORKSPACE_DIR/build_log.md" "$_DST"'
check "...promotes provenance and cleans up" \
      '[ "$(cat "$_SIG")" = "$(_root_sha)" ] && [ ! -f "$_INT" ] && [ -z "$(ls "$_DST".snap.* 2>/dev/null)" ] && [ "$_rc16c2" -eq 0 ]'

# (d) partial destination whose staged copy is GONE: nothing to roll forward from, so the tick
# keeps withholding (rc 3) with the intent intact instead of discarding it and pushing a prefix.
_arm_big_root
_fz="$(_fsize_shim "--replace|--rollforward")"
SYNC_PY="$_fz/python3" _snapshot_per_host_config > "$SB/16d.out" 2>&1; _rc16d=$?
rm -rf "$_fz"
rm -f "$_DST".snap.*
_snapshot_per_host_config > "$SB/16d2.out" 2>&1; _rc16d2=$?
check "d) no source: a still-partial destination keeps returning 3" '[ "$_rc16d" -eq 3 ] && [ "$_rc16d2" -eq 3 ]'
check "...the intent is kept and the destination is still the partial prefix" \
      '[ -f "$_INT" ] && _dst_is_strict_prefix_of_root'
check "...and the operator is told the source is gone" 'grep -q "staged copy is gone" "$SB/16d2.out"'
rm -f "$_INT"

# (c) KILL mid-write: only the on-disk state survives — a prefix, a durable intent, a staged copy.
_arm_big_root
_ino="$(ls -i "$_DST" | awk "{print \$1}")"
_stg="$(mktemp "$_DST.snap.XXXXXX")"; cp "$WORKSPACE_DIR/build_log.md" "$_stg"; _root_sha > "$_INT"
head -c 700 "$WORKSPACE_DIR/build_log.md" > "$_DST"
_snapshot_per_host_config > /dev/null 2>&1; _rc16c=$?
check "c) a kill mid-write is completed from the staged copy on the next tick" \
      'cmp -s "$WORKSPACE_DIR/build_log.md" "$_DST" && [ "$_rc16c" -eq 0 ]'
check "...with provenance promoted and nothing left behind" \
      '[ "$(cat "$_SIG")" = "$(_root_sha)" ] && [ ! -f "$_INT" ] && [ -z "$(ls "$_DST".snap.* 2>/dev/null)" ]'
check "...and the destination inode is unchanged by the roll-forward" \
      '[ "$(ls -i "$_DST" | awk "{print \$1}")" = "$_ino" ]'

# (d) control: a destination that is NOT a prefix of the staged copy is never written over.
_arm_big_root
_stg="$(mktemp "$_DST.snap.XXXXXX")"; cp "$WORKSPACE_DIR/build_log.md" "$_stg"; _root_sha > "$_INT"
{ head -c 700 "$WORKSPACE_DIR/build_log.md"; printf 'foreign append\n'; } > "$_DST"
_before="$(shasum -a 256 < "$_DST")"
_snapshot_per_host_config > "$SB/16d.out" 2>&1
check "d) control: a non-prefix destination is left exactly as found" \
      '[ "$(shasum -a 256 < "$_DST")" = "$_before" ]'
check "...the intent and staged copy are discarded, loudly" \
      '[ ! -f "$_INT" ] && [ -z "$(ls "$_DST".snap.* 2>/dev/null)" ] && grep -q "neither its publish intent" "$SB/16d.out"'
check "...and the signature is not promoted to bytes that are not there" \
      '[ "$(cat "$_SIG")" != "$(_root_sha)" ]'

# (e) control: a staged copy whose sha is NOT the intent's grants nothing — and with the
# destination still a partial prefix, the tick withholds (rc 3) rather than discarding the intent.
_arm_big_root
_stg="$(mktemp "$_DST.snap.XXXXXX")"; printf 'unrelated staged bytes\n' > "$_stg"; _root_sha > "$_INT"
head -c 700 "$WORKSPACE_DIR/build_log.md" > "$_DST"
_snapshot_per_host_config > /dev/null 2>&1; _rc16e=$?
check "e) control: an unmatched staged copy is ignored (destination untouched, not rolled forward from it)" \
      '[ "$(wc -c < "$_DST" | tr -d " ")" = "700" ] && [ "$(cat "$_SIG")" != "$(_root_sha)" ]'
check "...and a partial destination with no usable source keeps its intent and withholds" \
      '[ -f "$_INT" ] && [ "$_rc16e" -eq 3 ]'
rm -f "$_stg" "$_INT"

echo "17. the WRITER DIES mid-replace: a signal exit is an UNKNOWN outcome, verified, never pushed as whole"
# The shim SIGKILLs the real --replace after the truncate (destination EMPTY) or before it; the
# caller sees 137, not the 3 that names a partial write. --rollforward may be limited to one block.
_kill_shim() {
    _kz="$(mktemp -d)"
    if [ "$1" = after-truncate ]; then
        printf '%s\n' 'import os, signal' '_t = os.ftruncate' \
            'def _k(fd, n):' '    _t(fd, n); os.fsync(fd); os.kill(os.getpid(), signal.SIGKILL)' \
            'os.ftruncate = _k' > "$_kz/patch.py"
    else
        printf '%s\n' 'import os, signal, fcntl' \
            'def _k(fd, op): os.kill(os.getpid(), signal.SIGKILL)' 'fcntl.flock = _k' > "$_kz/patch.py"
    fi
    cat > "$_kz/python3" <<SHIM
#!/bin/bash
case "\$2" in
    --replace) { cat "$_kz/patch.py"; cat; } | "$(command -v python3)" "\$@"; exit \$? ;;
    --rollforward) [ "${2:-}" = short-rollforward ] && ulimit -S -f 1 ;;
esac
exec "$(command -v python3)" "\$@"
SHIM
    chmod +x "$_kz/python3"; printf '%s' "$_kz"
}

# (a) killed AFTER the truncate: the destination is empty on disk, the child exits 137.
_arm_big_root
_kz="$(_kill_shim after-truncate)"
SYNC_PY="$_kz/python3" _snapshot_per_host_config > "$SB/17a.out" 2>&1; _rc17a=$?
rm -rf "$_kz"
check "a) control: the shim really killed the writer (something other than 0/2/3 reached the caller is logged)" \
      'grep -q "writer exit 137" "$LOG"'
check "...the destination is NOT the empty file the truncate left" '[ -s "$_DST" ]'
check "...it holds the WHOLE root (rolled forward from the staged copy in the same tick)" \
      'cmp -s "$WORKSPACE_DIR/build_log.md" "$_DST"'
check "...provenance records the whole bytes" '[ "$(cat "$_SIG")" = "$(_root_sha)" ]'
check "...no staged copy or intent is left behind" \
      '[ ! -f "$_INT" ] && [ -z "$(ls "$_DST".snap.* 2>/dev/null)" ]'
check "...and the function reports success only because the copy is complete" '[ "$_rc17a" -eq 0 ]'

# (b) killed BEFORE the truncate (in the lock): nothing was mutated, and that is VERIFIED, not assumed.
_arm_big_root
_kz="$(_kill_shim before-truncate)"
SYNC_PY="$_kz/python3" _snapshot_per_host_config > "$SB/17b.out" 2>&1; _rc17b=$?
rm -rf "$_kz"
check "b) the destination still holds its OLD bytes" \
      '[ "$(cat "$_DST")" = "owned baseline" ]'
check "...the signature still names them" \
      '[ "$(cat "$_SIG")" = "$(printf "owned baseline\n" | shasum -a 256 | cut -d" " -f1)" ]'
check "...staged copy and intent are discarded (nothing to recover)" \
      '[ ! -f "$_INT" ] && [ -z "$(ls "$_DST".snap.* 2>/dev/null)" ]'
check "...the function reports success (the tick has nothing to withhold)" '[ "$_rc17b" -eq 0 ]'
check "...and the log says the destination was verified untouched" \
      'grep -q "before touching the destination" "$LOG"'

# (c) killed AFTER the truncate AND the same-tick roll-forward stops short: the tick withholds the
# push (rc 3) with the evidence kept, and the next unlimited tick completes it.
_arm_big_root
_kz="$(_kill_shim after-truncate short-rollforward)"
SYNC_PY="$_kz/python3" _snapshot_per_host_config > "$SB/17c.out" 2>&1; _rc17c=$?
rm -rf "$_kz"
check "c) the destination is a strict PREFIX of root (not empty, not whole)" '_dst_is_strict_prefix_of_root'
check "...the staged copy and the intent are KEPT" \
      '[ -f "$_INT" ] && [ -n "$(ls "$_DST".snap.* 2>/dev/null)" ]'
check "...the function returns 3 so the push is withheld" '[ "$_rc17c" -eq 3 ]'
check "...and the operator is told on stderr" 'grep -q "PARTIAL" "$SB/17c.out"'
_snapshot_per_host_config > "$SB/17c2.out" 2>&1; _rc17c2=$?
check "...the next tick rolls forward to the WHOLE root and promotes" \
      'cmp -s "$WORKSPACE_DIR/build_log.md" "$_DST" && [ "$(cat "$_SIG")" = "$(_root_sha)" ] && [ "$_rc17c2" -eq 0 ]'

[ "$fails" -eq 0 ] && { echo "ALL PASS"; exit 0; }
echo "$fails FAILURE(S)"; exit 1
