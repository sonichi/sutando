#!/usr/bin/env bash
# Windows-specific contract for scripts/python-binary.sh.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LAB="$(mktemp -d)"
trap 'rm -rf "$LAB"' EXIT
mkdir -p "$LAB/bin"

cat > "$LAB/bin/python3" <<EOF
#!/bin/sh
echo probed >> "$LAB/store-probed"
exit 49
EOF
cat > "$LAB/bin/py" <<'EOF'
#!/bin/sh
[ "$1" = "-c" ] && exit 0
exit 1
EOF
chmod +x "$LAB/bin/python3" "$LAB/bin/py"

resolved="$(
  OSTYPE=msys PATH="$LAB/bin:/bin" bash -c \
    ". '$REPO/scripts/python-binary.sh'; resolve_python '$REPO'"
)"

if [ "$resolved" != "$LAB/bin/py" ]; then
  echo "FAIL: expected py fallback, got: $resolved" >&2
  exit 1
fi
if [ ! -f "$LAB/store-probed" ]; then
  echo "FAIL: Store alias was not functionally probed" >&2
  exit 1
fi

echo "PASS: rejected Store python3 alias and selected py"
