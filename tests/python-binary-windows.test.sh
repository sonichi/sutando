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
[ "$1" = "-c" ] && [ "$2" = "pass" ] && exit 0
exit 1
EOF
cat > "$LAB/bin/python" <<'EOF'
#!/bin/sh
[ "$1" = "-c" ] || exit 1
case "$2" in
  "pass"|"import wantedmod") exit 0 ;;
esac
exit 1
EOF
chmod +x "$LAB/bin/python3" "$LAB/bin/py" "$LAB/bin/python"

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

module_resolved="$(
  OSTYPE=msys PATH="$LAB/bin:/bin" bash -c \
    ". '$REPO/scripts/python-binary.sh'; resolve_python_for_module '$REPO' wantedmod"
)"
if [ "$module_resolved" != "$LAB/bin/python" ]; then
  echo "FAIL: expected module resolver to continue to python, got: $module_resolved" >&2
  exit 1
fi

echo "PASS: selected Windows PATH interpreters by runtime and module capability"
