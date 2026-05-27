#!/usr/bin/env bash
# Tests for skills/loop-self-audit/scripts/audit.sh

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$REPO/skills/loop-self-audit/scripts/audit.sh"

PASS=0
FAIL=0
FAILURES=()

ok() {
  PASS=$((PASS + 1))
  echo "  ✓ $1"
}

fail() {
  FAIL=$((FAIL + 1))
  FAILURES+=("$1")
  echo "  ✗ $1"
}

assert_contains() {
  local desc="$1" haystack="$2" needle="$3"
  if echo "$haystack" | grep -qF "$needle"; then
    ok "$desc"
  else
    fail "$desc (expected: '$needle')"
  fi
}

assert_not_contains() {
  local desc="$1" haystack="$2" needle="$3"
  if ! echo "$haystack" | grep -qF "$needle"; then
    ok "$desc"
  else
    fail "$desc (unexpected: '$needle')"
  fi
}

assert_exit() {
  local desc="$1" expected="$2" actual="$3"
  if [[ "$actual" -eq "$expected" ]]; then
    ok "$desc"
  else
    fail "$desc (expected exit $expected, got $actual)"
  fi
}

# ── helpers ──────────────────────────────────────────────────────────────────

make_repo() {
  local dir
  dir="$(mktemp -d)"
  mkdir -p "$dir/notes" "$dir/results" "$dir/state"
  echo "$dir"
}

make_build_log() {
  local repo="$1"
  shift
  # Each arg is one decision line
  printf '%s\n' "$@" > "$repo/build_log.md"
}

# ── file-structure tests ──────────────────────────────────────────────────────

test_skill_md_exists() {
  if [[ -f "$REPO/skills/loop-self-audit/SKILL.md" ]]; then
    ok "SKILL.md exists"
  else
    fail "SKILL.md missing at skills/loop-self-audit/SKILL.md"
  fi
}

test_audit_sh_exists() {
  if [[ -f "$SCRIPT" ]]; then
    ok "audit.sh exists"
  else
    fail "audit.sh missing at $SCRIPT"
  fi
}

test_audit_sh_executable() {
  if [[ -x "$SCRIPT" ]]; then
    ok "audit.sh is executable"
  else
    fail "audit.sh is not executable"
  fi
}

test_audit_sh_shebang() {
  local first
  first="$(head -1 "$SCRIPT")"
  if [[ "$first" == "#!/usr/bin/env bash" ]]; then
    ok "audit.sh has correct shebang"
  else
    fail "audit.sh shebang: expected '#!/usr/bin/env bash', got '$first'"
  fi
}

test_skill_md_has_usage() {
  if grep -q "Usage" "$REPO/skills/loop-self-audit/SKILL.md"; then
    ok "SKILL.md documents usage"
  else
    fail "SKILL.md missing usage section"
  fi
}

# ── missing build_log ─────────────────────────────────────────────────────────

test_missing_build_log_exits_1() {
  local tmp_repo
  tmp_repo="$(make_repo)"
  # No build_log.md
  out="$(SUTANDO_REPO_DIR="$tmp_repo" bash "$SCRIPT" 2>&1)" && rc=$? || rc=$?
  assert_exit "missing build_log exits 1" 1 "$rc"
  assert_contains "missing build_log prints error" "$out" "not found"
  rm -rf "$tmp_repo"
}

# ── no decision lines ─────────────────────────────────────────────────────────

test_empty_build_log_exits_0() {
  local tmp_repo
  tmp_repo="$(make_repo)"
  echo "# just some prose, no chose: lines" > "$tmp_repo/build_log.md"
  out="$(SUTANDO_REPO_DIR="$tmp_repo" bash "$SCRIPT" 2>&1)" && rc=$? || rc=$?
  assert_exit "no decision lines exits 0" 0 "$rc"
  assert_contains "no decision lines prints notice" "$out" "no decision lines found"
  rm -rf "$tmp_repo"
}

# ── basic report written ──────────────────────────────────────────────────────

test_report_written_to_notes() {
  local tmp_repo today
  tmp_repo="$(make_repo)"
  today="$(date -u +%Y-%m-%d)"
  make_build_log "$tmp_repo" \
    "chose: fix-bug — category: BUG — reason: test" \
    "chose: write-tests — category: QUALITY — reason: coverage" \
    "chose: update-docs — category: MAINTENANCE — reason: stale"
  SUTANDO_REPO_DIR="$tmp_repo" bash "$SCRIPT" >/dev/null 2>&1
  if [[ -f "$tmp_repo/notes/loop-self-audit-$today.md" ]]; then
    ok "report written to notes/loop-self-audit-<date>.md"
  else
    fail "report file not found at notes/loop-self-audit-$today.md"
  fi
  rm -rf "$tmp_repo"
}

