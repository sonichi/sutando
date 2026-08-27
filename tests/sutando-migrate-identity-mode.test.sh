#!/usr/bin/env bash
# Identity = bytes AND mode, uniformly: mode-only differences are divergence,
# ignorable entries never outrank actionable ones, unverified is never genuine.
set -u
cd "$(dirname "$0")/.."
fails=0
check() { if [ "$2" = "$3" ]; then echo "  ok  $1"; else echo "FAIL  $1 — got '$2', want '$3'"; fails=$((fails+1)); fi; }

# --- control 1: identity_match itself, plus the commit-path consequence ---
# Extract and exercise the REAL helpers from the shipped script.
tmp="$(mktemp -d -t migrate-identity.XXXXXX)"
trap 'rm -rf "$tmp"' EXIT
printf '#!/bin/sh\necho hi\n' > "$tmp/a.sh"; chmod 0755 "$tmp/a.sh"
printf '#!/bin/sh\necho hi\n' > "$tmp/b.sh"; chmod 0644 "$tmp/b.sh"
printf '#!/bin/sh\necho hi\n' > "$tmp/c.sh"; chmod 0755 "$tmp/c.sh"
helpers="$tmp/helpers.sh"
sed -n '/^_stat() {/,/^}/p; /^mode_of() {/,/^}/p; /^identity_match() {/,/^}/p; /^sha_match() {/,/^}/p' scripts/sutando-migrate.sh > "$helpers"
# shellcheck disable=SC1090
. "$helpers"
# A failed extraction must fail loudly HERE: command-not-found inside a
# yes/no capture reads as a clean "no" and false-passes the no-expecting checks.
type identity_match >/dev/null 2>&1 || { echo "FAIL  helper extraction produced no identity_match"; exit 1; }
type _stat >/dev/null 2>&1 || { echo "FAIL  helper extraction produced no _stat (mode_of would return empty and every identity check would false-pass)"; exit 1; }
check "same bytes + same mode IS identical" "$(identity_match "$tmp/a.sh" "$tmp/c.sh" && echo yes || echo no)" "yes"
check "same bytes + different mode is NOT identical (exec bit must survive)" \
  "$(identity_match "$tmp/a.sh" "$tmp/b.sh" && echo yes || echo no)" "no"
check "different bytes never identical whatever the mode" \
  "$(printf 'other\n' > "$tmp/d.sh"; chmod 0755 "$tmp/d.sh"; identity_match "$tmp/a.sh" "$tmp/d.sh" && echo yes || echo no)" "no"
# a failing mode probe
# must FAIL the identity, never blank-compare into a false duplicate.
mode_of() { return 1; }
check "both mode probes failing -> NOT identical (fail closed)" \
  "$(identity_match "$tmp/a.sh" "$tmp/c.sh" && echo yes || echo no)" "no"
mode_of() { echo ""; }
check "empty mode output -> NOT identical (fail closed)" \
  "$(identity_match "$tmp/a.sh" "$tmp/c.sh" && echo yes || echo no)" "no"
# restore the real helper for anything below
# shellcheck disable=SC1090
. "$helpers"

# every commit/delete/verify decision site uses identity_match, none bare sha_match
bare="$(grep -cE 'if (\[ -f "\$(cand|g)" \] && )?sha_match ' scripts/sutando-migrate.sh)"
check "no decision site bypasses the mode check (bare sha_match ifs)" "$bare" "0"

# --- controls 2+3: the summary logic, driven through the real python block ---
summary_out="$(python3 - <<'PYEOF'
import json, os, tempfile, hashlib, subprocess, re, sys
# Run the shipped summary python against a synthetic index:
# 50 identical(mtime-diff) + 1 divergent + the unreadable-only variant.
src = open("scripts/sutando-migrate.sh").read()
m = re.search(r"_MAP = \{\"identical\": True.*?json\.dump\(out, sys\.stdout, indent=2\)", src, re.S)
assert m, "summary block not found"
block = m.group(0)
def run_case(verdict_map, collisions):
    g = {"_verdicts": verdict_map, "collisions": collisions,
         "by_rel": {k: [1,2] for k in collisions}, "defaultdict": __import__("collections").defaultdict,
         "json": json, "sys": sys, "a": "", "b": "", "c": "", "dest": "d"}
    import io
    old = sys.stdout; sys.stdout = io.StringIO()
    try:
        exec(block + "\n", g)
        return json.loads(sys.stdout.getvalue())
    finally:
        sys.stdout = old
def entry(tag, mtime, size):
    return {"tag": tag, "mtime": mtime, "size": size, "class": "structural"}
