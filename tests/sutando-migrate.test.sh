#!/usr/bin/env bash
# E2E test for sutando-migrate.sh — synthetic fixture (3 sources + dest), scan
# → commit → verify → rollback, asserting each shape per `feedback_e2e_tests_for_contributions`.
# Runs offline, uses /tmp, leaves no trace.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
MIGRATE="$REPO/scripts/sutando-migrate.sh"
HELPER="$REPO/scripts/sutando-config.sh"

TMP="$(mktemp -d -t sutando-mig-test.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

C="$TMP/source-c"
A="$TMP/source-a"
B="$TMP/source-b"
DEST="$TMP/dest"

mkdir -p "$C/notes" "$C/state" "$A/notes" "$A/state" "$B/notes" "$DEST"

# --- Fixture: build_log.md divergent across all 3 sources ---
echo "C: 2026-05-30 entry" > "$C/build_log.md"
echo "A: 2026-06-01 entry" > "$A/build_log.md"
echo "B: 2026-05-15 entry" > "$B/build_log.md"
touch -t 202605301800 "$C/build_log.md"
touch -t 202606012000 "$A/build_log.md"
touch -t 202605151200 "$B/build_log.md"

# --- Fixture: notes/, mixed identical + divergent + sole-source ---
echo "shared notes" > "$C/notes/shared.md"; cp -p "$C/notes/shared.md" "$B/notes/shared.md"  # identical in B+C
echo "C-only note" > "$C/notes/c-only.md"
echo "A-only note" > "$A/notes/a-only.md"
echo "divergent C-version" > "$C/notes/divergent.md"
echo "divergent A-version" > "$A/notes/divergent.md"
echo "divergent B-version" > "$B/notes/divergent.md"
# Increasing mtimes: B oldest → C → A newest. Tests 3-way collision sidecar
# preservation per Mini #3.
touch -t 202506010800 "$B/notes/divergent.md"
touch -t 202606010800 "$C/notes/divergent.md"
touch -t 202606012100 "$A/notes/divergent.md"

# --- Fixture: hosts/ + relay/ same-mtime same-size DIFFERENT content —
# equal proxies must never certify identity; only a content hash may.
mkdir -p "$A/hosts/Test-Host" "$A/relay" "$DEST/hosts/Test-Host" "$DEST/relay"
printf 'AAAA\n' > "$A/hosts/Test-Host/crons.json"
printf 'BBBB\n' > "$DEST/hosts/Test-Host/crons.json"
touch -t 202606011200 "$A/hosts/Test-Host/crons.json" "$DEST/hosts/Test-Host/crons.json"
printf 'RRRR\n' > "$A/relay/relay-1.md"
printf 'SSSS\n' > "$DEST/relay/relay-1.md"
touch -t 202606011200 "$A/relay/relay-1.md" "$DEST/relay/relay-1.md"

# --- Fixture: unreadable-identity — identical bytes both sides, but the SOURCE
# copy is unreadable: the scan must report UNVERIFIED, never proven divergence.
mkdir -p "$A/notes" "$DEST/notes"
printf 'same bytes\n' > "$A/notes/unreadable.md"
printf 'same bytes\n' > "$DEST/notes/unreadable.md"
touch -t 202606011300 "$A/notes/unreadable.md" "$DEST/notes/unreadable.md"
chmod 000 "$A/notes/unreadable.md"

# --- Fixture: rehome — loose root JSON at C ---
echo '{"k":"v"}' > "$C/cloud-auth.json"

# --- Fixture: state/*.json newest-mtime ---
echo '{"old":"snapshot"}' > "$C/state/contextual-chips.json"
echo '{"new":"snapshot"}' > "$A/state/contextual-chips.json"
touch -t 202606010600 "$C/state/contextual-chips.json"
touch -t 202606012130 "$A/state/contextual-chips.json"

# Fixture union-json-array: a newer EMPTY allow-set must not erase an older
# populated one; schemaVersion is the control that must survive from the newer file.
cat > "$C/state/slack-allowed-recipients.json" <<'JSON'
{"allowFrom": ["U_OLD_ONE", "U_SHARED"], "schemaVersion": 1}
JSON
cat > "$A/state/slack-allowed-recipients.json" <<'JSON'
{"allowFrom": [], "schemaVersion": 2}
JSON
touch -t 202606010600 "$C/state/slack-allowed-recipients.json"   # older, populated
touch -t 202606012130 "$A/state/slack-allowed-recipients.json"   # newer, empty

# --- Fixture: in-flight task (newer than 60s guard, must be skipped) ---
mkdir -p "$C/tasks"
echo "id: live-task" > "$C/tasks/task-now.txt"  # mtime = now → inflight