test_report_has_frontmatter() {
  local tmp_repo today out_file
  tmp_repo="$(make_repo)"
  today="$(date -u +%Y-%m-%d)"
  make_build_log "$tmp_repo" \
    "chose: fix — category: BUG — reason: test"
  SUTANDO_REPO_DIR="$tmp_repo" bash "$SCRIPT" >/dev/null 2>&1
  out_file="$tmp_repo/notes/loop-self-audit-$today.md"
  content="$(cat "$out_file")"
  assert_contains "report has YAML frontmatter" "$content" "title: Loop self-audit"
  rm -rf "$tmp_repo"
}

# ── category distribution ─────────────────────────────────────────────────────

test_category_dist_shown() {
  local tmp_repo today out_file
  tmp_repo="$(make_repo)"
  today="$(date -u +%Y-%m-%d)"
  make_build_log "$tmp_repo" \
    "chose: a — category: BUG — reason: r1" \
    "chose: b — category: BUG — reason: r2" \
    "chose: c — category: MAINTENANCE — reason: r3"
  SUTANDO_REPO_DIR="$tmp_repo" bash "$SCRIPT" >/dev/null 2>&1
  content="$(cat "$tmp_repo/notes/loop-self-audit-$today.md")"
  assert_contains "category distribution section present" "$content" "Category distribution"
  assert_contains "BUG appears in distribution" "$content" "BUG"
  rm -rf "$tmp_repo"
}

test_untagged_counted() {
  local tmp_repo today out_file
  tmp_repo="$(make_repo)"
  today="$(date -u +%Y-%m-%d)"
  make_build_log "$tmp_repo" \
    "chose: old-action — reason: no category tag here" \
    "chose: new-action — category: BUG — reason: tagged"
  SUTANDO_REPO_DIR="$tmp_repo" bash "$SCRIPT" >/dev/null 2>&1
  content="$(cat "$tmp_repo/notes/loop-self-audit-$today.md")"
  assert_contains "UNTAGGED counted" "$content" "UNTAGGED"
  rm -rf "$tmp_repo"
}

# ── idle rate ─────────────────────────────────────────────────────────────────

test_idle_rate_normal() {
  local tmp_repo today
  tmp_repo="$(make_repo)"
  today="$(date -u +%Y-%m-%d)"
  # 1 WAITING out of 10 = 10% — below 20% threshold
  lines=()
  for i in $(seq 1 9); do
    lines+=("chose: act-$i — category: BUG — reason: r$i")
  done
  lines+=("chose: wait — category: WAITING — reason: blocked")
  make_build_log "$tmp_repo" "${lines[@]}"
  SUTANDO_REPO_DIR="$tmp_repo" bash "$SCRIPT" >/dev/null 2>&1
  content="$(cat "$tmp_repo/notes/loop-self-audit-$today.md")"
  assert_not_contains "no idle warning below 20%" "$content" "Above 20% threshold"
  rm -rf "$tmp_repo"
}

test_idle_rate_warning() {
  local tmp_repo today
  tmp_repo="$(make_repo)"
  today="$(date -u +%Y-%m-%d)"
  # 3 WAITING out of 10 = 30% — above 20% threshold
  lines=()
  for i in $(seq 1 7); do
    lines+=("chose: act-$i — category: BUG — reason: r$i")
  done
  for i in $(seq 1 3); do
    lines+=("chose: wait-$i — category: WAITING — reason: blocked")
  done
  make_build_log "$tmp_repo" "${lines[@]}"
  SUTANDO_REPO_DIR="$tmp_repo" bash "$SCRIPT" >/dev/null 2>&1
  content="$(cat "$tmp_repo/notes/loop-self-audit-$today.md")"
  assert_contains "idle warning at >20%" "$content" "Above 20% threshold"
  rm -rf "$tmp_repo"
}

# ── 3-same-section detection ──────────────────────────────────────────────────

test_no_3same_when_varied() {
  local tmp_repo today
  tmp_repo="$(make_repo)"
  today="$(date -u +%Y-%m-%d)"
  make_build_log "$tmp_repo" \
    "chose: a — category: BUG — reason: r1" \
    "chose: b — category: MAINTENANCE — reason: r2" \
    "chose: c — category: BUG — reason: r3"
  SUTANDO_REPO_DIR="$tmp_repo" bash "$SCRIPT" >/dev/null 2>&1
  content="$(cat "$tmp_repo/notes/loop-self-audit-$today.md")"
  assert_contains "no 3-same when varied" "$content" "None detected"
  rm -rf "$tmp_repo"
}

test_3same_detected() {
  local tmp_repo today
  tmp_repo="$(make_repo)"
  today="$(date -u +%Y-%m-%d)"
  make_build_log "$tmp_repo" \
    "chose: a — category: BUG — reason: r1" \
    "chose: b — category: BUG — reason: r2" \
    "chose: c — category: BUG — reason: r3"
  SUTANDO_REPO_DIR="$tmp_repo" bash "$SCRIPT" >/dev/null 2>&1
  content="$(cat "$tmp_repo/notes/loop-self-audit-$today.md")"
  assert_contains "3-same violation detected" "$content" "Rule-2"
  rm -rf "$tmp_repo"
}

