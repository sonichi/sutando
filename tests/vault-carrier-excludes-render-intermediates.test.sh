#!/usr/bin/env bash
# The shipped carrier must exclude `notes/generated/` render intermediates while
# still carrying `notes/` itself, and untracking them must not read as deletion.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
TEST_ROOT="$(mktemp -d -t vault-render-exclude-test.XXXXXX)"
trap 'rm -rf "$TEST_ROOT"' EXIT

pass=0
fail=0
check() {
    local description="$1"
    shift
    if "$@" >/dev/null 2>&1; then
        echo "OK: $description"
        pass=$((pass + 1))
    else
        echo "FAIL: $description"
        fail=$((fail + 1))
    fi
}
refute() {
    local description="$1"
    shift
    if "$@" >/dev/null 2>&1; then
        echo "FAIL: $description"
        fail=$((fail + 1))
    else
        echo "OK: $description"
        pass=$((pass + 1))
    fi
}

# 1. The shipped default list itself.
check "shipped sutando.config.json carves out notes/generated/" \
    python3 -c "
import json, pathlib, sys
exc = json.loads(pathlib.Path('$REPO/sutando.config.json').read_text())['vault']['sync']['exclude']
sys.exit(0 if 'notes/generated/' in exc else 1)
"

# scripts/sync-workspace.sh:1529 already excludes generated/ AND media/ when it
# migrates notes; the carrier must not carve out only half that pair.
check "...and notes/media/, the other half of the migrator's pair" \
    python3 -c "
import json, pathlib, sys
exc = json.loads(pathlib.Path('$REPO/sutando.config.json').read_text())['vault']['sync']['exclude']
sys.exit(0 if 'notes/media/' in exc else 1)
"

check "the migrator this mirrors still excludes both (drift guard)" \
    python3 -c "
