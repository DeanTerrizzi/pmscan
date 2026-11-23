#!/usr/bin/env bash
# Builds dist/pmscan.app. Needs the Xcode Command Line Tools (swiftc) and `uv sync` run in the project.
#   macos/build.sh            build dist/pmscan.app
#   macos/build.sh --install  also copy it to ~/Applications
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$ROOT/dist/pmscan.app"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "Building $APP"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

swiftc -O -parse-as-library \
  -target "$(uname -m)-apple-macos12.0" \
  -o "$APP/Contents/MacOS/pmscan" \
  "$ROOT"/macos/Sources/*.swift

cp "$ROOT/macos/Info.plist" "$APP/Contents/Info.plist"

# App icon
swift "$ROOT/macos/make_icon.swift" "$WORK/icon.png"
ICONSET="$WORK/AppIcon.iconset"
mkdir -p "$ICONSET"
for s in 16 32 128 256 512; do
  sips -z $s $s "$WORK/icon.png" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  sips -z $((s * 2)) $((s * 2)) "$WORK/icon.png" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"

# Tell the app where the scanner lives (it runs `python -m pmscan.cli serve` from here).
PYTHON="$ROOT/.venv/bin/python"
[ -x "$PYTHON" ] || { echo "No virtualenv at $ROOT/.venv: run 'uv sync' first." >&2; exit 1; }
printf '{"project_dir": "%s", "python": "%s"}\n' "$ROOT" "$PYTHON" > "$APP/Contents/Resources/backend.json"

# Ad-hoc signature so macOS will run it locally and allow notifications.
codesign --force --sign - "$APP" >/dev/null

echo "Built $APP"
if [ "${1:-}" = "--install" ]; then
  mkdir -p "$HOME/Applications"
  rm -rf "$HOME/Applications/pmscan.app"
  cp -R "$APP" "$HOME/Applications/pmscan.app"
  echo "Installed to $HOME/Applications/pmscan.app"
fi
