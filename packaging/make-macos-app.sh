#!/usr/bin/env bash
#
# Assemble "AgentDuet Desktop.app" — the Swift shell as the app, the PyInstaller binary as the
# service it starts.
#
# WHY A SCRIPT AND NOT AN XCODE TARGET. The bundle has two executables in it, one of which is
# produced by a completely different toolchain on a different step of the build. Expressing that
# in an .xcodeproj means a generated file nobody here can open to fix. Twenty lines of `cp` are
# reviewable, and CI is the only machine that runs them.
#
# Usage:  packaging/make-macos-app.sh <swift-binary> <daemon-binary> <output-dir>
set -euo pipefail

SHELL_BIN="${1:?swift binary}"
DAEMON_BIN="${2:?pyinstaller binary}"
OUT_DIR="${3:-dist-bin}"

APP="$OUT_DIR/AgentDuet Desktop.app"
VERSION="$(grep -m1 '^version' "$(dirname "$0")/../pyproject.toml" | cut -d'"' -f2)"
VERSION="${VERSION:-0.1.0}"

# REFUSE TO EAT YOUR OWN INPUT. This removes $APP before writing it, and since macOS went
# --onedir the daemon handed in IS a bundle — so being pointed at the output directory would
# delete the source mid-run and leave a shell wrapped around nothing.
# EXACTLY the bundle we are about to delete, not merely something near it. The first version of
# this check refused anything under $OUT_DIR and so rejected the very workaround it recommends:
# build.yml stages the daemon as a sibling, INSIDE the output directory, which is fine.
_abs() { ( cd "$(dirname "$1")" 2>/dev/null && printf '%s/%s' "$(pwd)" "$(basename "$1")" ); }
if [ "$(_abs "$DAEMON_BIN")" = "$(_abs "$APP")" ]; then
  echo "error: the daemon and the output are the same bundle ('$APP')." >&2
  echo "  This script deletes it before writing, so that would destroy the input." >&2
  echo "  Move it aside first — build.yml stages it as pyinstaller-stage.app." >&2
  exit 1
fi

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# The shell is the app. `CFBundleExecutable` must match this name exactly.
cp "$SHELL_BIN" "$APP/Contents/MacOS/AgentDuet Desktop"
# The daemon rides ALONGSIDE it, which is where Daemon.swift looks. Contents/MacOS rather than
# Resources because it is an executable, and codesign treats the two directories differently —
# a Mach-O under Resources is a signing error, not a preference.
# TWO SHAPES, and the onedir one has a trap that cost a launch to find.
#
# A onefile binary is a single file — copy it and be done.
#
# A macOS onedir build must be handed as the .app PYINSTALLER BUILT, not as its COLLECT
# directory. The bootloader notices it is running inside a bundle (its own path contains
# Contents/MacOS) and then loads Python from ../Frameworks — so the libraries have to be in
# Contents/Frameworks, NOT in the _internal/ sibling that the COLLECT tree ships. Hand it the
# COLLECT tree and it launches, finds nothing, and dies with:
#   Failed to load Python shared library '.../Contents/Frameworks/libpython3.12.dylib'
# which reads like a broken build rather than a mis-assembled bundle.
if [ -f "$DAEMON_BIN" ]; then
  cp "$DAEMON_BIN" "$APP/Contents/MacOS/agentduet-desktop"
elif [ -d "$DAEMON_BIN/Contents/MacOS" ]; then
  cp "$DAEMON_BIN/Contents/MacOS/agentduet-desktop" "$APP/Contents/MacOS/agentduet-desktop"
  # Frameworks and Resources come across as-is; the daemon resolves both relative to Contents.
  for _d in Frameworks Resources; do
    [ -d "$DAEMON_BIN/Contents/$_d" ] || continue
    mkdir -p "$APP/Contents/$_d"
    cp -R "$DAEMON_BIN/Contents/$_d"/. "$APP/Contents/$_d/"
  done
else
  echo "error: '$DAEMON_BIN' is a directory but not a .app bundle." >&2
  echo "  On macOS pass the .app PyInstaller built — dist-bin/AgentDuet Desktop.app — and not" >&2
  echo "  its COLLECT directory: the daemon loads Python from Contents/Frameworks." >&2
  exit 1
fi
# APPLE'S STT HELPER, when it was built beside the shell. Found rather than passed, because it
# comes out of the same `swift build` and a third argument that is almost always "the obvious
# sibling" is a third argument to get wrong. Absent, transcribe.py falls back to Whisper — which
# is also what happens in the pywebview build, where no Swift is compiled at all.
_stt="$(dirname "$SHELL_BIN")/AgentDuetSTT"
if [ -f "$_stt" ]; then
  cp "$_stt" "$APP/Contents/MacOS/agentduet-stt"
  chmod +x "$APP/Contents/MacOS/agentduet-stt"
  echo "  $APP/Contents/MacOS/agentduet-stt"
