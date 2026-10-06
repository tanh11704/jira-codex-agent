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
codesign --force --sign - "$APP_DIR"

echo "Built: $APP_DIR"