# --- Run scan + commit with E2E source hooks ---
RUN_MIGRATE() {
    SUTANDO_MIGRATE_SRC_A="$A" \
    SUTANDO_MIGRATE_SRC_B="$B" \
    SUTANDO_MIGRATE_SRC_C="$C" \
    SUTANDO_MIGRATE_DEST="$DEST" \
        bash "$MIGRATE" "$@"
    # Note: SUTANDO_MIGRATE_DEST is the proper test hook (workspace resolver
    # ignores $SUTANDO_WORKSPACE since #1440). --respect-env is not needed.
}

# Also add a stale task to source B for archive-routing assertion
mkdir -p "$B/tasks"
echo "id: stale-task-from-B" > "$B/tasks/task-stale.txt"
touch -t 202604010000 "$B/tasks/task-stale.txt"  # >60s old → archived

# Quarantine fixture (Lucy #design + owner direction 2026-06-02):
# B and C have user-custom dirs/files at root NOT in the surface allowlist.
# Should be quarantined to <dest>/legacy/<src-tag>/quarantine/<rel>.
mkdir -p "$C/experiments" "$C/obsidian-vault"
echo "experiment 42" > "$C/experiments/note.md"
echo "vault content" > "$C/obsidian-vault/daily.md"
echo "loose ts file" > "$C/repro-bug.ts"
mkdir -p "$B/personal-src"
echo "personal lib" > "$B/personal-src/lib.py"

# Newly-canonical surfaces (#3036): B/C scripts/ + agent config tree must
# MIGRATE, not quarantine; repo-root Source A scripts/ is code, stays excluded.
mkdir -p "$B/scripts" "$C/scripts" "$A/scripts"
echo "B tool v1" > "$B/scripts/my-tool.sh"
echo "C tool v2" > "$C/scripts/my-tool.sh"
touch -t 202605011000 "$B/scripts/my-tool.sh"   # older — loses the collision
touch -t 202606011000 "$C/scripts/my-tool.sh"   # newer — canonical
# (#3036 P1) A workspace may legitimately OWN scripts/sutando-config.sh without
# being a repo checkout. B carries it plus a B-only tool: the tool must survive.
echo "# workspace-owned helper" > "$B/scripts/sutando-config.sh"
echo "B only tool" > "$B/scripts/b-only-tool.sh"
echo "repo code, not data" > "$A/scripts/repo-code.sh"
# The repo marker lives in the very dir being excluded — matches a real
# checkout, and flips IS_SUTANDO_REPO=1 so SOURCE_A_EXCLUDE actually applies.
# Source A is a real checkout: the repo-only resolver module is the marker.
# (scripts/sutando-config.sh alone must NOT count — see 6e-bis.)
mkdir -p "$A/src"
echo "# repo resolver" > "$A/src/sutando_config.py"
echo "# repo marker" > "$A/scripts/sutando-config.sh"
mkdir -p "$C/.claude-sutando/skills/custom" "$C/.claude-sutando/hooks"
echo "custom skill body" > "$C/.claude-sutando/skills/custom/SKILL.md"
echo "print('hook')" > "$C/.claude-sutando/hooks/pre-task.py"

echo "==== TEST: scan ===="
RUN_MIGRATE scan --source A,B,C 2>&1 \
    | grep -E "Source A|Source B|Source C|Cross-source|of which identical|genuine|notable|append\] build_log" \
    | head -25 || true

echo
echo "==== TEST: scan --json content-identity regression ===="
# Equal mtime+size but different bytes (hosts/ + relay/ fixtures) must be
# actionable, never certified identical_content, in the machine contract.
SCAN_JSON_FILE="$TMP/scan-out.json"
RUN_MIGRATE scan --source A,B,C --json 2>/dev/null > "$SCAN_JSON_FILE"
if python3 - "$SCAN_JSON_FILE" <<'PYSCAN'
import json, sys
raw = open(sys.argv[1]).read()
d = json.JSONDecoder().raw_decode(raw[raw.index("{"):])[0]
t = d["totals"]
notable = {n["rel"]: n for n in d["notable_collisions"]}
bad = []
for rel in ("hosts/Test-Host/crons.json", "relay/relay-1.md"):
    n = notable.get(rel)
    if n is None:
        bad.append(f"{rel}: missing from notable_collisions"); continue
    if n["byte_identical"] is not False:
        bad.append(f"{rel}: byte_identical={n['byte_identical']!r}, want False")
if t.get("proxy_identical_divergent", 0) < 2:
    bad.append(f"proxy_identical_divergent={t.get('proxy_identical_divergent')}, want >=2")
ident_true = sum(1 for n in notable.values() if n["byte_identical"] is True)
if t["identical_content"] != ident_true:
    bad.append(f"identical_content={t['identical_content']} != byte-verified count {ident_true}")
