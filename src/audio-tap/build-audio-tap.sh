#!/bin/bash
# Builds and ad-hoc-signs sys-audio-tap. The Info.plist is embedded directly
# into the binary (__TEXT,__info_plist) so the TCC prompt works without an .app bundle.
set -euo pipefail
cd "$(dirname "$0")"

PLIST=$(mktemp -t audio-tap-plist)
cat > "$PLIST" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key><string>com.sutando.sys-audio-tap</string>
    <key>CFBundleName</key><string>sys-audio-tap</string>
    <key>NSAudioCaptureUsageDescription</key>
    <string>Sutando's screen recorder captures system audio for your recordings. Speakers and volume keys are unaffected.</string>
</dict>
</plist>
EOF

swiftc -O sys-audio-tap.swift -o sys-audio-tap \
    -framework CoreAudio -framework AVFoundation \
    -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker "$PLIST"
codesign --force -s - sys-audio-tap
rm -f "$PLIST"
echo "built: $(pwd)/sys-audio-tap"
