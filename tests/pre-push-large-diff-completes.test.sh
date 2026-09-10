#!/bin/bash
# The pre-push hook must finish on a large diff: `${DIFF//[[:space:]]/}` was
# quadratic in bash (a 74 KB diff spun for minutes); the bound is 10x streaming.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
git -C "$T" init -q -b main; git -C "$T" -c user.email=t@x -c user.name=t commit -q --allow-empty -m base
git -C "$T" update-ref refs/remotes/origin/main HEAD
mkdir -p "$T/scripts" "$T/.githooks"
cp "$REPO/.githooks/pre-push" "$T/.githooks/pre-push"
printf '#!/bin/bash\ncat >/dev/null\necho "review-checks: PASS"\n' > "$T/scripts/review-checks.sh"
python3 - "$T/big.py" <<'PY'
import sys; open(sys.argv[1],'w').write("".join(f"x{i} = {i}  # padding line\n" for i in range(6000)))
PY
git -C "$T" add -A && git -C "$T" -c user.email=t@x -c user.name=t commit -q -m big
sha="$(git -C "$T" rev-parse HEAD)"
echo "diff bytes: $(git -C "$T" diff origin/main...HEAD | wc -c | tr -d ' ')"
start=$(date +%s)
cd "$T"
# The hook is $pid itself, so the bound can kill the spinning bash directly.
bash .githooks/pre-push origin https://example.invalid/r.git \
  <<< "refs/heads/main $sha refs/heads/main 0000000000000000000000000000000000000000" &
pid=$!
while kill -0 "$pid" 2>/dev/null; do
  if [ $(( $(date +%s) - start )) -ge 30 ]; then kill "$pid" 2>/dev/null; echo "FAIL: pre-push still running after 30s on a large diff"; exit 1; fi
  sleep 0.2
done
wait "$pid" && echo "PASS: pre-push completed in $(( $(date +%s) - start ))s"
