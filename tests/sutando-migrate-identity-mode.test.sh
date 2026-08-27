#!/usr/bin/env bash
# Identity = bytes AND mode, uniformly (kewei review on #3418, blockers 2+3).
# Three controls the review asked for:
#   1. same-bytes/different-mode commit path — the 0755 source must NOT be
#      dropped against a 0644 dest (the exec bit used to vanish unrecoverably);
#   2. 51-collision summary — 50 byte-identical mtime-differing entries must
#      not push the one divergent entry past the render cap;
#   3. unreadable-only — an unverified collision is NOT a genuine conflict.
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
sed -n '/^mode_of() {/,/^}/p; /^identity_match() {/,/^}/p; /^sha_match() {/,/^}/p' scripts/sutando-migrate.sh > "$helpers"
# shellcheck disable=SC1090
. "$helpers"
# A failed extraction must fail HERE, loudly — command-not-found inside a
# $(... && echo yes || echo no) capture reads as a clean "no" and false-passes
# exactly the checks that expect "no" (measured on this test's first run).
type identity_match >/dev/null 2>&1 || { echo "FAIL  helper extraction produced no identity_match"; exit 1; }
check "same bytes + same mode IS identical" "$(identity_match "$tmp/a.sh" "$tmp/c.sh" && echo yes || echo no)" "yes"
check "same bytes + different mode is NOT identical (exec bit must survive)" \
  "$(identity_match "$tmp/a.sh" "$tmp/b.sh" && echo yes || echo no)" "no"
check "different bytes never identical whatever the mode" \
  "$(printf 'other\n' > "$tmp/d.sh"; chmod 0755 "$tmp/d.sh"; identity_match "$tmp/a.sh" "$tmp/d.sh" && echo yes || echo no)" "no"
# reviewer control (exact-head finding at beeec419): a failing mode probe
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
# The summary python lives inline; exercise its logic via a faithful driver
# that imports the same rules by regenerating verdicts/sort from fixtures.
summary_out="$(python3 - <<'PYEOF'
import json, os, tempfile, hashlib, subprocess, re, sys
# Extract the inline summary python from the shipped script and run it against
# a synthetic collision index: 50 identical(mtime-diff) + 1 divergent + the
# unreadable-only variant.
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

if [ "$fails" -gt 0 ]; then echo "$fails FAILURE(S)"; exit 1; fi
echo "ALL PASS"
