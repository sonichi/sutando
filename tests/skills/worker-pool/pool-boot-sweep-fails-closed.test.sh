#!/usr/bin/env bash
# Step 1.7's bash block, extracted VERBATIM from skills/startup/SKILL.md and
# actually executed with a fake sweep script, not asserted in prose. Reviewed
# by qingyun-wu (Qingyun's Personal Codex) on PR #4503: the first version of
# this step only checked `rc -eq 3`, so a crash (1), a refused argument (2),
# or a missing interpreter (127) fell through silently and started the
# watcher anyway -- exactly the unsafe state the step exists to prevent.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
pass=0; fail=0
check() { if [ "$1" = "0" ]; then echo "  ok  $2"; pass=$((pass+1)); else echo "  FAIL $2"; fail=$((fail+1)); fi; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

# Extract the exact bash block Step 1.7 documents -- a fix to the SKILL.md
# prose is a fix to what this test runs, with no copy to drift out of step.
BLOCK="$(python3 -c "
import re
text = open('$REPO/skills/startup/SKILL.md').read()
m = re.search(r'### Step 1\.7.*?\`\`\`bash\n(.*?)\n\`\`\`', text, re.S)
print(m.group(1) if m else '')
")"
[ -n "$BLOCK" ]
check $? "Step 1.7's bash block extracts from skills/startup/SKILL.md"

# A stand-in for scripts/sutando-config.sh, callable from the extracted block.
mkdir -p "$TMP/scripts"
cat > "$TMP/scripts/sutando-config.sh" <<'SH'
#!/bin/bash
[ "$1" = "workspace" ] && echo "$TMP_WS"
SH
chmod +x "$TMP/scripts/sutando-config.sh"

run_block() {
  # $1 = the fake sweep's exit code
  local sweep="$TMP/fake-sweep-$1.py"
  printf '#!/usr/bin/env python3\nimport sys\nsys.exit(%s)\n' "$1" > "$sweep"
  chmod +x "$sweep"
  ( cd "$TMP" && TMP_WS="$TMP/ws" SUTANDO_POOL_BOOT_SWEEP="$sweep" \
    PATH="$REPO/../../..:$PATH" bash -c "
    $(command -v python3 >/dev/null || echo 'python3() { /usr/bin/env python3 \"\$@\"; }')
    $BLOCK
    echo REACHED_STEP_2
  " 2>"$TMP/stderr-$1.txt"; echo $?
  )
}

for rc in 1 2 3 127; do
  out="$(run_block "$rc")"
  exit_code="${out##*$'\n'}"
  [ "$exit_code" = "1" ]
  check $? "sweep exit $rc -> Step 1.7 refuses (exit 1), not $out"
  ! grep -q REACHED_STEP_2 <<<"$out"
  check $? "sweep exit $rc -> the watcher start (Step 2) is never reached"
done

out0="$(run_block 0)"
grep -q REACHED_STEP_2 <<<"$out0"
check $? "sweep exit 0 -> falls through, the watcher start IS reached"

grep -q "boot-time pool sweep exited 1" "$TMP/stderr-1.txt"
check $? "a non-3 failure (1) gets the generic diagnostic, not the code-3 one"
grep -q "boot-time pool sweep FAILED to publish" "$TMP/stderr-3.txt"
check $? "exit 3 keeps its own specific diagnostic"

echo ""
if [ "$fail" -eq 0 ]; then
  echo "PASS — $pass check(s) green"
  exit 0
else
  echo "FAIL — $fail of $((pass+fail)) check(s) red"
  exit 1
fi
