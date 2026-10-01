#!/bin/bash
# Forwarding shim: the skill is now skills/agy, and this path is removed next release.
# The real tree first (an installed link points into it), then the installed skills dir.
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
target="$here/../../agy/scripts/gemini-run.sh"
[[ -f "$target" ]] || target="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/agy/scripts/gemini-run.sh"
[[ -f "$target" ]] || { echo "gemini-run.sh: skills/agy not found beside $here or in the installed skills dir" >&2; exit 1; }
exec bash "$target" "$@"