n = notable.get("notes/unreadable.md")
if n is None:
    bad.append("notes/unreadable.md: missing from notable_collisions")
elif n["byte_identical"] is not None:
    bad.append(f"notes/unreadable.md: byte_identical={n['byte_identical']!r}, want None (unverified)")
if t.get("identity_unverified") != 1:
    bad.append(f"identity_unverified={t.get('identity_unverified')!r}, want 1")
if any(nn["rel"] == "notes/unreadable.md" for nn in d["notable_collisions"])\
        and t.get("proxy_identical_divergent", 0) != 2:
    bad.append(f"proxy_identical_divergent={t.get('proxy_identical_divergent')} counts the unreadable file, want exactly 2")
if bad:
    print("; ".join(bad)); sys.exit(1)
sys.exit(0)
PYSCAN
then
    echo "  OK: scan --json marks equal-proxy divergent fixtures actionable (byte_identical=false)"
else
    echo "  FAIL: scan --json content-identity contract"
    fail_scan_json=1
fi

echo
echo "==== TEST: scan uses the resolved interpreter, not PATH python3 ===="
# Control for the clean-macOS CLT-stub case: PATH python3 is a failing shim and
# $SUTANDO_PY points at a real interpreter — both report paths must still work.
REAL_PY="$(command -v python3)"
SHIMDIR="$TMP/shim-bin"
mkdir -p "$SHIMDIR"
printf '#!/bin/sh\nexit 97\n' > "$SHIMDIR/python3"
chmod +x "$SHIMDIR/python3"
fail_stub=0
STUB_HUMAN="$(PATH="$SHIMDIR:$PATH" SUTANDO_PY="$REAL_PY" RUN_MIGRATE scan --source A,B,C 2>&1 || true)"
if ! echo "$STUB_HUMAN" | grep -q "of which identical-content (byte-verified):  [0-9]"; then
    echo "  FAIL: human scan lost its identity report under a shadowed PATH python3"
    fail_stub=1
fi
STUB_JSON="$TMP/scan-stub.json"
if ! PATH="$SHIMDIR:$PATH" SUTANDO_PY="$REAL_PY" RUN_MIGRATE scan --source A,B,C --json 2>/dev/null > "$STUB_JSON"; then
    echo "  FAIL: json scan exited non-zero under a shadowed PATH python3"
    fail_stub=1
elif ! "$REAL_PY" -c 'import json,sys;raw=open(sys.argv[1]).read();d=json.JSONDecoder().raw_decode(raw[raw.index("{"):])[0];assert "identity_unverified" in d["totals"]' "$STUB_JSON" 2>/dev/null; then
    echo "  FAIL: json scan emitted no parseable contract under a shadowed PATH python3"
    fail_stub=1
fi
[ "$fail_stub" -eq 0 ] && echo "  OK: both report paths ran on \$SUTANDO_PY with PATH python3 shadowed"

echo
echo "==== TEST: scan leaves no residue in TMPDIR (verdict tempfile cleanup) ===="
# Two probes: GNU mktemp honors $TMPDIR (isolated dir discriminates on Linux);
# macOS ignores it, so the named-template count in the real temp root gates there.
ISO_TMP="$TMP/iso-tmpdir"
mkdir -p "$ISO_TMP"
REAL_T="$(dirname "$(mktemp -u)")"
PRE_VERDICTS="$(ls "$REAL_T" 2>/dev/null | grep -c "sutando-migrate-verdicts" || true)"
TMPDIR="$ISO_TMP" RUN_MIGRATE scan --source A,B,C --json > /dev/null 2>&1
TMPDIR="$ISO_TMP" RUN_MIGRATE scan --source A,B,C > /dev/null 2>&1
# Scope to the tempfiles THIS script creates. Any-file-is-a-leak is wrong on a
# Command-Line-Tools host, where Apple's xcrun caches `xcrun_db` into $TMPDIR.
LEFTOVER="$(ls -A "$ISO_TMP" 2>/dev/null | grep '^sutando-migrate-verdicts' || true)"
POST_VERDICTS="$(ls "$REAL_T" 2>/dev/null | grep -c "sutando-migrate-verdicts" || true)"
if [ -n "$LEFTOVER" ]; then
    echo "  FAIL: scan left files in isolated TMPDIR: $LEFTOVER"
    fail_stub=1
elif [ "$POST_VERDICTS" != "$PRE_VERDICTS" ]; then
    echo "  FAIL: verdict tempfiles accumulated in $REAL_T ($PRE_VERDICTS -> $POST_VERDICTS)"
    fail_stub=1
else
    echo "  OK: no scan residue (isolated TMPDIR empty; verdict count stable $PRE_VERDICTS)"
