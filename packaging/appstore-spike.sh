#!/usr/bin/env bash
# The App Store SPIKE: build the real app from this checkout, with Apple's sandbox switched on,
# so what the sandbox breaks can be found by running it rather than guessed.
#
# NOT a release path. It signs with the Developer ID, not the App Store certificate, and makes
# no .pkg — the point is only to run the app sandboxed. It gets its own bundle id
# (`.sandbox`), so macOS keeps its container, permissions and login item apart from the
# installed app's, and its data lives in
#     ~/Library/Containers/com.b3networks.agentduet-desktop.sandbox/Data/.agentduet-desktop
# because a sandboxed process's home directory IS its container.
#
# Usage:  packaging/appstore-spike.sh [--run]
set -euo pipefail
cd "$(dirname "$0")/.."
VENV=.venv-build
OUT=dist-spike
APP="$OUT/AgentDuet Sandbox.app"
[ -x "$VENV/bin/python" ] || { echo "no $VENV — see CLAUDE.md Build"; exit 1; }

# THE CHECKOUT, not a stale install: the spec takes code from src/ but DATA from site-packages
# (CLAUDE.md, "A LOCAL build takes CODE from src/ but DATA from site-packages").
"$VENV/bin/pip" install -q --force-reinstall --no-deps .
rm -rf "$OUT"
"$VENV/bin/pyinstaller" --noconfirm --log-level WARN --distpath "$OUT" packaging/agentduet-desktop.spec
(cd macos && swift build -c release -Xswiftc -warnings-as-errors) | tail -1
mv "$OUT/AgentDuet Desktop.app" "$OUT/pyinstaller-stage.app"
packaging/make-macos-app.sh macos/.build/release/AgentDuetShell "$OUT/pyinstaller-stage.app" "$OUT" >/dev/null
rm -rf "$OUT/pyinstaller-stage.app"
mv "$OUT/AgentDuet Desktop.app" "$APP"
PLIST="$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier com.b3networks.agentduet-desktop.sandbox" \
  -c "Set :CFBundleName AgentDuet Sandbox" "$PLIST"
/usr/libexec/PlistBuddy -c "Set :CFBundleDisplayName AgentDuet Sandbox" "$PLIST" 2>/dev/null || true

# SIGN INSIDE-OUT, after assembling (CLAUDE.md, "sign AFTER assembling"). Libraries with no
# entitlements; the two executables the shell launches as HELPERS, which Apple accepts only with
# exactly app-sandbox + inherit; then the app itself with the sandbox entitlements.
KEYCHAIN="$HOME/Library/Keychains/agentduet-signing.keychain-db"
security unlock-keychain -p "$(cat "$HOME/.apple-signing/keychain-pw")" "$KEYCHAIN"
IDENTITY=$(security find-identity -v -p codesigning "$KEYCHAIN" | awk '/Developer ID Application/ {print $2; exit}')
# NO HARDENED RUNTIME: an App Store build is secured by the sandbox, and signing it this way is
# what showed the runtime exceptions are not needed at all (see entitlements-appstore.plist).
SIGN=(codesign --force --timestamp=none --keychain "$KEYCHAIN" --sign "$IDENTITY")
while IFS= read -r -d '' f; do
  case "$f" in */Contents/MacOS/*) continue;; esac
  # AN `if`, AND NO PIPE (2026-09-29): `file | grep -q X && sign` made the loop's status that of
  # its last file, so a build whose last file was not a binary stopped here under `set -e` — and
  # `grep -q` quitting early could fail the pipe under pipefail, so which build stopped varied.
  # ONE RETRY, AND THE ERROR SAID: a signature here has failed intermittently mid-build and then
  # succeeded by hand, with its error thrown away — so the next one is reported.
  if [[ "$(file -b "$f")" == *Mach-O* ]]; then
    "${SIGN[@]}" "$f" >/dev/null 2>&1 || { sleep 1; err=$("${SIGN[@]}" "$f" 2>&1) \
      || { echo "could not sign $f: $err" >&2; exit 1; }; }
  fi
done < <(find "$APP/Contents" -type f -print0)
for helper in agentduet-desktop agentduet-stt; do
  "${SIGN[@]}" --entitlements packaging/entitlements-appstore-helper.plist \
    "$APP/Contents/MacOS/$helper" 2>&1 | grep -v "replacing existing" || true
done
"${SIGN[@]}" --entitlements packaging/entitlements-appstore.plist "$APP" 2>&1 \
  | grep -v "replacing existing" || true
codesign --verify --deep --strict "$APP"
echo "built and signed, sandboxed: $APP"

# THE SIGN-IN SERVICE, as dev-app.sh supplies it: the product still ships it unset, and a
# sandboxed daemon started through `open` inherits no environment. Written into the CONTAINER's
# own .env, which the daemon loads at startup — a public address, not a secret.
CENV="$HOME/Library/Containers/com.b3networks.agentduet-desktop.sandbox/Data/.agentduet-desktop/.env"
mkdir -p "$(dirname "$CENV")"
grep -q '^AGENTDUET_OAUTH_URL=' "$CENV" 2>/dev/null \
  || echo "AGENTDUET_OAUTH_URL=${AGENTDUET_OAUTH_URL:-https://auth.agentduet.com}" >> "$CENV"

if [ "${1:-}" = "--run" ]; then
  # The shell ATTACHES to whatever already answers the port, so nothing else may be up.
  for a in "AgentDuet Desktop" "AgentDuet Dev" "AgentDuet Sandbox"; do
    osascript -e "if application \"$a\" is running then tell application \"$a\" to quit" 2>/dev/null || true
  done
  sleep 2
  open "$APP"
  echo "running — its log: ~/Library/Containers/com.b3networks.agentduet-desktop.sandbox/Data/.agentduet-desktop/run/daemon-start.log"
fi
