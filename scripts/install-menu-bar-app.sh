#!/usr/bin/env bash
# Build the optional macOS menu-bar app and its .app bundle, so an OSS install
# can actually run it. #2677 made core startup headless and removed the only
# code that produced the bundle, which left install-sutando-app-launchd.sh
# pointing at a path nothing builds.
#
# Usage: bash scripts/install-menu-bar-app.sh [--launch] [--supervise]
#   (no flags)   build the binary + bundle, sign, print what to do next
#   --launch     also launch it now
#   --supervise  also install the launchd KeepAlive supervisor (login auto-start)
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
SRC_DIR="$REPO/src/Sutando"
BIN="$SRC_DIR/Sutando"
APP="$SRC_DIR/Sutando.app"
LAUNCH=0
SUPERVISE=0

for arg in "$@"; do
  case "$arg" in
    --launch) LAUNCH=1 ;;
    --supervise) SUPERVISE=1 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

[ "$(uname -s)" = "Darwin" ] || { echo "macOS only — nothing to do."; exit 0; }
command -v swiftc >/dev/null 2>&1 || {
  echo "swiftc not found — install Xcode Command Line Tools: xcode-select --install" >&2
  exit 1
}

echo "Building menu-bar binary…"
( cd "$SRC_DIR" && swiftc -O -o Sutando main.swift SutandoConfig.swift \
    -framework Cocoa -framework Carbon -framework ApplicationServices \
    -framework AVFoundation )
echo "  ✓ $BIN"

# LSUIElement keeps it out of the Dock; the bundle id is what the TCC
# Accessibility grant binds to, so it must stay stable across rebuilds.
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Sutando</string>
  <key>CFBundleDisplayName</key><string>Sutando</string>
  <key>CFBundleExecutable</key><string>Sutando</string>
  <key>CFBundleIdentifier</key><string>com.sutando.menubar</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSUIElement</key><true/>
  <key>NSAppleEventsUsageDescription</key>
  <string>Sutando reads your Finder selection to drop files into the agent task queue.</string>
</dict>
</plist>
PLIST
cp "$BIN" "$APP/Contents/MacOS/Sutando"
echo "  ✓ $APP"

# Prefer a stable identity so the Accessibility grant survives rebuilds (cdhash
# churn); the designated requirement is identifier-only for the same reason.
SIGN_ID="$(security find-identity -v -p codesigning 2>/dev/null | awk '/"Sutando Dev"/{print $2; exit}')"
if [ -n "$SIGN_ID" ]; then
  codesign --force --sign "$SIGN_ID" --identifier com.sutando.menubar \
    --requirements '=designated => identifier "com.sutando.menubar"' "$APP" 2>/dev/null \
    || codesign --force --sign - "$APP" 2>/dev/null || true
  echo "  ✓ signed (Sutando Dev + identifier-only designated requirement)"
else
  codesign --force --sign - "$APP" 2>/dev/null || true
  echo "  ✓ signed (ad-hoc — install a \"Sutando Dev\" cert for a TCC grant that survives rebuilds)"
fi

if [ "$SUPERVISE" -eq 1 ]; then
  bash "$REPO/src/install-sutando-app-launchd.sh"
elif [ "$LAUNCH" -eq 1 ]; then
  pkill -x Sutando 2>/dev/null || true
  open "$APP"
  echo "  ✓ launched"
fi

cat <<EOF

Done. The core stays headless — this app is opt-in and separate.
  launch now        : open "$APP"
  auto-start at login: bash scripts/install-menu-bar-app.sh --supervise
  first run         : grant Accessibility in System Settings > Privacy & Security
EOF