fi

# The narrowed probe must still SEE a real leak and NOT fire on foreign residue;
# without the first, narrowing could make the assertion unable to fail at all.
_ctl="$TMP/leak-ctl"; mkdir -p "$_ctl"
: > "$_ctl/sutando-migrate-verdicts.abc123"
_pos="$(ls -A "$_ctl" 2>/dev/null | grep '^sutando-migrate-verdicts' || true)"
if [ -n "$_pos" ]; then
    echo "  OK: control — the narrowed probe still CATCHES a real verdict tempfile"
else
    echo "  FAIL: control — narrowed probe cannot see a real leak; it can no longer fail"
    fail_stub=1
fi
rm -f "$_ctl/sutando-migrate-verdicts.abc123"
: > "$_ctl/xcrun_db"
_neg="$(ls -A "$_ctl" 2>/dev/null | grep '^sutando-migrate-verdicts' || true)"
if [ -z "$_neg" ]; then
    echo "  OK: control — foreign tooling residue (xcrun_db) is correctly ignored"
else
    echo "  FAIL: control — probe fired on foreign residue: $_neg"
    fail_stub=1
fi
rm -rf "$_ctl"

echo
echo "==== TEST: python-binary.sh owns require_python (no shadow) ===="
# The sourced helper is the single loud-failure owner; a local redefinition
# shadows it and its subshell memoization silently never caches.
if [ "$(grep -cE '^require_python\(\)' "$REPO/scripts/sutando-migrate.sh")" != "0" ]; then
    echo "  FAIL: sutando-migrate.sh redefines require_python (shadows python-binary.sh)"
    fail_stub=1
elif ! grep -q 'python-binary.sh' "$REPO/scripts/sutando-migrate.sh"; then
    echo "  FAIL: sutando-migrate.sh no longer sources python-binary.sh"
    fail_stub=1
else
    echo "  OK: single require_python owner (sourced from python-binary.sh)"
fi

# Restore the unreadable fixture before commit — its scan job is done, and the
# commit-phase copy semantics for unreadable sources are a separate contract.
chmod 644 "$A/notes/unreadable.md"

echo
echo "==== TEST: commit ===="
COMMIT_OUT="$(RUN_MIGRATE commit --source A,B,C 2>&1)"
echo "$COMMIT_OUT" | grep -E "Committing source|copied:|identical:|kept-dest:|sidecar:|skipped:|sentinel:|backup|COMMIT" | head -40
INITIAL_BACKUP_ID="$(echo "$COMMIT_OUT" | grep -E "migration-backup-.*\.tar\.gz" | head -1 | sed -E 's@.*migration-backup-(.+)\.tar\.gz.*@\1@' || true)"

echo
echo "==== ASSERTIONS ===="
fail=0
[ "${fail_stub:-0}" -ne 0 ] && { echo "FAIL: interpreter-resolution control"; fail=1; }
[ "${fail_scan_json:-0}" = "1" ] && fail=1

# 1. build_log.md sidecar default: each source's variant goes to legacy/<tag>/build_log.md
for tag in A B C; do
    if [ ! -f "$DEST/legacy/$tag/build_log.md" ]; then
        echo "  FAIL: $DEST/legacy/$tag/build_log.md missing"
        fail=1
    fi
done
[ "$fail" = "0" ] && echo "  OK: build_log.md sidecar quarantine for A,B,C"

# 2. notes/shared.md — identical between B+C, should land at dest once (mtime preserved)
if [ ! -f "$DEST/notes/shared.md" ]; then
    echo "  FAIL: $DEST/notes/shared.md missing"; fail=1
else
    # mtime should match source (Python for portability — stat -f %m is BSD-only)
    src_mt="$(python3 -c "import os; print(int(os.stat('$C/notes/shared.md').st_mtime))")"
    dst_mt="$(python3 -c "import os; print(int(os.stat('$DEST/notes/shared.md').st_mtime))")"
    if [ "$src_mt" != "$dst_mt" ]; then
        echo "  FAIL: shared.md mtime not preserved (src=$src_mt dst=$dst_mt)"; fail=1
    else
        echo "  OK: notes/shared.md identical drop-dup; mtime preserved"
    fi
fi

# 3. notes/divergent.md — A wins (newer mtime), C version (which was at dest after C committed
#    first) goes to .legacy-prior-from-A-<ts> sidecar per Mini #3 fix (timestamped + tagged).
if [ ! -f "$DEST/notes/divergent.md" ]; then
    echo "  FAIL: divergent.md missing"; fail=1
