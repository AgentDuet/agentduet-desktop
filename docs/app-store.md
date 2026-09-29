# The Mac App Store — what it takes

Started 2026-09-29 (Stanley). The Developer ID DMG stays the way betas ship; this records what an
App Store build needs, what a sandboxed spike has **proven**, and what is still only expected.

Run the spike: `packaging/appstore-spike.sh --run`. It builds this checkout, signs it the App Store
way (sandbox on, no hardened runtime) with the Developer ID, as `AgentDuet Sandbox` under its own
bundle id `com.b3networks.agentduet-desktop.sandbox`, and opens it. Its data lives in
`~/Library/Containers/com.b3networks.agentduet-desktop.sandbox/Data/.agentduet-desktop`, because a
sandboxed process's home directory IS its container. Nothing it does touches the real instance.

## Proven in the spike (2026-09-29, M5, macOS 26.6)

| | Result |
|---|---|
| The Swift shell launching the bundled daemon as a helper | Works, with the helper signed `app-sandbox` + `inherit` only |
| The page server the window loads (loopback) | Works with `network.server` — **after one fix** (below) |
| Network out: GitHub, Hugging Face | Works with `network.client` |
| Gemma 4 E4B on Metal (llama.cpp) | Loads and answers |
| Qwen3-ASR on Metal | Loads and transcribes both sides of a real call, same text as outside |
| **No hardened runtime, none of the runtime exceptions** (JIT, unsigned memory, library validation off, dyld variables) | All of the above still works. They are not needed for an App Store build |

**The one fix it needed.** The page server did not start at all: `aiohttp` builds its MIME table
at import, and Python's `mimetypes` reads `/etc/apache2/mime.types`. The sandbox forbids that read,
and a forbidden file raises where a missing one is skipped, so the import failed and the window
had nothing to load. `entry.py` now drops the MIME files the process cannot open before anything
imports `aiohttp`. Checked by opening the file: `os.access` reports the Unix permissions, which
allow it, and the first version of the fix kept the very file it failed on.

The entitlements are in `packaging/entitlements-appstore.plist` (the app) and
`packaging/entitlements-appstore-helper.plist` (the two helpers).

## Must change for the App Store (known)

- **Where the files live.** `~/.agentduet-desktop` becomes the container. A Developer ID owner who
  moves to the App Store build starts empty unless we migrate — and the sandboxed build cannot read
  the old folder to migrate it without the owner choosing it.
- **The Documents default.** Inside the sandbox "Documents" is the container's own
  (`…/Containers/…/Data/Documents/AgentDuet`), so the Permissions step would "succeed" and put
  recordings where the owner never looks. The App Store build needs the owner to PICK the folder
  once, remembered with a security-scoped bookmark — and the Permissions step changes with it.
- **The folder picker and opening links.** `reveal.pick_folder` runs `osascript`, and `links._open`
  runs `open`. A folder chosen through `osascript` grants access to osascript, not to us, so the
  pick has to move into the Swift shell (`NSOpenPanel`), and links should go through
  `NSWorkspace` there too. Expected to fail; not tried, because both put something on screen.
- **The update check.** An App Store app is updated by the store; the check against GitHub and
  the "newer version" notice come out of that build.
- **Sign in with Apple** (App Review 4.8): offering Google sign-in requires an equivalent
  privacy option. Asked of the auth service on #ai-product, 2026-09-29.

## Not yet tried

The microphone in the window (entitlement present; needs a call), Start at Login being switched on,
Google sign-in and its loopback callback, and the connector connecting — the last needs a
connector that is not the one the dev app uses, since one connector has one client.

## Submitting (mechanical, not started)

Apple Distribution + Mac Installer certificates and a `.pkg` (`productbuild`), not the notarized
DMG; an App Store Connect record with privacy labels, a privacy policy URL, the export-compliance
answer, screenshots and category (`LSApplicationCategoryType`); a full icon set with the 1024 px
master (the icon item on the CLAUDE.md checklist becomes a hard requirement); upload through
Transporter, whose checks include a private-API scan — the unknown for a bundled Python runtime.
App Review needs a way in: a demo sign-in and, since a reviewer cannot place a call to a trunk, a
video of a call.