# case A: 50 identical-with-differing-mtimes + 1 proven divergent (same size/mtime)
coll = {}
verd = {}
for i in range(50):
    k = f"zz-ident-{i:02d}"          # names sort AFTER the divergent one alphabetically? make them sort FIRST to stress the cap:
    k = f"aa-ident-{i:02d}"
    coll[k] = [entry("A", 100+i, 10), entry("B", 200+i, 10)]
    verd[k] = "identical"
coll["zz-the-divergent"] = [entry("A", 100, 10), entry("B", 200, 10)]
verd["zz-the-divergent"] = "divergent"
outA = run_case(verd, coll)
notable = outA["notable_collisions"]
first = notable[0]["rel"] if notable else ""
present = any(r["rel"] == "zz-the-divergent" for r in notable)
# case B: unreadable-only
outB = run_case({"u1": "unverified"}, {"u1": [entry("A", 1, 5), entry("B", 2, 6)]})
print(json.dumps({
    "A_first": first, "A_divergent_present": present,
    "A_genuine": outA["totals"]["genuine_conflicts"],
    "B_genuine": outB["totals"]["genuine_conflicts"],
    "B_unverified": outB["totals"]["identity_unverified"],
}))
PYEOF
)"
A_first="$(python3 -c "import json,sys;print(json.loads(sys.argv[1])['A_first'])" "$summary_out")"
A_present="$(python3 -c "import json,sys;print(json.loads(sys.argv[1])['A_divergent_present'])" "$summary_out")"
A_genuine="$(python3 -c "import json,sys;print(json.loads(sys.argv[1])['A_genuine'])" "$summary_out")"
B_genuine="$(python3 -c "import json,sys;print(json.loads(sys.argv[1])['B_genuine'])" "$summary_out")"
B_unverified="$(python3 -c "import json,sys;print(json.loads(sys.argv[1])['B_unverified'])" "$summary_out")"
check "51-collision: the one divergent entry renders FIRST" "$A_first" "zz-the-divergent"
check "51-collision: the divergent entry survives the cap" "$A_present" "True"
check "51-collision: genuine_conflicts counts only the proven one" "$A_genuine" "1"
check "unreadable-only: genuine_conflicts is 0" "$B_genuine" "0"
check "unreadable-only: surfaced as identity_unverified" "$B_unverified" "1"


# --- control 4: the PRODUCTION scan -> commit -> verify path, not helpers ---
# The union writer creates a fresh temp and `mv`s it over the destination, so
# without an explicit policy the process umask decides the result and a private
# 0600 allowlist silently becomes 0644 while scan and verify both report clean.
# Every check below drives the real CLI; helper-level coverage cannot see this.
e2e="$(mktemp -d -t migrate-mode-e2e.XXXXXX)"
trap 'rm -rf "$tmp" "$e2e"' EXIT
SRC="$e2e/A"; DEST="$e2e/dest"
mkdir -p "$SRC/state" "$DEST/state"
REL="state/slack-allowed-recipients.json"   # the shipped union-json-array rule
printf '{"allow": ["a@example.org"]}\n' > "$SRC/$REL";  chmod 0644 "$SRC/$REL"
printf '{"allow": ["b@example.org"]}\n' > "$DEST/$REL"; chmod 0600 "$DEST/$REL"

RUN_E2E() { SUTANDO_MIGRATE_SRC_A="$SRC" SUTANDO_MIGRATE_DEST="$DEST" \
            bash scripts/sutando-migrate.sh "$@" 2>&1; }

_scan="$(RUN_E2E scan --source A)"
check "the union collision is SURFACED by scan, not silently absent" \
  "$(printf '%s' "$_scan" | grep -c 'slack-allowed-recipients' | tr -d ' ')" "1"

RUN_E2E commit --source A >/dev/null 2>&1
# GNU first — see mode_of()'s note: BSD-first emits filesystem info AND the mode.
_mode="$(stat -c '%a' "$DEST/$REL" 2>/dev/null || stat -f '%Lp' "$DEST/$REL" 2>/dev/null)"
check "a 0644 source must NOT widen a 0600 destination through the union" "$_mode" "600"
check "and the union still merged both allow-lists" \
  "$(grep -c 'a@example.org' "$DEST/$REL" | tr -d ' ')" "1"
check "the pre-existing entry survives the union" \
  "$(grep -c 'b@example.org' "$DEST/$REL" | tr -d ' ')" "1"

# Positive control: the check above must be capable of FAILING. A destination
# the union never touched would also read 0600, so prove the path ran.
check "the destination was actually rewritten (control: not an untouched file)" \
  "$(grep -c 'a@example.org' "$DEST/$REL" | tr -d ' ')" "1"


