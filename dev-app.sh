#!/usr/bin/env bash
# The NATIVE WINDOW, running the daemon FROM SOURCE. Use this instead of a browser when testing
# on a Mac — it is what an owner actually sees.
#
# `dev.sh` serves the pages to a browser, which is quick but is not the product: Chrome grants a
# microphone that the signed app has to earn through an entitlement, a usage string and a web
# view permission handler, and a browser test proves none of the three. This builds the real
# Swift shell, wraps it in a real .app, and hands it a daemon that is a two-line script running
# `src/` — so the window, the menu bar item and the macOS permissions are the shipped ones, and
# only the daemon is not frozen.
#
# SAME ITERATION SPEED FOR PAGES: they are read per request, so an HTML change is a reload
# (Cmd-R in the window), with no rebuild. A Python change needs this script again (~20s, most of
# it the Swift build, which is incremental after the first).
#
# SIGNED WITH THE HARDENED RUNTIME AND THE REAL ENTITLEMENTS, on purpose: the runtime is what
# refuses a microphone without `device.audio-input`, so signing without it would test a looser
# app than the one we ship. Developer ID when the Mac has it, ad hoc otherwise — see below.
#
# ITS OWN BUNDLE ID (`.dev`), so macOS keeps its microphone grant and login item apart from the
# installed app's. An ad-hoc signature changes every build, so macOS may ask for the microphone
# again after a rebuild — that is the dev bundle, not a bug in the product.
#
# Same $AGENTDUET_HOME as the installed app and dev.sh, so it is the owner's real instance. For
# a throwaway, prefix with AGENTDUET_HOME=/tmp/whatever SECRETARY_WEB_PORT=8901.
#
# THE RECORDER EDITION: `EDITION=recorder ./dev-app.sh` builds "AgentDuet Recorder Dev" — the shell
# compiled with -D RECORDER, the daemon told AGENTDUET_EDITION=recorder, and its own instance at
# ~/.agentduet-recorder (src/agentduet_desktop/edition.py). The AI modules are still on disk here,
# so this shows the recorder's UI; tests/test_recorder.py is what proves it runs without them.
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"

VENV=.venv-build
[ -x "$VENV/bin/python" ] || { echo "no $VENV — see CLAUDE.md Build"; exit 1; }

EDITION="${EDITION:-full}"
if [ "$EDITION" = "recorder" ]; then
  NAME="AgentDuet Recorder Dev"; BUNDLE="com.b3networks.agentduet-recorder.dev"
  SWIFT_FLAGS=(-Xswiftc -DRECORDER --product AgentDuetShell --build-path .build-recorder)
  BUILT=macos/.build-recorder/release/AgentDuetShell
elif [ "$EDITION" = "ai" ]; then
  NAME="AgentDuet AI Dev"; BUNDLE="com.b3networks.agentduet-ai.dev"
  SWIFT_FLAGS=(-Xswiftc -DAI_ONLY --build-path .build-ai)
  BUILT=macos/.build-ai/release/AgentDuetShell
else
  NAME="AgentDuet Dev"; BUNDLE="com.b3networks.agentduet-desktop.dev"
  SWIFT_FLAGS=()
  BUILT=macos/.build/release/AgentDuetShell
fi

# STOP WHATEVER IS SERVING FIRST. The shell ATTACHES to a daemon that already answers rather
# than starting its own, so with the installed app or a dev.sh daemon still up, this window
# would show THAT code — the confusion this script exists to remove.
# Apps FIRST (each stops the daemon it started), then any daemon left over. "if running", because
# a bare `quit app` launches an app that is not running just to quit it.
# WHAT THIS ONE CLASHES WITH. The full app and the recorder share port 8899 (and a phone line),
# so either replaces the other. AgentDuet AI runs BESIDE the recorder — its own port, no line —
# so it replaces only its own.
if [ "$EDITION" = "ai" ]; then
  APPS=("AgentDuet AI" "AgentDuet AI Dev"); STOP=(ai)
else
  APPS=("AgentDuet Desktop" "AgentDuet Dev" "AgentDuet Recorder" "AgentDuet Recorder Dev")
  STOP=(full recorder)