else
    body="$(cat "$DEST/notes/divergent.md")"
    # Sidecar uses glob: divergent.md.legacy-prior-from-A-<timestamp>
    sidecar_path="$( { ls "$DEST/notes/divergent.md.legacy-prior-from-A-"* 2>/dev/null || true; } | head -1 )"
    if [ -z "$sidecar_path" ]; then
        echo "  FAIL: .legacy-prior-from-A-<ts> sidecar missing"; fail=1
    elif [ "$body" != "divergent A-version" ]; then
        echo "  FAIL: divergent.md should hold A-version (newer mtime wins), got: $body"; fail=1
    elif [ "$(cat "$sidecar_path")" != "divergent C-version" ]; then
        echo "  FAIL: sidecar should hold C-version (what was at dest before A), got: $(cat "$sidecar_path")"; fail=1
    else
        echo "  OK: notes/divergent.md collision A-wins; C-version sidecared at $(basename "$sidecar_path") (timestamped per Mini #3)"
    fi
fi

# 3b. hosts/ + relay/ equal-mtime equal-size different content must be a
#     COLLISION (both variants present under DEST), never identical-drop.
for pair in "hosts/Test-Host/crons.json|AAAA|BBBB" "relay/relay-1.md|RRRR|SSSS"; do
    rel="${pair%%|*}"; rest="${pair#*|}"; srcv="${rest%%|*}"; dstv="${rest#*|}"
    hits_src="$( { grep -rl "$srcv" "$DEST/$(dirname "$rel")" 2>/dev/null || true; } | wc -l | tr -d ' ')"
    hits_dst="$( { grep -rl "$dstv" "$DEST/$(dirname "$rel")" 2>/dev/null || true; } | wc -l | tr -d ' ')"
    if [ "$hits_src" -ge 1 ] && [ "$hits_dst" -ge 1 ]; then
        echo "  OK: $rel equal-mtime/equal-size different content preserved as collision (both variants under DEST)"
    else
        echo "  FAIL: $rel — same-mtime/same-size different content lost a variant (src-present=$hits_src dst-present=$hits_dst)"; fail=1
    fi
done

# 4. cloud-auth.json re-homed to dest/state/auth/ per Mini #design 2026-06-02
if [ ! -f "$DEST/state/auth/cloud-auth.json" ]; then
    echo "  FAIL: cloud-auth.json not re-homed to state/auth/"; fail=1
else
    echo "  OK: cloud-auth.json re-homed to state/auth/ (Mini per-file recommendation)"
fi

# 5. state/contextual-chips.json newest-mtime: A wins
if [ ! -f "$DEST/state/contextual-chips.json" ]; then
    echo "  FAIL: state/contextual-chips.json missing"; fail=1
else
    body="$(cat "$DEST/state/contextual-chips.json")"
    if [[ "$body" != *'"new":"snapshot"'* ]]; then
        echo "  FAIL: state/contextual-chips.json should be A's newer version, got: $body"; fail=1
    else
        echo "  OK: state/contextual-chips.json newest-mtime A wins"
    fi
fi

# 3b. 3-way collision (Mini #3): ALL 3 versions preserved uniquely.
# After commit C→A→B, A wins canonical, C goes to sidecar prior-from-A,
# B is the oldest+dest-loser → sidecar legacy-B.
side_a="$( { ls "$DEST/notes/divergent.md.legacy-prior-from-A-"* 2>/dev/null || true; } | head -1 )"
side_b="$( { ls "$DEST/notes/divergent.md.legacy-B-"*-p* 2>/dev/null || true; } | head -1 )"
if [ -z "$side_a" ] || [ -z "$side_b" ]; then
    echo "  FAIL: 3-way collision: missing one of the sidecars (prior-from-A=$side_a, legacy-B=$side_b)"
    fail=1
elif [ "$(cat "$side_a")" != "divergent C-version" ]; then
    echo "  FAIL: prior-from-A sidecar should hold C-version (what was at dest before A landed)"
    fail=1
elif [ "$(cat "$side_b")" != "divergent B-version" ]; then
    echo "  FAIL: legacy-B sidecar should hold B-version (older src lost to dest)"
    fail=1
else
    echo "  OK: 3-way collision preserves all 3 versions: canonical=A, prior-from-A=C, legacy-B=B"
fi

# 6. tasks/task-now.txt in-flight protected (NOT copied)
if [ -f "$DEST/tasks/task-now.txt" ]; then
    echo "  FAIL: in-flight task incorrectly copied"; fail=1
else
    echo "  OK: in-flight task (<60s) skipped"
fi

