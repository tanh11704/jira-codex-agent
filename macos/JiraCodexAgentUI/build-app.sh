#!/bin/zsh
set -euo pipefail

SCRIPT_DIR=${0:A:h}
BUILD_DIR="$SCRIPT_DIR/.build"
APP_DIR="$SCRIPT_DIR/dist/Jira Codex Agent.app"
CONTENTS="$APP_DIR/Contents"

swift build --package-path "$SCRIPT_DIR" -c release --scratch-path "$BUILD_DIR"

mkdir -p "$CONTENTS/MacOS" "$CONTENTS/Resources"
cp "$BUILD_DIR/release/JiraCodexAgentUI" "$CONTENTS/MacOS/JiraCodexAgentUI"
cp "$SCRIPT_DIR/Info.plist" "$CONTENTS/Info.plist"
/usr/libexec/PlistBuddy -c "Add :AgentProjectDirectory string ${SCRIPT_DIR:h:h}" "$CONTENTS/Info.plist"
cp "$SCRIPT_DIR/Assets/logo.png" "$CONTENTS/Resources/logo.png"
ICONSET="$BUILD_DIR/AppIcon.iconset"
mkdir -p "$ICONSET"
for SIZE in 16 32 128 256 512; do
  sips -z "$SIZE" "$SIZE" "$SCRIPT_DIR/Assets/logo.png" --out "$ICONSET/icon_${SIZE}x${SIZE}.png" >/dev/null
  DOUBLE_SIZE=$((SIZE * 2))
  sips -z "$DOUBLE_SIZE" "$DOUBLE_SIZE" "$SCRIPT_DIR/Assets/logo.png" --out "$ICONSET/icon_${SIZE}x${SIZE}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$CONTENTS/Resources/AppIcon.icns"
codesign --force --sign - "$APP_DIR"

echo "Built: $APP_DIR"
