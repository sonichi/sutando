#!/usr/bin/env bash
# Run the process-hygiene reaper once from a witness's EXIT trap, so no witness leaves orphans
# behind. Skill absent: one line, skipped. Always exits 0; a reaper error is printed, not raised.
set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REL="process-hygiene/scripts/reap_orphans.py"

roots=("$REPO/skills")
if [ -f "$REPO/scripts/sutando-config.sh" ]; then
  ws="$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)" || ws=""
  [ -n "$ws" ] && roots+=("$ws/skills")
fi

reaper=""
for root in "${roots[@]}"; do
  if [ -f "$root/$REL" ]; then reaper="$root/$REL"; break; fi
done

if [ -z "$reaper" ]; then
  echo "witness-reap: process-hygiene skill not installed; skipping" >&2
  exit 0
fi

out="$(python3 "$reaper" --reap --spotlight 2>&1)"; rc=$?
[ -n "$out" ] && printf '%s\n' "$out" | sed 's/^/witness-reap: /' >&2
if [ "$rc" -ne 0 ]; then
  echo "witness-reap: reaper exited rc=$rc; ignored, the witness result stands" >&2
fi
exit 0