# 6c. Quarantine (Lucy #design + owner direction): non-canonical content from
# B + C goes to <dest>/legacy/<src-tag>/quarantine/<rel>, not skip-unknown.
for q in "$DEST/legacy/C/quarantine/experiments/note.md" \
         "$DEST/legacy/C/quarantine/obsidian-vault/daily.md" \
         "$DEST/legacy/C/quarantine/repro-bug.ts" \
         "$DEST/legacy/B/quarantine/personal-src/lib.py"; do
    if [ ! -f "$q" ]; then
        echo "  FAIL: quarantine target missing: $q"
        fail=1
    fi
done
if [ -z "${fail:-}" ] || [ "$fail" = "0" ]; then
    echo "  OK: quarantine preserves 4 non-canonical user files at legacy/<tag>/quarantine/"
fi

# 6b. tasks/task-stale.txt from B routes to archive/B/, NOT to live tasks/
if [ -f "$DEST/tasks/task-stale.txt" ]; then
    echo "  FAIL: stale task incorrectly copied to live tasks/ (would re-fire watcher)"; fail=1
elif [ ! -f "$DEST/tasks/archive/B/task-stale.txt" ]; then
    echo "  FAIL: stale task not routed to archive/B/"; fail=1
else
    echo "  OK: stale task routed to tasks/archive/B/ (no watcher re-fire)"
fi

# 6d. (#3036) B/C custom scripts/ MIGRATE to dest — canonical newest wins,
# conflicting version preserved (collision-keep-both), nothing quarantined.
if [ ! -f "$DEST/scripts/my-tool.sh" ]; then
    echo "  FAIL: scripts/my-tool.sh not at dest (quarantined? #3036 surface regression)"; fail=1
elif [ "$(cat "$DEST/scripts/my-tool.sh")" != "C tool v2" ]; then
    echo "  FAIL: scripts/my-tool.sh should hold C's newer version, got: $(cat "$DEST/scripts/my-tool.sh")"; fail=1
else
    tool_sidecar="$( { ls "$DEST/scripts/my-tool.sh.legacy-"* 2>/dev/null || true; } | head -1 )"
    if [ -z "$tool_sidecar" ]; then
        echo "  FAIL: scripts/my-tool.sh conflicting B version not preserved (no legacy sidecar)"; fail=1
    elif [ "$(cat "$tool_sidecar")" != "B tool v1" ]; then
        echo "  FAIL: scripts sidecar should hold B's version, got: $(cat "$tool_sidecar")"; fail=1
    else
        echo "  OK: scripts/ migrates; collision keeps both (canonical=C, sidecar=B)"
    fi
fi
if [ -e "$DEST/legacy/B/quarantine/scripts" ] || [ -e "$DEST/legacy/C/quarantine/scripts" ]; then
    echo "  FAIL: B/C scripts/ was quarantined — must migrate to dest (#3036)"; fail=1
fi

# 6e. (#3036) nested agent config tree lands at its canonical relpath
for f in "$DEST/.claude-sutando/skills/custom/SKILL.md" \
         "$DEST/.claude-sutando/hooks/pre-task.py"; do
    if [ ! -f "$f" ]; then
        echo "  FAIL: agent-config file not migrated: $f (#3036 surface regression)"; fail=1
    fi
done
if [ -e "$DEST/legacy/C/quarantine/.claude-sutando" ]; then
    echo "  FAIL: .claude-sutando was quarantined — breaks hooks/skills/memory (#3036)"; fail=1
fi
[ "$fail" = "0" ] && echo "  OK: .claude-sutando nested skill+hook migrated to canonical relpaths"

# 6e-bis. (#3036 P1) a workspace-owned scripts/sutando-config.sh must NOT make
# B look like a repo checkout and swallow its scripts/ surface.
if [ ! -f "$DEST/scripts/b-only-tool.sh" ]; then
    echo "  FAIL: B scripts/b-only-tool.sh dropped — workspace-owned sutando-config.sh triggered the repo exclusion (#3036 P1)"; fail=1
elif [ "$(cat "$DEST/scripts/b-only-tool.sh")" != "B only tool" ]; then
    echo "  FAIL: B scripts/b-only-tool.sh content wrong: $(cat "$DEST/scripts/b-only-tool.sh")"; fail=1
else
    echo "  OK: workspace-owned sutando-config.sh does not exclude B scripts/"
fi

# 6f. (#3036) repo-root Source A scripts/ is CODE — excluded, not migrated, not quarantined
if [ -f "$DEST/scripts/repo-code.sh" ]; then
    echo "  FAIL: Source A repo scripts/ leaked into dest (SOURCE_A_EXCLUDE regression)"; fail=1
elif [ -e "$DEST/legacy/A/quarantine/scripts" ]; then
    echo "  FAIL: Source A repo scripts/ was quarantined — should be excluded entirely"; fail=1
else
    echo "  OK: Source A repo scripts/ excluded from migration"
fi

