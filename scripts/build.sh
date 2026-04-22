#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "Installing build dependencies..."
uv pip install pyinstaller pystray pillow pyobjc-framework-Cocoa

echo "Running tests..."
uv run pytest

echo "Building with PyInstaller..."
uv run pyinstaller mtgcompare.spec --clean --noconfirm

APP="dist/MTG Compare.app"
if [ ! -d "$APP" ]; then
    echo "Build failed — '$APP' not found." >&2
    exit 1
fi

ZIP="dist/mtgcompare-macos.zip"
echo "Zipping to $ZIP..."
rm -f "$ZIP"
(cd dist && zip -r "mtgcompare-macos.zip" "MTG Compare.app")

echo ""
echo "Done: $ZIP"
echo "Note: the app is unsigned — on first launch right-click → Open to bypass Gatekeeper."
echo "Test by running: open \"$APP\""
