#!/usr/bin/env bash
# Run the process-hygiene reaper once from a witness's EXIT trap: report-only unless opted in.
# Skill not linked into the host's skills dir: one line, skipped. Always exits 0.
set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KEY="PROCESS_HYGIENE_WITNESS_REAP"

cli=""
for arg in "$@"; do
  case "$arg" in
    --reap) cli="1" ;;
    --report-only) cli="0" ;;
    *) echo "witness-reap: ignoring unknown argument: $arg" >&2 ;;
  esac
done

skip() { echo "witness-reap: $1; skipping" >&2; exit 0; }

# Installed means linked by skills/install.sh, not merely shipped in the repo.
skills_dir=""
if [ -f "$REPO/scripts/sutando-config.sh" ]; then
  skills_dir="$(bash "$REPO/scripts/sutando-config.sh" claude-home-path skills 2>/dev/null)" || skills_dir=""
fi
[ -n "$skills_dir" ] || skip "no installed skills dir resolved"
skill="$(cd -P "$skills_dir/process-hygiene" 2>/dev/null && pwd)" || skill=""
reaper="$skill/scripts/reap_orphans.py"
{ [ -n "$skill" ] && [ -f "$reaper" ]; } || skip "process-hygiene skill not installed"

PY="python3"
if [ -f "$REPO/scripts/python-binary.sh" ]; then
  . "$REPO/scripts/python-binary.sh"
  PY="$(resolve_python "$REPO")"
  [ -n "$PY" ] || skip "no runnable python3 (resolve_python printed nothing)"
fi

# CLI > env > manifest, per skills/MANIFEST.md.
opt="$cli"
[ -n "$opt" ] || opt="${PROCESS_HYGIENE_WITNESS_REAP:-}"
if [ -z "$opt" ]; then
  opt="$("$PY" -c 'import json,sys
try: print(json.load(open(sys.argv[1])).get("config", {}).get(sys.argv[2], ""))
except Exception: print("")' "$skill/manifest.json" "$KEY" 2>/dev/null)"
fi
args=(--spotlight)
[ "$opt" = "1" ] && args=(--reap --spotlight)

out="$("$PY" "$reaper" "${args[@]}" 2>&1)"; rc=$?
[ -n "$out" ] && printf '%s\n' "$out" | sed 's/^/witness-reap: /' >&2
if [ "$rc" -ne 0 ]; then
  echo "witness-reap: reaper exited rc=$rc; ignored, the witness result stands" >&2
fi
exit 0
