#!/usr/bin/env bash
set -euo pipefail

# Build script for macOS.
# Expected files in the same folder:
#   audio_splitter_mac.py
#   ffmpeg
#   ffprobe

cd "$(dirname "$0")"

APP_NAME="Audio Splitter"
SCRIPT_NAME="audio_splitter_mac.py"

if ! command -v python3.10 >/dev/null 2>&1; then
  echo "python3.10 not found. Install Python 3.10 first."
  exit 1
fi

if [[ ! -f "$SCRIPT_NAME" ]]; then
  echo "$SCRIPT_NAME not found in this folder."
  exit 1
fi

if [[ ! -f "ffmpeg" || ! -f "ffprobe" ]]; then
  echo "ffmpeg and/or ffprobe not found in this folder. Put both files next to this build script."
  exit 1
fi

chmod +x ffmpeg ffprobe

python3.10 -m pip install --upgrade pip pyinstaller
rm -rf build dist "$APP_NAME.spec"

python3.10 -m PyInstaller \
  --noconfirm \
  --clean \
  --windowed \
  --name "$APP_NAME" \
  "$SCRIPT_NAME"

APP_MACOS_DIR="dist/$APP_NAME.app/Contents/MacOS"
if [[ ! -d "$APP_MACOS_DIR" ]]; then
  echo "Build finished, but $APP_MACOS_DIR was not found. Check PyInstaller output."
  exit 1
fi

cp ffmpeg "$APP_MACOS_DIR/ffmpeg"
cp ffprobe "$APP_MACOS_DIR/ffprobe"
chmod +x "$APP_MACOS_DIR/ffmpeg" "$APP_MACOS_DIR/ffprobe"

echo "Done: dist/$APP_NAME.app"
echo "For a user, you can distribute the app bundle: dist/$APP_NAME.app"
echo "If you do not sign/notarize it, macOS may show a security warning on another Mac."