# ── repeat-reason detection ───────────────────────────────────────────────────

test_repeat_reason_detected() {
  local tmp_repo today
  tmp_repo="$(make_repo)"
  today="$(date -u +%Y-%m-%d)"
  make_build_log "$tmp_repo" \
    "chose: a — category: BUG — reason: same-reason" \
    "chose: b — category: BUG — reason: same-reason" \
    "chose: c — category: BUG — reason: same-reason"
  SUTANDO_REPO_DIR="$tmp_repo" bash "$SCRIPT" >/dev/null 2>&1
  content="$(cat "$tmp_repo/notes/loop-self-audit-$today.md")"
  assert_contains "repeat reason detected" "$content" "same-reason"
  assert_contains "lazy-reasoning warning shown" "$content" "Lazy-reasoning"
  rm -rf "$tmp_repo"
}

test_no_repeat_reason_when_unique() {
  local tmp_repo today
  tmp_repo="$(make_repo)"
  today="$(date -u +%Y-%m-%d)"
  make_build_log "$tmp_repo" \
    "chose: a — category: BUG — reason: reason-one" \
    "chose: b — category: BUG — reason: reason-two" \
    "chose: c — category: BUG — reason: reason-three"
  SUTANDO_REPO_DIR="$tmp_repo" bash "$SCRIPT" >/dev/null 2>&1
  content="$(cat "$tmp_repo/notes/loop-self-audit-$today.md")"
  assert_not_contains "no repeat-reason when all unique" "$content" "Lazy-reasoning"
  rm -rf "$tmp_repo"
}

# ── anomaly dedup (sig file) ──────────────────────────────────────────────────

test_anomaly_dedup_same_types() {
  local tmp_repo today
  tmp_repo="$(make_repo)"
  today="$(date -u +%Y-%m-%d)"
  # Create a 3-same anomaly (rule-2 active on LATEST 3)
  make_build_log "$tmp_repo" \
    "chose: a — category: BUG — reason: r1" \
    "chose: b — category: BUG — reason: r2" \
    "chose: c — category: BUG — reason: r3"
  # First run: should fire
  out1="$(SUTANDO_REPO_DIR="$tmp_repo" bash "$SCRIPT" 2>&1)"
  # Second run with same anomaly: should suppress
  out2="$(SUTANDO_REPO_DIR="$tmp_repo" bash "$SCRIPT" 2>&1)"
  assert_contains "second run suppressed" "$out2" "unchanged or subset"
  rm -rf "$tmp_repo"
}

test_anomaly_sig_cleared_when_no_anomaly() {
  local tmp_repo today
  tmp_repo="$(make_repo)"
  today="$(date -u +%Y-%m-%d)"
  # Seed a sig file manually
  echo "rule-2" > "$tmp_repo/state/last-loop-audit-anomaly.txt"
  # Run with no anomalies
  make_build_log "$tmp_repo" \
    "chose: a — category: BUG — reason: r1" \
    "chose: b — category: MAINTENANCE — reason: r2" \
    "chose: c — category: QUALITY — reason: r3"
  SUTANDO_REPO_DIR="$tmp_repo" bash "$SCRIPT" >/dev/null 2>&1
  if [[ ! -f "$tmp_repo/state/last-loop-audit-anomaly.txt" ]]; then
    ok "sig file cleared when no anomaly"
  else
    fail "sig file not cleared when anomaly resolved"
  fi
  rm -rf "$tmp_repo"
}

# ── repo path resolution ──────────────────────────────────────────────────────

test_repo_uses_sutando_repo_dir_env() {
  local tmp_repo
  tmp_repo="$(make_repo)"
  make_build_log "$tmp_repo" \
    "chose: fix — category: BUG — reason: test"
  out="$(SUTANDO_REPO_DIR="$tmp_repo" bash "$SCRIPT" 2>&1)"
  assert_contains "uses SUTANDO_REPO_DIR env" "$out" "Wrote"
  rm -rf "$tmp_repo"
}

# ── run all tests ─────────────────────────────────────────────────────────────

test_skill_md_exists
test_audit_sh_exists
test_audit_sh_executable
test_audit_sh_shebang
test_skill_md_has_usage
test_missing_build_log_exits_1
test_empty_build_log_exits_0
test_report_written_to_notes
test_report_has_frontmatter
test_category_dist_shown
test_untagged_counted
test_idle_rate_normal
test_idle_rate_warning
test_no_3same_when_varied
test_3same_detected
test_repeat_reason_detected
test_no_repeat_reason_when_unique
test_anomaly_dedup_same_types
test_anomaly_sig_cleared_when_no_anomaly
test_repo_uses_sutando_repo_dir_env

echo
if [[ "$FAIL" -eq 0 ]]; then
  echo "All $PASS loop-self-audit tests passed."
else
  echo "$FAIL/$((PASS + FAIL)) tests FAILED:"
  for f in "${FAILURES[@]}"; do
    echo "  $f"
  done
  exit 1
fi
