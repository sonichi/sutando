#!/usr/bin/env bash
# Regression: the SHIPPED default carrier must NOT back up rendered episode
# intermediates. `notes/` is an included subtree, so `notes/generated/` — where
# the WIRE renderer ships mp4 bundles — was carried by inheritance.
#
# Measured on the live vault when this was added: 951 files / 2.28 GiB tracked
# under notes/generated, of which 2.15 GiB was mp4 across 292 files, committed
# AND pushed (0 unpushed commits) to a private remote whose .git had reached
# 3.5 GB. `git check-ignore` matched no rule, and a probe file under the
# canonical workspace showed as `??` — i.e. visible to the carrier's `add -A`.
#
# These are regenerable binary derivatives of a spec that IS carried, so the
# vault gains nothing by holding them and pays for it in clone/push cost.
#
# The negative half matters as much as the positive: `notes/` itself must stay
# carried. A carve-out that widened to `notes/` would silently drop the entire
# episode-spec + notes corpus out of the backup while sync kept reporting a
# successful push — the same failure shape documented for `vault.sync.include`
# in scripts/sync-workspace.sh (include REPLACES wholesale).
#
# Note this does NOT untrack files already committed: gitignore rules do not
# affect tracked paths. It stops accumulation going forward; purging existing
# history is a separate, owner-authorized operation.

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

# ---------------------------------------------------------------------------
# 1. The shipped default list itself.
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# 2. Drive the SHIPPED config through the SHIPPED composer.
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# 3. The behaviour the rules are for: what git would actually track.
#    This half survives a refactor of the rule syntax.
# ---------------------------------------------------------------------------
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

echo
echo "Total: $((pass + fail)) — pass: $pass, fail: $fail"
[ "$fail" -eq 0 ]