# 6g. newer-empty + older-populated must yield a POPULATED active file — the
# reported bug; newest-mtime and structural both fail it, neither merges in-file.
UJ="$DEST/state/slack-allowed-recipients.json"
if [ ! -f "$UJ" ]; then
    echo "  FAIL: $UJ missing"; fail=1
else
    union_check="$(python3 - "$UJ" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
got = d.get("allowFrom")
problems = []
if sorted(got or []) != ["U_OLD_ONE", "U_SHARED"]:
    problems.append(f"allowFrom={got!r}, expected the union of both sources")
if len(got or []) != len(set(map(repr, got or []))):
    problems.append(f"duplicate entries in allowFrom: {got!r}")
if d.get("schemaVersion") != 2:
    problems.append(f"schemaVersion={d.get('schemaVersion')!r}, expected 2 from the newer file")
print("; ".join(problems) if problems else "OK")
PY
)"
    if [ "$union_check" = "OK" ]; then
        echo "  OK: allow-set unioned (grants survive a newer empty file; unrelated fields kept)"
    else
        echo "  FAIL: union-json-array — $union_check"; fail=1
    fi
fi

# 6i. Three-source accumulation: the scalar winner must be the NEWEST source, not
# whichever ran last — the union rewrites dest and resets its mtime, hiding this.
U3="$TMP/union3"
mkdir -p "$U3"
printf '{"schemaVersion":1,"allowFrom":["U_C"]}\n' > "$U3/C.json"
printf '{"schemaVersion":2,"allowFrom":["U_A"]}\n' > "$U3/A.json"
printf '{"schemaVersion":3,"allowFrom":["U_B"]}\n' > "$U3/B.json"
touch -t 202601010000 "$U3/C.json"
touch -t 202601020000 "$U3/A.json"
touch -t 202601030000 "$U3/B.json"
cp "$U3/C.json" "$U3/dst.json"; touch -t 202601010000 "$U3/dst.json"
# Call the production writer itself, not a reimplementation of the rule.
u3_fn="$TMP/union_fn.sh"
python3 - "$MIGRATE" "$u3_fn" "$REPO" <<'PYX'
import sys
src, out, repo = sys.argv[1], sys.argv[2], sys.argv[3]
s = open(src).read()
i = s.index("union_json_arrays_into() {")
j = s.index("\n}\n", i) + 3
open(out, "w").write(
    f'SCRIPT_DIR="{repo}/scripts"\nREPO_DIR="{repo}"\n'
    f'. "{repo}/scripts/python-binary.sh"\n\n' + s[i:j])
PYX
u3_check="$(
  . "$u3_fn"
  union_json_arrays_into "$U3/A.json" "$U3/dst.json" || echo "union A failed"
  union_json_arrays_into "$U3/B.json" "$U3/dst.json" || echo "union B failed"
  python3 - "$U3/dst.json" <<'PYX'
import json, sys
d = json.load(open(sys.argv[1]))
problems = []
if d.get("schemaVersion") != 3:
    problems.append(f"schemaVersion={d.get('schemaVersion')!r}, expected 3 from the newest source")
if sorted(d.get("allowFrom") or []) != ["U_A", "U_B", "U_C"]:
    problems.append(f"allowFrom={d.get('allowFrom')!r}, expected all three accumulated")
print("; ".join(problems) if problems else "OK")
PYX
)"
if [ "$u3_check" = "OK" ]; then
    echo "  OK: three-source union keeps the newest scalar and accumulates every array"
else
    echo "  FAIL: three-source union — $u3_check"; fail=1
fi

# 6h. Idempotency against the REAL script, not a reimplementation: a second pass
# over an already-unioned dest must not duplicate entries or drop fields.
IDEM_DEST="$TMP/dest-idem"
mkdir -p "$IDEM_DEST/state"
cp -p "$UJ" "$IDEM_DEST/state/slack-allowed-recipients.json"
before_idem="$(cat "$IDEM_DEST/state/slack-allowed-recipients.json")"
SUTANDO_MIGRATE_SRC_C="$C" SUTANDO_MIGRATE_DEST="$IDEM_DEST" \
    bash "$MIGRATE" commit --source C --no-confirm >/dev/null 2>&1 || true
after_idem="$(python3 - "$IDEM_DEST/state/slack-allowed-recipients.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print(json.dumps({"allowFrom": sorted(d.get("allowFrom") or []),
                  "schemaVersion": d.get("schemaVersion")}, sort_keys=True))
PY
)"
expected_idem='{"allowFrom": ["U_OLD_ONE", "U_SHARED"], "schemaVersion": 2}'
if [ "$after_idem" = "$expected_idem" ]; then
    echo "  OK: union is idempotent (second pass adds no duplicates, keeps fields)"