fi

chmod +x "$APP/Contents/MacOS/AgentDuet Desktop" "$APP/Contents/MacOS/agentduet-desktop"

# ---- the icon ------------------------------------------------------------------------------
#
# GENERATED, NOT COMMITTED. One source of truth is src/agentduet_desktop/logo.png, which the
# pages also serve as their mark and favicon — a checked-in .icns is a second copy that silently
# stops matching the first.
#
# Until now the bundle declared NO icon at all, so Finder, the Dock and the DMG window all showed
# the blank generic application icon. That reads as a broken or untrusted download, which is an
# expensive first impression for something a tester was asked to install.
#
# SQUARE, PADDED TRANSPARENT — and the reason changed on 2026-09-18. This padded with WHITE,
# correctly at the time: the mark arrived on an opaque white ground, so a transparent pad would
# have left a white rectangle floating inside a transparent square. The ground is transparent
# now, so white padding would be the only thing MAKING it a square, and macOS applies no mask to
# an .icns — whatever shape the art has is the shape the icon has.
#
# STILL NOT RIGHT, and the checklist keeps the item open: macOS convention is a rounded squircle
# with the art inset, and the source is 306x222, which cannot fill 1024 without going soft. A
# floating mark beats a hard white square; a designed 1024x1024 master beats both.
LOGO="$(cd "$(dirname "$0")/.." && pwd)/src/agentduet_desktop/logo.png"
if [ -f "$LOGO" ] && command -v iconutil >/dev/null 2>&1; then
  ICONSET="$(mktemp -d)/AgentDuet.iconset"
  mkdir -p "$ICONSET"
  SQUARE="$(mktemp -d)/square.png"
  # The long edge decides the canvas, so nothing is cropped.
  LONG=$(sips -g pixelWidth -g pixelHeight "$LOGO" | awk '/pixel/ {print $2}' | sort -rn | head -1)
  # No --padColor: sips keeps the alpha channel and pads with transparency.
  sips -p "$LONG" "$LONG" "$LOGO" --out "$SQUARE" >/dev/null 2>&1
  for SZ in 16 32 64 128 256 512; do
    sips -z "$SZ" "$SZ" "$SQUARE" --out "$ICONSET/icon_${SZ}x${SZ}.png" >/dev/null 2>&1
    DBL=$((SZ * 2))
    sips -z "$DBL" "$DBL" "$SQUARE" --out "$ICONSET/icon_${SZ}x${SZ}@2x.png" >/dev/null 2>&1
  done
  if iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AgentDuet.icns" 2>/dev/null; then
    echo "  icon:  Contents/Resources/AgentDuet.icns"
  else
    echo "  icon:  iconutil refused the iconset — shipping without an icon"
  fi
  rm -rf "$(dirname "$ICONSET")" "$(dirname "$SQUARE")"
else
  echo "  icon:  no logo.png or no iconutil — shipping without an icon"
fi

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>AgentDuet Desktop</string>
  <key>CFBundleDisplayName</key><string>AgentDuet Desktop</string>
  <key>CFBundleIdentifier</key><string>com.b3networks.agentduet-desktop</string>
  <key>CFBundleExecutable</key><string>AgentDuet Desktop</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <!-- Named WITHOUT the extension, which is what CFBundleIconFile expects; macOS appends
       .icns. A missing file here is not an error at build time and shows as the generic
       icon at runtime, so the generator above says out loud when it could not produce one. -->
  <key>CFBundleIconFile</key><string>AgentDuet</string>
  <key>CFBundleShortVersionString</key><string>${VERSION}</string>
  <key>CFBundleVersion</key><string>${VERSION}</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <!-- A MENU BAR APP, not a Dock app. It answers the phone while the owner is away, so "no
       window, still running" is its normal state — and a Dock icon makes it look like a
       document app that ought to be quit when you are done reading.
       THIS IS ONLY SAFE BECAUSE THE SHELL BUILDS AN NSStatusItem. Setting it in a bundle
       without one leaves a running service the owner cannot reach: no Dock icon, no menu bar
       item, nothing to click. Which is why the PyInstaller bundle in
       packaging/agentduet-desktop.spec keeps this FALSE — pywebview has no status item. -->
  <key>LSUIElement</key><true/>
  <!-- THE IN-APP PHONE answers a call through the page's microphone. Without this string
       macOS KILLS the app the first time anything asks for the microphone. -->
  <key>NSMicrophoneUsageDescription</key><string>AgentDuet uses the microphone when you answer a call in the app.</string>
  <!-- The window loads http://127.0.0.1. Loopback is the ONE exemption ATS grants by name;
       without this key a debug build can still be refused, and NSAllowsArbitraryLoads would
       buy the same thing by switching the policy off everywhere. -->
  <key>NSAppTransportSecurity</key>
  <dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict>
</plist>
PLIST

echo "built: $APP"
find "$APP" -type f | sed "s|^|  |"