fi
for a in "${APPS[@]}"; do
  osascript -e "if application \"$a\" is running then tell application \"$a\" to quit" \
    2>/dev/null || true
done
# An edition's daemon only if it has an instance: importing the package seeds one where none is.
for e in "${STOP[@]}"; do
  case "$e" in recorder) h=.agentduet-recorder ;; ai) h=.agentduet-ai ;; *) h=.agentduet-desktop ;; esac
  [ "$e" != "full" ] && [ ! -d "$HOME/$h" ] && continue
  AGENTDUET_EDITION=$e PYTHONPATH=src "$VENV/bin/python" -m agentduet_desktop.cli stop \
    2>/dev/null || true
done

(cd macos && swift build -c release -Xswiftc -warnings-as-errors ${SWIFT_FLAGS[@]+"${SWIFT_FLAGS[@]}"}) | tail -1

# The daemon, as the shell expects to find it beside itself: an executable named
# `agentduet-desktop`. `exec`, so the pid the shell holds IS the daemon and Quit stops it.
# THE SIGN-IN SERVICE. A signed-in install cannot connect without it, and the native window does
# not inherit a terminal's environment. The product still ships it unset — `oauth.py` refuses to
# hardcode an endpoint — so this default lives in the DEV script only, and it is the public
# production address (CLAUDE.md, "Single sign-on"). Override by exporting it before running.
OAUTH="${AGENTDUET_OAUTH_URL:-https://auth.agentduet.com}"
STAGE="$(mktemp -d)"
cat > "$STAGE/agentduet-desktop" <<EOF
#!/bin/bash
export PYTHONPATH="$ROOT/src\${PYTHONPATH:+:\$PYTHONPATH}"
export AGENTDUET_OAUTH_URL="$OAUTH"
export AGENTDUET_EDITION="$EDITION"
exec "$ROOT/$VENV/bin/python" -m agentduet_desktop.cli "\$@"
EOF
chmod +x "$STAGE/agentduet-desktop"

OUT=dist-dev
# NAMED AND IDENTIFIED BY THE SCRIPT, with its own bundle id so macOS keeps its permissions and
# login item apart from the installed app's.
EDITION="$EDITION" APP_NAME="$NAME" BUNDLE_ID="$BUNDLE" \
  packaging/make-macos-app.sh "$BUILT" "$STAGE/agentduet-desktop" "$OUT" >/dev/null
APP="$OUT/$NAME.app"

# WITH THE DEVELOPER ID WHEN THIS MAC HAS IT, ad hoc otherwise. Two reasons it matters here and
# not only for release: macOS ties an ad-hoc app's microphone grant to that exact build, so every
# rebuild asks again; and WebKit captures in a helper process that must prove whose app it is
# working for, which an ad-hoc identity may not satisfy. The keychain is the one sign-macos.sh
# builds; no timestamp, since nothing here is notarized.
KEYCHAIN="$HOME/Library/Keychains/agentduet-signing.keychain-db"
IDENTITY=""
if [ -f "$KEYCHAIN" ] && [ -f "$HOME/.apple-signing/keychain-pw" ]; then
  security unlock-keychain -p "$(cat "$HOME/.apple-signing/keychain-pw")" "$KEYCHAIN"
  IDENTITY=$(security find-identity -v -p codesigning "$KEYCHAIN" \
             | awk '/Developer ID Application/ {print $2; exit}')
fi
codesign --force --deep --options runtime --timestamp=none \
  --entitlements packaging/entitlements.plist --sign "${IDENTITY:--}" \
  ${IDENTITY:+--keychain "$KEYCHAIN"} "$APP" 2>&1 | grep -v "replacing existing signature" || true
if [ -n "$IDENTITY" ]; then echo "  signed: Developer ID"; else echo "  signed: ad hoc"; fi

open "$APP"
echo "  $NAME is up — the native window, daemon from source"
case "$EDITION" in recorder) _home=.agentduet-recorder ;; ai) _home=.agentduet-ai ;; *) _home=.agentduet-desktop ;; esac
echo "  log: ${AGENTDUET_HOME:-$HOME/$_home}/run/daemon-start.log"