# --- control 5: byte_identical is a claim about BYTES, through the real JSON ---
# Reviewer input: equal bytes, equal mtime, modes 0755 vs 0644 previously reported
# byte_identical=False and proxy_identical_divergent=1 — no byte differed. Bytes
# and mode are now two fields; every DECISION still uses both, so drop-safety is
# unchanged and only the reporting was split.
j5="$(mktemp -d -t migrate-json5.XXXXXX)"
_scan_case() {  # $1=bytes_differ(0|1) $2=srcmode $3=dstmode -> prints compact json
    local w="$j5/$RANDOM$RANDOM"; mkdir -p "$w/A/notes" "$w/dest/notes"
    # SAME LENGTH as the destination on purpose: proxy_identical_divergent means
    # equal size + equal mtime + PROVEN different bytes, so a shorter variant
    # would be a size_mismatch instead and never exercise that counter.
    if [ "$1" = "1" ]; then printf 'IDENTICAL BYTES\n' > "$w/A/notes/same.md"
    else printf 'identical bytes\n' > "$w/A/notes/same.md"; fi
    printf 'identical bytes\n' > "$w/dest/notes/same.md"
    chmod "$2" "$w/A/notes/same.md"; chmod "$3" "$w/dest/notes/same.md"
    touch -t 202606010000 "$w/A/notes/same.md" "$w/dest/notes/same.md"
    SUTANDO_MIGRATE_SRC_A="$w/A" SUTANDO_MIGRATE_DEST="$w/dest" \
        bash scripts/sutando-migrate.sh scan --json --source A 2>/dev/null \
        | python3 -c 'import sys,json;d=sys.stdin.read();j=json.loads(d[d.index("{"):]);r={x["rel"]:x for x in j["notable_collisions"]}.get("notes/same.md",{});print("%s|%s|%s|%s"%(r.get("byte_identical"),r.get("mode_conflict"),j["totals"]["proxy_identical_divergent"],j["totals"]["identical_content"]))'
}
_m="$(_scan_case 0 0755 0644)"
check "mode-only difference: byte_identical is True (no byte differs)" "$(echo "$_m" | cut -d'|' -f1)" "True"
check "mode-only difference: mode_conflict is True" "$(echo "$_m" | cut -d'|' -f2)" "True"
check "mode-only difference is NOT proven byte divergence" "$(echo "$_m" | cut -d'|' -f3)" "0"
check "mode-only difference is NOT drop-safe (identical_content stays 0)" "$(echo "$_m" | cut -d'|' -f4)" "0"
# Control: real byte divergence must still report as such, or the split above
# could be satisfied by a scan that simply stopped detecting divergence.
_d="$(_scan_case 1 0644 0644)"
check "control: real byte divergence still reports byte_identical False" "$(echo "$_d" | cut -d'|' -f1)" "False"
check "control: real byte divergence still counts as proven divergence" "$(echo "$_d" | cut -d'|' -f3)" "1"
# Control: the only drop-safe shape still reads drop-safe.
_i="$(_scan_case 0 0644 0644)"
check "control: equal bytes AND equal modes remain drop-safe" "$(echo "$_i" | cut -d'|' -f4)" "1"
# A mode-only difference must remain ACTIONABLE, not merely reported. Presence in
# notable_collisions cannot show that — the cap admits ignorable rows too — but
# ORDER can: actionable rows sort to the front. The names are chosen so that
# alphabetical order is the OPPOSITE of the expected order, otherwise this check
# would pass on the tie-break alone and prove nothing.
w6="$j5/ordering"; mkdir -p "$w6/A/notes" "$w6/dest/notes"
printf 'identical bytes\n' > "$w6/A/notes/aaa-drop-safe.md"
printf 'identical bytes\n' > "$w6/dest/notes/aaa-drop-safe.md"
chmod 0644 "$w6/A/notes/aaa-drop-safe.md" "$w6/dest/notes/aaa-drop-safe.md"
printf 'identical bytes\n' > "$w6/A/notes/zzz-mode-only.md"
printf 'identical bytes\n' > "$w6/dest/notes/zzz-mode-only.md"
chmod 0755 "$w6/A/notes/zzz-mode-only.md"; chmod 0644 "$w6/dest/notes/zzz-mode-only.md"
touch -t 202606010000 "$w6/A/notes"/*.md "$w6/dest/notes"/*.md
_first="$(SUTANDO_MIGRATE_SRC_A="$w6/A" SUTANDO_MIGRATE_DEST="$w6/dest" \
    bash scripts/sutando-migrate.sh scan --json --source A 2>/dev/null \
    | python3 -c 'import sys,json;d=sys.stdin.read();j=json.loads(d[d.index("{"):]);print(j["notable_collisions"][0]["rel"])')"
check "a mode-only difference still outranks a drop-safe row (stays ACTIONABLE)" \
      "$_first" "notes/zzz-mode-only.md"
rm -rf "$j5"

if [ "$fails" -gt 0 ]; then echo "$fails FAILURE(S)"; exit 1; fi
echo "ALL PASS"