import pathlib, re, sys
src = pathlib.Path('$REPO/scripts/sync-workspace.sh').read_text()
ok = re.search(r\"--exclude='generated/'\", src) and re.search(r\"--exclude='media/'\", src)
sys.exit(0 if ok else 1)
"

check "...and still INCLUDES notes/ (the carve-out must not swallow the parent)" \
    python3 -c "
import json, pathlib, sys
inc = json.loads(pathlib.Path('$REPO/sutando.config.json').read_text())['vault']['sync']['include']
sys.exit(0 if 'notes/' in inc else 1)
"

refute "...and does NOT exclude notes/ wholesale (would drop the spec corpus)" \
    python3 -c "
import json, pathlib, sys
exc = json.loads(pathlib.Path('$REPO/sutando.config.json').read_text())['vault']['sync']['exclude']
bad = [p for p in exc if p.rstrip('/*') == 'notes']
sys.exit(0 if bad else 1)
"

# 2. Drive the SHIPPED config through the SHIPPED composer.
FIXTURE_ROOT="$TEST_ROOT/render-exclude"
FIXTURE_REPO="$FIXTURE_ROOT/repo"
FIXTURE_WS="$FIXTURE_ROOT/workspace"
FIXTURE_VAULT="$FIXTURE_ROOT/vault.git"
mkdir -p "$FIXTURE_REPO/scripts" "$FIXTURE_REPO/src"
cp "$REPO/scripts/sync-workspace.sh" "$FIXTURE_REPO/scripts/"
cp "$REPO/scripts/sutando-config.sh" "$FIXTURE_REPO/scripts/"
cp "$REPO/scripts/python-binary.sh" "$FIXTURE_REPO/scripts/"
cp "$REPO/src/sutando_config.py" "$FIXTURE_REPO/src/"
cp "$REPO/sutando.config.json" "$FIXTURE_REPO/sutando.config.json"
touch "$FIXTURE_REPO/CLAUDE.md"
mkdir -p "$FIXTURE_REPO/skills"
git init -q "$FIXTURE_REPO"
git init -q --bare "$FIXTURE_VAULT"

# A render bundle beside an ordinary note and an episode spec — the three
# things whose fates must differ.
mkdir -p "$FIXTURE_WS/notes/generated/ep999-bundle" \
         "$FIXTURE_WS/notes/media" \
         "$FIXTURE_WS/notes/sutando-wire/episode-specs" \
         "$FIXTURE_WS/hosts/render-host"
printf '\x00\x00' > "$FIXTURE_WS/notes/generated/ep999-bundle/ep999-v1.mp4"
printf '\x00\x00' > "$FIXTURE_WS/notes/media/source-clip.mov"
printf '{"v":1}\n' > "$FIXTURE_WS/notes/generated/ep999-bundle/provenance.json"
printf 'a note\n'  > "$FIXTURE_WS/notes/a.md"
printf 'title: t\n' > "$FIXTURE_WS/notes/sutando-wire/episode-specs/ep999.yaml"

cat > "$FIXTURE_REPO/sutando.config.local.json" <<JSON
{"workspace": {"path": "$FIXTURE_WS"}}
JSON

SYNC="$FIXTURE_REPO/scripts/sync-workspace.sh"
SYNC_ENV=(
    SUTANDO_HOST_OVERRIDE=render-host
    SUTANDO_WS_ID_OVERRIDE=render1
    SUTANDO_SYNC_LOCK_DIR="$FIXTURE_ROOT/sync.lock"
    GIT_AUTHOR_NAME="Render Exclude Test"
    GIT_AUTHOR_EMAIL="render-exclude@example.com"
    GIT_COMMITTER_NAME="Render Exclude Test"
    GIT_COMMITTER_EMAIL="render-exclude@example.com"
)
env "${SYNC_ENV[@]}" bash "$SYNC" \
    --vault-url "$FIXTURE_VAULT" --force-gitignore --init \
    >/dev/null 2>&1 || true

RULES="$FIXTURE_WS/.git/info/exclude"

check "generated rules still un-ignore notes/ (carrier intact)" \
    grep -qFx '!notes/**' "$RULES"
check "generated rules carve out notes/generated/" \
    grep -qE '^notes/generated/' "$RULES"

# 3. The behaviour the rules are for: what git would actually track.
#    This half survives a refactor of the rule syntax.
git -C "$FIXTURE_WS" add -A >/dev/null 2>&1 || true

refute "git does NOT track the rendered mp4 (the 2.15 GiB class)" \
    git -C "$FIXTURE_WS" ls-files --error-unmatch notes/generated/ep999-bundle/ep999-v1.mp4
refute "git does NOT track source media either (the 0.79 GiB class)" \
    git -C "$FIXTURE_WS" ls-files --error-unmatch notes/media/source-clip.mov
refute "git does NOT track the render bundle's sidecar json either" \
    git -C "$FIXTURE_WS" ls-files --error-unmatch notes/generated/ep999-bundle/provenance.json
check "git DOES track an ordinary note (carrier not broken)" \
    git -C "$FIXTURE_WS" ls-files --error-unmatch notes/a.md
check "git DOES track the episode SPEC (the input worth backing up)" \
    git -C "$FIXTURE_WS" ls-files --error-unmatch notes/sutando-wire/episode-specs/ep999.yaml

# 4. EXISTING installs: untracking >50 already-carried files stages >50 D's, which
#    met the mass-deletion tripwire. Disk presence separates untrack from deletion.

MIG="$TEST_ROOT/migration"
mkdir -p "$MIG" && git -C "$MIG" init -q .
git -C "$MIG" config user.email t@t && git -C "$MIG" config user.name t
mkdir -p "$MIG/notes/generated/ep999-bundle" "$MIG/notes/real"
# 51 already-carried render intermediates — one more than the default tripwire.
for i in $(seq 1 51); do echo "frame $i" > "$MIG/notes/generated/ep999-bundle/f$i.json"; done
for i in $(seq 1 51); do echo "note $i" > "$MIG/notes/real/n$i.md"; done
git -C "$MIG" add -A >/dev/null 2>&1
git -C "$MIG" commit -qm "existing install: generated/ already tracked"

# Mirrors the shipped classifier: ignored AND still on disk. Counting ignore
# matches alone excuses a real deletion whose path happens to be excluded.
count_policy() {
    local n=0 _p
    while IFS= read -r -d '' _p; do
        if [ -e "$1/$_p" ] || [ -L "$1/$_p" ]; then n=$(( n + 1 )); fi
    done < <(git -C "$1" diff -M --cached --name-only --diff-filter=D -z \
        | git -C "$1" check-ignore -z --stdin --no-index 2>/dev/null || true)
    echo "$n"
}
count_staged_d() {
    git -C "$1" diff -M --cached --name-only --diff-filter=D | wc -l | tr -d ' '
}

# The migration: rules now exclude generated/, the tick untracks the 51.
mkdir -p "$MIG/.git/info" && printf 'notes/generated/\n' > "$MIG/.git/info/exclude"
while IFS= read -r -d '' _p; do git -C "$MIG" rm -q --cached -- "$_p" 2>/dev/null || true; done \
    < <(git -C "$MIG" ls-files -z | git -C "$MIG" check-ignore -z --stdin --no-index 2>/dev/null)

check "migration stages more D's than the tripwire allows (the repro)" \
    test "$(count_staged_d "$MIG")" -gt 50
check "...and ALL of them are classified as carrier-policy untracks" \
    test "$(count_policy "$MIG")" -eq "$(count_staged_d "$MIG")"
check "...so the tripwire's real-deletion count is 0 and the push proceeds" \
    test "$(( $(count_staged_d "$MIG") - $(count_policy "$MIG") ))" -eq 0

# THE CONTROL THAT MATTERS: same path, files actually gone from disk. An ignore
# match alone cannot tell this from the migration above — only disk presence can.
SAME="$TEST_ROOT/samepath"
mkdir -p "$SAME" && git -C "$SAME" init -q .
git -C "$SAME" config user.email t@t && git -C "$SAME" config user.name t
mkdir -p "$SAME/notes/generated/ep999-bundle"
for i in $(seq 1 51); do echo "frame $i" > "$SAME/notes/generated/ep999-bundle/f$i.json"; done
git -C "$SAME" add -A >/dev/null 2>&1
git -C "$SAME" commit -qm "existing install: generated/ already tracked"
mkdir -p "$SAME/.git/info" && printf 'notes/generated/\n' > "$SAME/.git/info/exclude"
rm -rf "$SAME/notes/generated"
git -C "$SAME" add -A >/dev/null 2>&1

check "a real deletion UNDER the excluded path stages >50 D's" \
    test "$(count_staged_d "$SAME")" -gt 50
check "...and NONE are excused as policy (the files are gone from disk)" \
    test "$(count_policy "$SAME")" -eq 0
check "...so the tripwire still sees >50 real deletions and refuses" \
    test "$(( $(count_staged_d "$SAME") - $(count_policy "$SAME") ))" -gt 50
# Proves the pair is not vacuous: identical paths and rules, opposite verdicts,
# and the ignore-match-only formula scores this same case 51 -> 0 real deletions.
check "the ignore-match-only formula WOULD have excused all 51 (the fixed bug)" \
    test "$(git -C "$SAME" diff -M --cached --name-only --diff-filter=D -z \
        | git -C "$SAME" check-ignore -z --stdin --no-index 2>/dev/null \
        | tr -dc '\0' | wc -c | tr -d ' ')" -eq 51

git -C "$MIG" reset -q
DEL="$TEST_ROOT/realdel"
mkdir -p "$DEL" && git -C "$DEL" init -q .
git -C "$DEL" config user.email t@t && git -C "$DEL" config user.name t
mkdir -p "$DEL/notes/real"
for i in $(seq 1 51); do echo "note $i" > "$DEL/notes/real/n$i.md"; done
git -C "$DEL" add -A >/dev/null 2>&1 && git -C "$DEL" commit -qm init
mkdir -p "$DEL/.git/info" && printf 'notes/generated/\n' > "$DEL/.git/info/exclude"
git -C "$DEL" rm -q --cached -r -- notes/real >/dev/null 2>&1

check "a REAL 51-file deletion still stages >50 D's" \
    test "$(count_staged_d "$DEL")" -gt 50
check "...and NONE are excused as policy (rules do not cover notes/real/)" \
    test "$(count_policy "$DEL")" -eq 0
check "...so the tripwire still sees >50 real deletions and refuses" \
    test "$(( $(count_staged_d "$DEL") - $(count_policy "$DEL") ))" -gt 50

# Mixed: one genuine deletion alongside the policy untracks must survive counting.
git -C "$MIG" reset -q
while IFS= read -r -d '' _p; do git -C "$MIG" rm -q --cached -- "$_p" 2>/dev/null || true; done \
    < <(git -C "$MIG" ls-files -z | git -C "$MIG" check-ignore -z --stdin --no-index 2>/dev/null)
git -C "$MIG" rm -q --cached -- notes/real/n1.md
check "a real deletion mixed with policy untracks is still counted (exactly 1)" \
    test "$(( $(count_staged_d "$MIG") - $(count_policy "$MIG") ))" -eq 1

# The cases above compute the rule locally, so they cannot see whether the shipped
# tripwire uses it. Comments are stripped so one cannot satisfy a guard.
SYNC_CODE="$(sed 's/#.*//' "$REPO/scripts/sync-workspace.sh")"
CLASSIFIER="$(printf '%s\n' "$SYNC_CODE" | sed -n '/untracked_by_policy=0/,/deleted=\$(( staged_d/p')"

check "the classifier region was located (guard is not vacuous)" \
    test -n "$CLASSIFIER"
check "the shipped tripwire subtracts policy untracks from its count" \
    grep -qF 'deleted=$(( staged_d - untracked_by_policy ))' <<< "$SYNC_CODE"
check "...and it does not compare the RAW staged-D count against max_delete" \
    bash -c '! grep -qE "^\s*deleted=\\\$staged_d\s*\$" <<< "$1"' _ "$SYNC_CODE"
# Disk presence is the whole fix: without it an excluded path's real deletion is
# excused. --no-index is required because these paths just left the index.
check "the classifier gates on the path still existing on disk" \
    bash -c 'grep -qF -- "[ -e \"\$_p\" ] || [ -L \"\$_p\" ]" <<< "$1"' _ "$CLASSIFIER"
check "the classifier's check-ignore keeps --no-index" \
    grep -qF -- "--no-index" <<< "$CLASSIFIER"

# 5. The REAL upgrade path. A hand-written exclude file skips the refusal gate
#    that actually blocks existing installs, so drive the shipped function.

SYNC_SH="$REPO/scripts/sync-workspace.sh"
DRIVER="$TEST_ROOT/drive-generate-exclude.sh"
# Only the shipped function definitions are eval'd; the subcommand dispatch at the
# bottom of the script is not a function, so it never runs here.
cat > "$DRIVER" <<'DRIVE'
set -uo pipefail
SYNC="$1"; SCRIPT_PARENT="$2"; WORKSPACE_DIR="$3"
DRY_RUN=0; FORCE_GITIGNORE=0
eval "$(awk '/^[A-Za-z_][A-Za-z0-9_]*\(\) \{/,/^\}$/' "$SYNC")"
log() { :; }
color_warn() { :; }
_host() { echo "TestHost"; }
# The REAL _is_literal_host_label is used: stubbing it to 1 made the widening a
# pass-through, so the combined-migration case could never pass in this harness.
generate_exclude
DRIVE

compose_rules() {
    SCRIPT_PARENT="$REPO" bash -c 'set -uo pipefail
        eval "$(awk "/^[A-Za-z_][A-Za-z0-9_]*\(\) \{/,/^\}\$/" "$1")"
        log() { :; }; color_warn() { :; }
        _compose_exclude_content' _ "$SYNC_SH"
}
# Builds a workspace whose exclude file is the CURRENT generated content minus
# the lines named, i.e. what an older generated install actually carries.
seed_older_install() {
    local dir="$1"; shift
    local -a strip=()
    local l
    # A dir path emits BOTH `p/` and `p/**`; stripping only the config value leaves
    # the `**` rule behind, and the recognizer then has less to add than reality.
    for l in "$@"; do
        strip+=(-e "$l")
        [[ "$l" == */ ]] && strip+=(-e "${l}**")
    done
    rm -rf "$dir"; mkdir -p "$dir/.git/info"
    if [ "${#strip[@]}" -gt 0 ]; then
        compose_rules | grep -vxF "${strip[@]}" > "$dir/.git/info/exclude"
    else
        compose_rules > "$dir/.git/info/exclude"
    fi
}
upgrade_rc() {
    bash "$DRIVER" "$SYNC_SH" "$REPO" "$1" >/dev/null 2>&1
    echo $?
}
carveouts_in() { grep -cE '^notes/(generated|media)/(\*\*)?$' "$1/.git/info/exclude" || true; }

check "the composer emits both shipped carve-outs (fixture is representative)" \
    test "$(compose_rules | grep -cE '^notes/(generated|media)/$')" -eq 2

UPG="$TEST_ROOT/upgrade-generated"
seed_older_install "$UPG" 'notes/generated/' 'notes/media/'
check "an older GENERATED install starts without the carve-outs" \
    test "$(carveouts_in "$UPG")" -eq 0
# The fixture must lack EVERY emitted carve-out rule (4 = 2 dirs x {p/, p/**}).
# With any left behind, the refresh has less to add than a real install does.
check "...and lacks all FOUR emitted carve-out rules, not just the 2 config values" \
    test "$(grep -cE '^notes/(generated|media)/(\*\*)?$' "$UPG/.git/info/exclude")" -eq 0
check "...while the CURRENT generated content carries all four" \
    test "$(compose_rules | grep -cE '^notes/(generated|media)/(\*\*)?$')" -eq 4
check "...the real generate_exclude accepts the refresh (rc=0)" \
    test "$(upgrade_rc "$UPG")" -eq 0
check "...and both carve-outs are now present (migration actually reaches it)" \
    test "$(carveouts_in "$UPG")" -eq 4

# An OWNED BUILT-IN deny (not a vault.sync.exclude carve-out) must migrate too, or
# an upgraded workspace stages the crash temp the rule exists to keep out.
BI_RULE='hosts/*/build_log.md.snap.??????'
builtin_in() { grep -cxF "$BI_RULE" "$1/.git/info/exclude" || true; }

check "the composer emits the owned built-in deny (fixture is representative)" \
    test "$(compose_rules | grep -cxF "$BI_RULE")" -eq 1

UPG_BI="$TEST_ROOT/upgrade-builtin-deny"
seed_older_install "$UPG_BI" "$BI_RULE"
check "an older GENERATED install starts without the owned built-in deny" \
    test "$(builtin_in "$UPG_BI")" -eq 0
check "...the real generate_exclude ACCEPTS the refresh (rc=0)" \
    test "$(upgrade_rc "$UPG_BI")" -eq 0
check "...and the built-in deny is now present (migration actually reaches it)" \
    test "$(builtin_in "$UPG_BI")" -eq 1

# The rule landing is not the point; NOT STAGING the temp is. Drive the real
# generate_exclude in a real repo, then the real `git add -A`.
STG="$TEST_ROOT/upgrade-builtin-staging"
seed_older_install "$STG" "$BI_RULE"
git init -q "$STG"
mkdir -p "$STG/hosts/H"
printf 'log\n'  > "$STG/hosts/H/build_log.md"
printf 'temp\n' > "$STG/hosts/H/build_log.md.snap.AB12cd"
check "before migration the crash temp IS staged (the defect being fixed)" \
    test "$( (cd "$STG" && git add -A >/dev/null 2>&1; git -C "$STG" ls-files --cached hosts/H/build_log.md.snap.AB12cd | wc -l) )" -eq 1
git -C "$STG" rm -q --cached -r . >/dev/null 2>&1 || true
# Bare call: under `set -e` a refusing generate_exclude would abort the suite and
# silently skip the two assertions below — the ones that actually discriminate.
upgrade_rc "$STG" >/dev/null || true
check "after migration a real git add -A does NOT stage the crash temp" \
    test "$( (cd "$STG" && git add -A >/dev/null 2>&1; git -C "$STG" ls-files --cached hosts/H/build_log.md.snap.AB12cd | wc -l) )" -eq 0
check "control: ...while the real build_log beside it IS still staged" \
    test "$(git -C "$STG" ls-files --cached hosts/H/build_log.md | wc -l)" -eq 1

# Control: widening to built-ins must not weaken the operator guard.
UPG_BI_OP="$TEST_ROOT/upgrade-builtin-operator-edited"
seed_older_install "$UPG_BI_OP" "$BI_RULE"
echo '!operator/keeps/this' >> "$UPG_BI_OP/.git/info/exclude"
check "control: an operator-edited file is STILL refused even for a built-in (rc=1)" \
    test "$(upgrade_rc "$UPG_BI_OP")" -eq 1
check "control: ...and the built-in was not force-added behind the refusal" \
    test "$(builtin_in "$UPG_BI_OP")" -eq 0

UPG_OP="$TEST_ROOT/upgrade-operator-edited"
seed_older_install "$UPG_OP" 'notes/generated/' 'notes/media/'
echo '!my/operator/rule' >> "$UPG_OP/.git/info/exclude"
check "an operator-edited exclude is still REFUSED (rc=1)" \
    test "$(upgrade_rc "$UPG_OP")" -eq 1
check "...and the operator's own rule survives untouched" \
    grep -qxF '!my/operator/rule' "$UPG_OP/.git/info/exclude"
check "...and the carve-outs were NOT force-added behind the refusal" \
    test "$(carveouts_in "$UPG_OP")" -eq 0

UPG_DROP="$TEST_ROOT/upgrade-would-drop"
seed_older_install "$UPG_DROP" 'notes/generated/'
echo 'legacy-only-rule/' >> "$UPG_DROP/.git/info/exclude"
check "a refresh that would DROP an existing rule is refused (rc=1)" \
    test "$(upgrade_rc "$UPG_DROP")" -eq 1
check "...and the rule that would have been dropped is still there" \
    grep -qxF 'legacy-only-rule/' "$UPG_DROP/.git/info/exclude"

# Comments are inert in gitignore, so header drift alone must not block a refresh.
UPG_CMT="$TEST_ROOT/upgrade-comment-drift"
seed_older_install "$UPG_CMT" 'notes/generated/' 'notes/media/'
printf '# an older header line that no longer ships\n' >> "$UPG_CMT/.git/info/exclude"
check "comment-only drift does not block the refresh (rc=0)" \
    test "$(upgrade_rc "$UPG_CMT")" -eq 0
check "...and the carve-outs landed despite the stale comment" \
    test "$(carveouts_in "$UPG_CMT")" -eq 4
check "...and the operator's comment SURVIVES the refresh (preserved, not dropped)" \
    grep -qxF '# an older header line that no longer ships' "$UPG_CMT/.git/info/exclude"

# keweichen's acceptance case on #3198: the owned deny must LAND while an
# operator comment beside it SURVIVES. Refusing the refresh preserved the
# comment but left the deny out, which is how the crash temp became stageable.
UPG_BOTH="$TEST_ROOT/upgrade-deny-and-comment"
seed_older_install "$UPG_BOTH" 'notes/generated/' 'notes/media/'
grep -vxF "$BI_RULE" "$UPG_BOTH/.git/info/exclude" > "$UPG_BOTH/.git/info/exclude.t" &&
    mv "$UPG_BOTH/.git/info/exclude.t" "$UPG_BOTH/.git/info/exclude"
printf '# why we keep notes/raw: it is my scratch area\n' >> "$UPG_BOTH/.git/info/exclude"
check "deny+comment: the refresh is allowed (rc=0)" \
    test "$(upgrade_rc "$UPG_BOTH")" -eq 0
check "...the owned deny LANDS" \
    test "$(builtin_in "$UPG_BOTH")" -eq 1
check "...and the operator's comment survives beside it" \
    grep -qxF '# why we keep notes/raw: it is my scratch area' "$UPG_BOTH/.git/info/exclude"

check "the refusal chain consults the carve-out recognizer" \
    grep -qF '_is_safe_carveout_addition "$exclude_path" "$tmp_path"' <<< "$SYNC_CODE"

# A generated install can need BOTH safe migrations. They were independent whole-file
# comparisons, so each recognizer refused a file that needed the other as well.
host_downgrade() {
    sed 's#^!hosts/\*/$#!hosts/TestHost/#; s#^!hosts/\*/\*\*$#!hosts/TestHost/**#'
}
seed_combined() {
    local dir="$1"
    rm -rf "$dir"; mkdir -p "$dir/.git/info"
    compose_rules | host_downgrade \
        | grep -vxF -e 'notes/generated/' -e 'notes/generated/**' \
                    -e 'notes/media/' -e 'notes/media/**' > "$dir/.git/info/exclude"
}
host_rules_in() { grep -c '^!hosts/\*/' "$1/.git/info/exclude" || true; }

CMB="$TEST_ROOT/upgrade-combined"
seed_combined "$CMB"
check "the combined fixture starts on the legacy per-host scope" \
    test "$(grep -c '^!hosts/TestHost/' "$CMB/.git/info/exclude")" -eq 2
check "...and with none of the four carve-out rules" \
    test "$(carveouts_in "$CMB")" -eq 0
check "...the real generate_exclude accepts BOTH migrations at once (rc=0)" \
    test "$(upgrade_rc "$CMB")" -eq 0
check "...the host scope is widened to hosts/*/" \
    test "$(host_rules_in "$CMB")" -eq 2
check "...and all four carve-out rules landed" \
    test "$(carveouts_in "$CMB")" -eq 4

CMB_OP="$TEST_ROOT/upgrade-combined-operator"
seed_combined "$CMB_OP"
echo '!my/operator/rule' >> "$CMB_OP/.git/info/exclude"
check "an operator rule ON TOP of the combined shape is still REFUSED (rc=1)" \
    test "$(upgrade_rc "$CMB_OP")" -eq 1
check "...and that operator rule survives" \
    grep -qxF '!my/operator/rule' "$CMB_OP/.git/info/exclude"

# One widening implementation, consumed by both recognizers.
check "the widening is a shared helper, not duplicated awk" \
    test "$(grep -c '_widen_legacy_host_scope' <<< "$SYNC_CODE")" -ge 3
check "the carve-out recognizer runs on the WIDENED content" \
    grep -qF '_widen_legacy_host_scope "$existing" > "$widened"' <<< "$SYNC_CODE"
check "the widened temp file is removed on every return path" \
    test "$(grep -c 'rm -f "$widened"' <<< "$SYNC_CODE")" -ge 2

echo
echo "Total: $((pass + fail)) — pass: $pass, fail: $fail"
[ "$fail" -eq 0 ]
