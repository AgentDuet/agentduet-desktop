#!/usr/bin/env bash
#
# Sign, notarize and staple a locally built .app — the same sequence build.yml runs, so that a
# developer with a Mac can produce a shippable DMG without pushing.
#
# WHY THIS EXISTS: CI is the only thing that has ever signed a build, and CI builds the REMOTE.
# So proving a fix works and proving the signed artifact works were two different builds, and
# for a while neither one was both. Locally this is one command.
#
# Usage:  packaging/sign-macos.sh [dist-bin]
#
# Needs, and says so plainly when they are absent:
#   - a "Developer ID Application" identity in a keychain (import the .p12 once)
#   - a notarytool credential profile, default name AGENTDUET_NOTARY_PROFILE or "agentduet"
set -euo pipefail

DIST="${1:-dist-bin}"
APP="$DIST/AgentDuet Desktop.app"
PROFILE="${AGENTDUET_NOTARY_PROFILE:-agentduet}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ENTITLEMENTS="$HERE/entitlements.plist"

[ -d "$APP" ] || { echo "no app at '$APP' — build it first:"; \
  echo "  pyinstaller --noconfirm --distpath $DIST packaging/agentduet-desktop.spec"; exit 1; }
[ -f "$ENTITLEMENTS" ] || { echo "missing $ENTITLEMENTS"; exit 1; }

# A DEDICATED KEYCHAIN, when one exists. The signing key can legitimately sit in several
# keychains at once, and "the first identity that matches" is then decided by search-list order.
# That matters because a copy in the LOGIN keychain raises a GUI password prompt — the login
# keychain keeps its own password, which diverges from the account password whenever that was
# reset outside System Settings — and codesign then hangs waiting for a dialog no script can
# answer. Naming the keychain makes it deterministic, and --keychain below keeps codesign
# looking in the same place find-identity did.
KEYCHAIN="${AGENTDUET_SIGNING_KEYCHAIN:-$HOME/Library/Keychains/agentduet-signing.keychain-db}"
SIGNDIR="$HOME/.apple-signing"

if [ -f "$KEYCHAIN" ] && [ -f "$SIGNDIR/keychain-pw" ]; then
  if security unlock-keychain -p "$(cat "$SIGNDIR/keychain-pw")" "$KEYCHAIN" 2>/dev/null; then
    security set-keychain-settings "$KEYCHAIN" 2>/dev/null      # no auto-lock while we work
  else
    # REBUILD RATHER THAN PROMPT. If the stored password no longer opens it — seen after a macOS
    # update — codesign would raise a GUI dialog asking for a random string nobody knows, and
    # then fail errSecInternalComponent. Everything the keychain holds came from $SIGNDIR and is
    # still there, so throwing it away and remaking it costs a second and needs no human.
    echo "the signing keychain would not unlock — rebuilding it from $SIGNDIR"
    security delete-keychain "$KEYCHAIN" 2>/dev/null || rm -f "$KEYCHAIN"
  fi
fi

# BUILD THE KEYCHAIN IF IT IS NOT THERE, from the key and certificate in ~/.apple-signing. Doing
# it here rather than by hand matters because two of the steps are not guessable:
#
#  - set-key-partition-list. Without it codesign raises a GUI prompt for keychain access and the
#    run HANGS rather than failing, so it looks like a slow build.
#  - the key must NOT also live in the login keychain. With a copy in both, Security resolves the
#    private key from the login keychain whatever --keychain says, and the login keychain has its
#    own password — which diverges from the account password whenever that was reset outside
#    System Settings. The symptom is a password dialog nobody knows the answer to, followed by
#    errSecInternalComponent on every file. Remove the duplicate:
#        security delete-identity -Z <sha1> ~/Library/Keychains/login.keychain-db
if [ ! -f "$KEYCHAIN" ] && [ -f "$SIGNDIR/devid.key" ] && [ -f "$SIGNDIR/developerID_application.cer" ]; then
  echo "no signing keychain yet — creating one from $SIGNDIR"
  PWFILE="$SIGNDIR/keychain-pw"
  # Kept, so a later re-sign needs no rediscovery. It is no weaker than what it protects: the key
  # is beside it at mode 600, so this keychain lets codesign USE the key, it is not a second
  # boundary around it.
  ( umask 077; [ -f "$PWFILE" ] || openssl rand -base64 24 > "$PWFILE" )
  KCPW=$(cat "$PWFILE")
  security create-keychain -p "$KCPW" "$KEYCHAIN"
  security set-keychain-settings "$KEYCHAIN"        # no auto-lock, no lock on sleep
  security unlock-keychain -p "$KCPW" "$KEYCHAIN"
  security import "$SIGNDIR/devid.key" -k "$KEYCHAIN" -T /usr/bin/codesign >/dev/null
  security import "$SIGNDIR/developerID_application.cer" -k "$KEYCHAIN" -T /usr/bin/codesign >/dev/null
  security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$KCPW" "$KEYCHAIN" >/dev/null
  # Append to the search list without dropping what was already on it.
  security list-keychains -d user -s $(security list-keychains -d user | tr -d '"' | xargs) "$KEYCHAIN"
fi

# UNLOCK IT FIRST, every time. A keychain relocks on reboot, and a locked one makes codesign
# raise a GUI password prompt — for the keychain's OWN password, which is the random string in
# keychain-pw and nothing like the owner's login password, so the dialog is unanswerable by
# anyone who did not create it. This script created it and stored that password; not using it
# was the gap. It went unnoticed because a keychain is unlocked at the moment it is made, so the
# first run after setup always worked and the second one after a reboot did not.
if [ -f "$KEYCHAIN" ]; then
  KC_ARGS=(--keychain "$KEYCHAIN")
  FIND_IN=("$KEYCHAIN")
  echo "using keychain $KEYCHAIN"