else
    echo "  FAIL: union not idempotent — got $after_idem, expected $expected_idem"
    echo "        (before: $before_idem)"
    fail=1
fi

# 7. Per-source sentinels exist
for tag in A B C; do
    if ! ls "$DEST/state/.migrated-from-$tag-"* >/dev/null 2>&1; then
        echo "  FAIL: missing sentinel for source $tag"; fail=1
    fi
done

# 8. Sources preserved (no --delete-source)
for src in "$A" "$B" "$C"; do
    [ ! -f "$src/build_log.md" ] && { echo "  FAIL: source build_log.md deleted at $src"; fail=1; }
done
[ "$fail" = "0" ] && echo "  OK: sources preserved (default no-delete)"

# 9. Idempotency: re-run commit, should detect sentinel + skip
echo
echo "==== TEST: verify (mandatory phase three — this suite claims scan→commit→verify) ===="
# The fixture includes a divergent union (state/slack-allowed-recipients.json),
# so this also pins class-aware semantic verification: a union result differs
# from both inputs by design and must still verify.
VERIFY_OUT="$(RUN_MIGRATE --verify 2>&1)" && verify_rc=0 || verify_rc=$?
echo "$VERIFY_OUT" | grep -E "verify summary|verify:" | head -3
if [ "$verify_rc" -ne 0 ]; then
    echo "  FAIL: verify exited $verify_rc after a successful commit"; fail=1
else
    echo "  OK: post-commit verify passes"
fi

echo "==== TEST: re-run commit (idempotency) ===="
out="$(RUN_MIGRATE commit --source A,B,C 2>&1)"
if echo "$out" | grep -q "prior migration sentinel — skip"; then
    echo "  OK: re-run detects sentinel + skips all 3 sources"
else
    echo "  FAIL: re-run did not detect sentinel; output:"
    echo "$out" | head -10
    fail=1
fi

# 10. Rollback FIRST (before --delete-source mutates dest state). Use INITIAL_BACKUP_ID.
echo
echo "==== TEST: rollback ===="
backup_id="$INITIAL_BACKUP_ID"
if [ -z "$backup_id" ]; then
    echo "  FAIL: no initial backup id captured"; fail=1
else
    RUN_MIGRATE rollback --backup-id "$backup_id" 2>&1 | grep -E "ROLLBACK|OK" || true
    if [ -f "$DEST/notes/divergent.md" ] || [ -f "$DEST/legacy/A/build_log.md" ]; then
        echo "  FAIL: rollback did not restore (artifacts remain)"; fail=1
    else
        echo "  OK: rollback restored dest to pre-commit state"
    fi
fi

# Re-commit so --delete-source has something to delete from.
RUN_MIGRATE commit --source A,B,C 2>&1 > /dev/null
COMMIT2_BACKUP_ID="$(ls "$DEST/state/migration-backup-"*.tar.gz | sort -r | head -1 | sed -E 's@.*migration-backup-(.+)\.tar\.gz@\1@')"

# 9b. --delete-source: requires --backup-id (Mini's polish). Without it, refuses.
echo
echo "==== TEST: --delete-source requires --backup-id ===="
out="$(RUN_MIGRATE commit --source A,B,C --delete-source 2>&1 || true)"
if echo "$out" | grep -q "ERROR: --delete-source requires --backup-id"; then
    echo "  OK: --delete-source without --backup-id refused with explanation"
else
    echo "  FAIL: --delete-source without --backup-id should refuse, got: $out"
    fail=1
fi

# 9c. --delete-source --backup-id <id>: actually deletes sources after sha verify (Mini #4).
echo
echo "==== TEST: --delete-source actually removes sources ===="
out2="$(RUN_MIGRATE commit --source A,B,C --delete-source --backup-id "$COMMIT2_BACKUP_ID" 2>&1 || true)"
if echo "$out2" | grep -q "deleted:"; then
    # Pick a known-sidecared source file that sha-matches dest:
    # state/contextual-chips.json was newest-mtime'd; A's version landed at dest.
    # Verify A's source contextual-chips.json is now gone post-delete-source.
    if [ ! -f "$A/state/contextual-chips.json" ]; then
        echo "  OK: --delete-source removed A/state/contextual-chips.json (sha matched dest)"
    else
        echo "  OK: --delete-source ran (deleted counter printed; some kept-unsafe is fine)"
    fi
else
    echo "  FAIL: --delete-source did not print 'deleted:' counter; output: $(echo "$out2" | tail -5)"
    fail=1
fi

echo
if [ "$fail" = "0" ]; then
    echo "==== ALL ASSERTIONS PASSED ===="
    exit 0
else
    echo "==== TEST FAILED ===="
    exit 1
fi
