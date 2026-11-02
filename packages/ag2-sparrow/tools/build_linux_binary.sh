#!/bin/bash
# Build + smoke the one-file Linux binary; run it inside python:3.12-slim-bullseye,
# whose glibc (2.31) becomes the binary's floor. Usage: build_linux_binary.sh <output>
set -euo pipefail
OUT="${1:?usage: build_linux_binary.sh <output-path>}"
PKG="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

if ! command -v objdump >/dev/null; then
  apt-get update -qq && apt-get install -y -qq --no-install-recommends binutils >/dev/null
fi
python3 -m pip install --quiet --disable-pip-version-check pyinstaller==6.22.3

printf 'from ag2_sparrow.remote_gateway_bridge import main\nmain()\n' > "$WORK/ag2-sparrow.py"
python3 -m PyInstaller --onefile --name ag2-sparrow --paths "$PKG" \
  --collect-submodules ag2_sparrow --distpath "$WORK/dist" --workpath "$WORK/build" \
  --specpath "$WORK" --log-level WARN "$WORK/ag2-sparrow.py"

mkdir -p "$(dirname "$OUT")"
cp "$WORK/dist/ag2-sparrow" "$OUT"
echo "glibc floor: $(ldd --version | head -1)"
python3 "$PKG/tools/smoke_entry.py" "$OUT"