else
  KC_ARGS=()
  FIND_IN=()
fi

# Ask the keychain which identity it holds rather than rebuilding its name from the team id: the
# name embeds the legal entity, and a rename would break this for a reason nobody would guess.
IDENTITY=$(security find-identity -v -p codesigning ${FIND_IN[@]+"${FIND_IN[@]}"} \
           | awk '/Developer ID Application/ {print $2; exit}')
if [ -z "$IDENTITY" ]; then
  echo "No 'Developer ID Application' identity in any keychain, so there is nothing to sign with."
  echo "Import the certificate once (it carries the private key):"
  echo "    open ~/.apple-signing/devid.p12      # Keychain Access prompts for its password"
  echo "Then check it landed:  security find-identity -v -p codesigning"
  exit 1
fi
echo "signing with identity $IDENTITY"

# SWEEP UP AFTER A KILLED RUN FIRST. codesign writes <name>.cstemp while it works and removes
# it on success; interrupt it and the stub survives. The next run's `find` then lists a file
# that codesign refuses and xargs reports non-zero — which, under `set -e`, aborts signing
# halfway through with a message about a file nobody asked to sign.
find "$APP" -name "*.cstemp" -delete 2>/dev/null || true

# INNER BINARIES FIRST. A bundle's signature covers its contents, so signing the outer .app
# before its dylibs invalidates the outer one. `--deep` looks like the shortcut and is deprecated
# by Apple precisely because it applies the same entitlements to nested code that should not have
# them.
find "$APP" -type f \( -name "*.dylib" -o -name "*.so" -o -perm +111 \) -print0 \
  | xargs -0 -I{} codesign --force --options runtime --timestamp ${KC_ARGS[@]+"${KC_ARGS[@]}"} \
      --entitlements "$ENTITLEMENTS" --sign "$IDENTITY" {}

codesign --force --options runtime --timestamp ${KC_ARGS[@]+"${KC_ARGS[@]}"} \
  --entitlements "$ENTITLEMENTS" --sign "$IDENTITY" "$APP"

# --strict, because a signature that merely exists is not one Gatekeeper accepts.
codesign --verify --deep --strict --verbose=2 "$APP"
echo "signature verified"

# SIZE THE IMAGE EXPLICITLY. hdiutil's own estimate is too tight for an app this size and the
# failure reads as a disk problem on the machine rather than an undersized image.
# STAGE THE VOLUME, don't hand hdiutil the .app directly. A drag install needs somewhere to
# drag TO: without an /Applications alias in the window, INSTALL.md's "drag AgentDuet Desktop to
# your Applications folder" asks the owner to open a second Finder window and work out where it
# goes. The alias is the affordance every Mac user already recognises.
#
# `ditto` rather than `cp -R` for the bundle: it preserves the extended attributes and the code
# signature properly, which is the difference between a notarizable app and one that fails
# verification for a reason the message will not name.
MB=$(du -sm "$APP" | cut -f1)
DMG="agentduet-desktop-macos-arm64.dmg"
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
ditto "$APP" "$STAGE/$(basename "$APP")"
ln -s /Applications "$STAGE/Applications"
echo "app is ${MB} MB; creating a $((MB + 200)) MB image"
hdiutil create -volname "AgentDuet Desktop" -srcfolder "$STAGE" \
  -size $((MB + 200))m -ov -format UDZO "$DMG" >/dev/null
codesign --force --timestamp ${KC_ARGS[@]+"${KC_ARGS[@]}"} --sign "$IDENTITY" "$DMG"

if ! xcrun notarytool history --keychain-profile "$PROFILE" >/dev/null 2>&1; then
  echo
  echo "Signed '$DMG', but there is no notarytool profile '$PROFILE', so it is NOT notarized."
  echo "A user opening it would still be refused. Store the credential once:"
  echo "    xcrun notarytool store-credentials $PROFILE \\"
  echo "        --key ~/.apple-signing/asc.p8 --key-id <KEY_ID> --issuer <ISSUER_ID>"
  echo "  or, with no API key at all:"
  echo "    xcrun notarytool store-credentials $PROFILE \\"
  echo "        --apple-id <apple-id> --team-id 98EXQNMN6C"
  exit 1
fi

# DO NOT TRUST THE EXIT CODE ALONE: `submit --wait` has returned 0 on a submission that came back
# Invalid, so the printed status is the only reliable signal.
xcrun notarytool submit "$DMG" --keychain-profile "$PROFILE" --wait --timeout 30m 2>&1 | tee notary.txt
grep -q "status: Accepted" notary.txt || {
  ID=$(awk '/id: / {print $2; exit}' notary.txt)
  echo "notarization was NOT accepted — full log follows"
  [ -n "$ID" ] && xcrun notarytool log "$ID" --keychain-profile "$PROFILE"
  exit 1
}

# Notarization is Apple recording that it scanned the build. STAPLING attaches the result to the
# file — without it a first launch needs a working network to check, and a tester on a bad
# connection sees the warning we paid to remove.
xcrun stapler staple "$DMG"
xcrun stapler validate "$DMG"
# What a user's Mac will actually decide, asked the way Gatekeeper asks it.
spctl -a -t open --context context:primary-signature -vv "$DMG"
echo
echo "done: $DMG is signed, notarized and stapled"
