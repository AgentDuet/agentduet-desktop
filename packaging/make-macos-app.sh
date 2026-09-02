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

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# The shell is the app. `CFBundleExecutable` must match this name exactly.
cp "$SHELL_BIN" "$APP/Contents/MacOS/AgentDuet Desktop"
# The daemon rides ALONGSIDE it, which is where Daemon.swift looks. Contents/MacOS rather than
# Resources because it is an executable, and codesign treats the two directories differently —
# a Mach-O under Resources is a signing error, not a preference.
# EITHER SHAPE. macOS builds are --onedir, so PyInstaller emits a DIRECTORY holding the
# executable plus _internal/. Copy its CONTENTS in, so the executable keeps _internal as a
# sibling — the layout onedir needs — and so Contents/MacOS/agentduet-desktop is still the
# executable Daemon.swift launches. A onefile binary (Linux, or an older build) copies as before.
if [ -d "$DAEMON_BIN" ]; then
  cp -R "$DAEMON_BIN"/. "$APP/Contents/MacOS/"
else
  cp "$DAEMON_BIN" "$APP/Contents/MacOS/agentduet-desktop"
fi
chmod +x "$APP/Contents/MacOS/AgentDuet Desktop" "$APP/Contents/MacOS/agentduet-desktop"

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
  <key>CFBundleShortVersionString</key><string>${VERSION}</string>
  <key>CFBundleVersion</key><string>${VERSION}</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <!-- It answers the phone while the owner is away, so it must not be culled for having no
       visible window. -->
  <key>LSUIElement</key><false/>
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
