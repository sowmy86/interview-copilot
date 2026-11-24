#!/bin/bash
# Builds the on-device transcription helper (listener/listener).
set -euo pipefail
cd "$(dirname "$0")/listener"
swiftc -O -swift-version 5 -parse-as-library \
  -framework ScreenCaptureKit -framework Speech -framework AVFoundation \
  -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker Info.plist \
  listener.swift -o listener
codesign --force --sign - --identifier local.interview-copilot.listener listener 2>/dev/null || true
echo "built $(pwd)/listener"

cd ../overlay
swiftc -O -swift-version 5 -framework AppKit -framework WebKit -framework Carbon \
  overlay.swift -o overlay 2>&1 | grep -E "error" || true
codesign --force --sign - --identifier local.interview-copilot.overlay overlay 2>/dev/null || true
echo "built $(pwd)/overlay"
