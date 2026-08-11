# Onboarding gap — the six-step flow vs. this package

The onboarding flow (August 2026) describes six steps from download to a working test call, and
claims three things are removed from the old path: the hosting decision, the manual model-key
copy-paste, and the wait on a person to issue credentials.

**We removed hosting. The other two are still there, and neither is ours to remove.**

This file is the per-step reckoning. It is written to stay honest about which side of the line
each gap sits on, because the flow's strongest claims land exactly where this package has the
least control.

| # | Step | State |
|---|---|---|
| 1 | Download package | **partial** — builds exist, not for every platform, and unsigned |
| 2 | Install & run on PC | **done** |
| 3 | OAuth creates the API key | **missing** — platform side |
| 4 | OAuth auto-links the model key | **missing** — platform side |
| 5 | Verify WhatsApp in the application | **missing** — needs a per-owner number |
| 6 | Call in to test | **partial** — inbound arrives, nothing reads it |

---

## 1. Download package — partial

**Have.** A single-file binary via PyInstaller, built in CI and attached to a GitHub release.
Linux and macOS (Apple Silicon).

**Missing.**

- **Windows build.** Not attempted. The pieces most likely to break are the ones already known
  to be platform-specific: the GUI window has no backend inside a one-file binary on Linux and
  is expected to work through WebView2 instead, and the file mode that protects the stored model
  key is a no-op on Windows.
- **Notarization.** The macOS bundle is unsigned, so a first launch needs right-click → Open.
  For a customer who was told to download and run, that reads as a broken download.
- **Intel Mac was dropped** — the runner image is retired. A pre-2020 Mac cannot run this build.
- The published release trails the working tree by a long way and should not be handed to anyone
  as a current example.

**Ours.** All of it.

## 2. Install & run on PC — done

**Have.** An installer that places the binary, links it onto `PATH`, and can start, stop and
report on the background service. It can also install a login item so the service comes back
after a reboot.

This is the step where "no more hosting decision" actually got delivered.

**It no longer asks for an AI assistant** (2026-08-11). Setup used to detect one, offer to
install Goose, register the mcp and open it. The person this is packaged for does not have a
coding assistant and should not install one to finish setting up a phone answering service.
`agentduet-desktop connect` remains for whoever wants it.

**Missing.** Linux system-library detection is partial, and the Windows install path is untested
because there is no Windows build to test it with.

**Ours.** Yes.

## 3. OAuth creates the API key — missing

**Required.** The customer authorises in a browser, and a key plus a connector come back without
anybody at our end doing anything.

**Have.** Nothing. Credentials are typed in by hand, and the connector identifier is issued by a
person on request.

**Missing.** The authorisation endpoint, and self-serve connector provisioning behind it.

**One constraint that must survive the design:** a connector accepts **one client**. A second
client on the same connector makes the call-answer handshake race, and the symptom is a call that
never connects. So provisioning has to mint one **per install** — it cannot be a shared
credential handed to a partner to distribute.

**Ours.** No. This is the platform side.

**Why this is the gate.** Everything else on this list is an inconvenience. This one means every
single install needs a human at our end, which caps the product at however many people we are
willing to serve by hand — and makes it unsellable by anyone else, because a reseller cannot
carry a product where each unit requires an engineer they do not employ.

## 4. OAuth auto-links the model key — missing

**Required.** Model credentials attached during authorisation, with no copy-paste.

**Have.** Manual entry, through the installer or the settings page, written to the instance's
`.env` and to the running process's environment.

**Missing.** The linking mechanism itself.

**Ours.** Half. Anything read from `.env` is read from the environment **at use time**, never
captured at startup, and the channel loop re-checks whether the connector is ready on a timer —
so a credential that arrives later takes effect without a restart. A linked key would drop into
that with no further work. The issuing side is not ours.

**Note what does not change.** The customer still needs their own model key. Authorisation
removes the copy-paste and the wait, not the requirement.

## 5. Verify WhatsApp in the application — missing

**Required.** Confirm the customer's number before the agent goes live, since on this path there
is no trunk already tying the agent to a line.

**Have.** Nothing. Inbound WhatsApp reaches the connector, but over a **shared sandbox number** —
fine for testing, unusable as a product.

**Missing.** The in-application verification step, and a per-owner WhatsApp Business account for
it to verify against.

**Ours.** The in-application step is. The per-owner number is not, and is the later of the two.

## 6. Call in via WhatsApp to test — partial

**Required.** A real inbound message or call, proving the install works.

**Have.** The daemon answers WhatsApp (2026-08-11). The inbound handler accepts `Network.WA`,
replies with `SendWAMessage` in the shape the SDK's own echo-bot uses, and the owner's queued
replies go out the same way. A WhatsApp sender counts as **verified** — the number is proven at
registration, which is a stronger claim than a collected email, and it grants their curated
profile and their own history without widening `knowledge/`, which is public to every asker
regardless.

This is also what let the vendored SDK wheel be deleted. The released SDK carries WA; only DDUET
needed the private branch.

**Missing.**

- **The inbound payload shape is still unconfirmed.** The echo-bot proves the outbound shape and
  nothing about the inbound one — it replies with a fixed string and never reads a body. The
  extractor accepts Meta's flat and wrapped forms and the older typed-parts form, and **logs any
  payload it cannot read, in full**. Narrow it once a real message has been seen; do not narrow
  it before.
- A **per-owner WhatsApp Business Account**, as in step 5. Until then this is a shared sandbox
  number: testable, not shippable.

**Ours.** Yes, apart from the number.

---

## The shorter path

The trunk-initiated route skips steps 3, 4 and 5 outright:

- **No WhatsApp verification** is required, so it does not wait on a per-owner number.
- Authorising against a trunk that already establishes identity **answers the provisioning gate
  by another route** — the hardest item on this list stops being a new system to build.
- Its opening use case is call transcription and recording.

That last point deserves saying plainly, because it cuts against an assumption easy to carry in
from the rest of this repository: **transcription and recording need almost none of the safety
architecture here.** No knowledge lookup, no disclosure decision, no bounds check — nobody is
answered and nothing is decided. Which makes it much sooner and much safer to ship, and means
the security work is not what carries the first version.

**It is not a forward and not a tap.** The 07 August topology puts the agent between two legs:
the inbound call terminates on it, and it originates a second leg onward to the PBX. A
back-to-back user agent. So the media is ours by construction rather than by permission, which
is what "out of the box" means here — and the two audio tracks are simply the two legs.

**The custody question therefore gets bigger, not smaller.** The secretary holds only what the
owner told it to say; this holds everything both parties say. The answer is in the topology —
the app runs on the owner's own machine — but state it precisely: recordings are **stored** only
there, while the media transits the platform to reach it.

## Decisions this surfaces

**The owner-facing site is no longer transitional.** "Authorise in the UI" and "verify in the
application" are a first-run setup surface. The standing decision treats the site as temporary,
kept only until the command line covers first-run configuration. This flow makes it load-bearing
for onboarding. That is a reversal, and `design.md` currently says the opposite.

**Two audiences, one build.** A small vendor installing this for themselves and an integrator
reading it as a worked example want the same binary and the same source. Keep it one codebase —
a second copy for the second audience is how the two silently drift and the example stops
matching the product.
