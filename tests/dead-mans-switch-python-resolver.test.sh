#!/bin/bash
# The ping job runs from launchd every 300s and can reach the CLT stub, which
# satisfies `command -v`, so the resolver must prove the interpreter runs.
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
pass=0; fail=0
ck() { if [ "$2" = "$3" ]; then pass=$((pass+1)); echo "  ok   $1"; else fail=$((fail+1)); echo "  FAIL $1 (got '$2' want '$3')"; fi; }

stub="$(mktemp -d)/python3"
printf '#!/bin/sh\necho "xcode-select: note: No developer tools were found." >&2\nexit 1\n' > "$stub"
chmod +x "$stub"

command -v "$stub" >/dev/null 2>&1 && cv=yes || cv=no
ck "the stub satisfies command -v (why a name check is not enough)" "$cv" "yes"
"$stub" -c 'import sys' >/dev/null 2>&1 && runs=yes || runs=no
ck "the stub fails the run check" "$runs" "no"

# Exercise the PRODUCTION resolver, not a copied recipe: a local re-implementation
# passes while the shipped one drifts, which is the failure this file exists for.
# shellcheck source=../scripts/python-binary.sh
. "$REPO/scripts/python-binary.sh"

# An explicit pin wins by contract — resolve_python does not re-probe it. The
# subshell is required: `VAR=v x=$(...)` is two assignments, so VAR would leak.
pinned="$(SUTANDO_PY="$stub"; resolve_python "$REPO" || true)"
ck "an explicit SUTANDO_PY pin is honoured verbatim" "$pinned" "$stub"

real="$(resolve_python "$REPO" || true)"
ck "a real interpreter is found when one exists" "$([ -n "$real" ] && echo yes || echo no)" "yes"
ck "and the selection actually runs" \
   "$("$real" -c 'print("yes")' 2>/dev/null || echo no)" "yes"

for f in skills/dead-mans-switch/scripts/ping.sh skills/dead-mans-switch/install.sh; do
  n="$(sed 's/[[:space:]]*#.*$//' "$REPO/$f" | grep -cE '(^|[^"/[:alnum:]_$])python3 ' | tr -d ' ')"
  ck "$(basename "$f") has no bare 'python3 ' invocation" "$n" "0"
  d="$(grep -c 'python-binary.sh' "$REPO/$f" | tr -d ' ')"
  ck "$(basename "$f") delegates to the shared resolver" "$d" "1"
  h="$(grep -cE '/opt/homebrew|/usr/local/bin' "$REPO/$f" | tr -d ' ')"
  ck "$(basename "$f") carries no hardcoded interpreter path" "$h" "0"
done

echo "  $pass passed, $fail failed"
[ "$fail" -eq 0 ]
