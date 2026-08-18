# CLAUDE.md — agentduet-desktop

The release package for **AgentDuet Desktop**, which runs on the owner's machine.

Split out of `../secretary-sample/` on 2026-07-30. That folder still exists and still runs the
POC demo — **do not assume a change here is there, or vice versa.** They will diverge; this one
is the deliverable.

## TWO PRODUCTS, ONE BINARY — read this before planning anything (2026-08-17)

**The RECORDER is the product. The SECRETARY is the ambitious one, and it does not gate the
recorder.**

- **The recorder** — the call is carried through to the owner, both sides are recorded, the
  audio is transcribed on their machine, and a cloud model may summarise the transcript
  afterwards. **Two humans talk. Nobody is answered. Nothing is decided.**
- **The secretary** — the agent picks up, speaks for the owner, and may act. Everything under
  *Invariants* exists for this, and applies only to this.

**Why this is written down.** We spent months on the fence — disclosure, capabilities, bounds,
escalation, the two-part tool split — and it was good work that is enforced in code and tested.
It also stopped us shipping the simpler product, which needs none of it. Every one of those
invariants governs what an agent may SAY or DO on the owner's behalf. A recorder says nothing
and does nothing. Requiring it to satisfy a fence built for a different product is how a
three-week feature becomes a three-month one.

**The rule that follows:** when a change touches only the recorder, do not reach for the
secretary's machinery, and do not ask a recorder feature to justify itself against invariants
that have no subject. If a feature has an agent speaking or acting, the fence is mandatory and
non-negotiable.

**The entry point is the recorder**, per `agentduet_macos_app_ux_mockup.html`: sign in, choose a
folder, and four services — record calls, transcribe them, record messages, connect a model for
summaries. Setup asks nothing about a model or an agent. The secretary is configured later, by
someone who wants it, and is not on the path of a new install.

**THE MOCKUP IS A SPEC OF INTENT, NOT A SET OF CLAIMS TO AUDIT.** When it shows something we
have not built — single sign-on, SMS archiving, "Apple Neural Engine" — the answer is a STUB and
a checklist item, not an edit to the design. **We are stub-first: the gap is the work, and the
mockup is what says the work exists.** Quietly reworded to match today's implementation, the
design stops being a target and becomes a description, and the thing we meant to build is lost
without anyone deciding to drop it. A line comes out only when it is genuinely IMPOSSIBLE, and
after a conversation with the team — never because one engineer found it inconvenient.
(Written 2026-08-17 after I proposed rewording the Neural Engine claim to match faster-whisper.)

**What this does NOT mean.** The secretary is not deleted and the invariants are not relaxed.
`tests/test_rules.py` still enforces them, and the day an agent speaks on a call they all apply
exactly as written. This is about which product a new install is, and what a recorder change has
to answer for.

**The one place they genuinely collide:** `voice.register()` claims `on_incoming_call`, and one
connector has one handler. Answering and carrying are therefore mutually exclusive per install,
which is a MODE, not a preference — see the trunk-use-case section at the end.

## Layout

```
src/agentduet_desktop/        the framework — 23 modules, package-relative imports
  web.html sim.html       the owner's view, the channel simulator
  setup.html              first-run setup (2 steps)
  canvas-default.html     generic asker-facing surface for a capability with no page of its own
  templates/              seeded ONCE into $AGENTDUET_HOME on first run, then owned by the owner
  examples/               working capabilities to copy from — NEVER installed
tests/                    test_rules (model-free), test_behaviour (drives a real model), test_isolation
packaging/                PyInstaller spec
entry.py                  frozen-binary entry point (a spec cannot use a console-script name)
```

Instance data lives in **`$AGENTDUET_HOME`** (default `~/.agentduet-desktop` — *not*
`~/.agentduet`, which is where an SDK user's API key goes): `settings.md`, `knowledge/`,
`canvas/`, `people/`, `permissions.json`, `capabilities.json`, `.env`, `run/`.

Build: `uv build --wheel` · `pyinstaller --distpath dist-bin packaging/agentduet-desktop.spec`
(needs a build venv with the working SDK — see Blockers). ~35s, output `dist-bin/agentduet-desktop`.

**Don't rebuild to iterate.** `./dev.sh` restarts from source in ~3s against the same
`$AGENTDUET_HOME`, and the pages (`web.html`, `settings.html`, `setup.html`, `sim.html`) are
`read_text()` **per request** — an HTML change needs only a browser refresh, no restart and no
build. Rebuild only to test the real install or to ship. The site token survives a restart, so
an open tab keeps working either way.

## Which surface, per platform (decided 2026-08-14)

**macOS and Windows set up in the browser page; Linux sets up in the console.** All three ship
the same binary and both surfaces exist everywhere — this is about which one is DOCUMENTED and
led with, not which one works.

The reasoning is who is holding it. Mac is where the testers are today. Windows is where the
SIs will be, and neither audience wants a terminal. Linux is where someone SELF-HOSTS, on a box
they reached over ssh, where opening a loopback browser page is the awkward path rather than the
easy one.

**Consequence, and it is the part that bites: `init` must cover what the wizard covers.** It has
drifted TWICE now, in both directions — the wizard gained a mode question, a recording setting
and a speech-model download while `init` asked only for a model, a connector and the interview;
then later `init` gained a language question the settings page did not have, and language is the
setting that decides whether an English call comes back as fluent Malay. `tests/test_rules.py`
now checks the two surfaces cover the same fields, because remembering did not work.

**The interview is model-driven, so it cannot be the only way to set a name.** It hands the
answers to the model and lets it write the files — which fails at the first question for an
owner with no key, and that is precisely the owner this console path exists for. `who_you_are()`
sets name and pronoun with no model; the interview is offered only when one is attached, and
only adds what the owner DOES. This matters beyond tidiness: `transcribe.py` primes the speech
engine with the owner's name, which measurably beat moving to a bigger model, so a nameless
install silently gets worse transcripts.

**The Linux browser page is for debugging now**, not the documented path. Anything an owner must
be able to do has to work in `init`.

## The house style — `app.css`

**One stylesheet, served at `/app.css`, linked by every page.** It was pasted into each page and
they drifted within a day, so a change now lands everywhere at once. Page-specific layout stays
in the page; what lives in `app.css` is what more than one page needs — the window chrome, the
tokens, and the controls.

The values come from `agentduet_macos_app_ux_mockup.html` (its Tailwind config and the classes
it uses), written out as plain CSS. **Nothing in it may be fetched at runtime** — Tailwind
arrives from a CDN in the mockup and this app has to open on a machine with no network. The one
exception is the two Google Fonts links in each page's `<head>`, which is a known gap and on the
checklist.

**The three traffic lights are ours, drawn in HTML.** In a browser they are the illusion the
design intends. In the native window macOS draws its OWN in the real titlebar, and two sets of
lights is worse than none — so `nativeChrome()` puts `.native` on `<html>` when
`window.pywebview` exists and `app.css` hides ours. It listens for `pywebviewready` too, because
the object is not always injected before the script runs.

## Working rules

- **Never wipe `$AGENTDUET_HOME`.** No `rm -rf` on it to "start clean" — correct the specific
  file or key instead. Clearing it destroys the owner's setup, the knowledge they have built up
  by using the agent, and `run/secretary.pid`, after which a second launch cannot see the first
  and both fight for port 8899. Use a throwaway `AGENTDUET_HOME=/tmp/...` for experiments.
- **Check what is already running before starting anything** (`status`, or `ss -ltnp | grep 8899`).
  `--onefile` shows TWO processes per launch — a bootloader and its child — which is normal, not
  a duplicate.

## Invariants — enforced in code, not by convention

**These govern the SECRETARY.** They are about what an agent may say or do on the owner's
behalf, so on the recorder path most of them have no subject at all — nothing is disclosed,
nothing is committed, no bounds are checked, because nobody is answered. Do not treat them as a
checklist a recording or transcription change must pass. See *Two products, one binary* above.

Break one of these and the secretary is a different product.

1. **Disclosure follows the folder grant, entirely.** No keyword filter second-guesses it.
2. **Action is never granted by a document.** Committing, pricing, scheduling → `policy.COMMITMENT_RULES`.
3. **An action must fit the declared bounds** (`capabilities.check_bounds`), and a capability
   with no bounds authorises nothing.
4. **Knowledge writes stay inside `knowledge/`** — granted folders can be real source trees.
5. **An edit must match exactly once** (`edit_knowledge`), and every edit is journalled.
6. **Drafting has no send path.** `draft_reply` cannot send; only `reply_to` sends.
7. **A grant cannot be walked out of** via symlink (`folder_index`).
8. **The owner site binds loopback only**, with a per-machine token.
9. **The asker-facing surface never imports the owner registry** (`canvas.py` must not import `tools`).

`tests/test_rules.py` covers most of these and runs with **no model and no venv** (156 checks).

## The three documents

- **`docs/design.md`** — the architecture, and what would reverse each decision. Single source of
  direction. Records live decisions only; done work belongs in `git log`.
- **`docs/tool-surface-risk.md`** — the attack class the two-part split exists to prevent, with a
  worked example. Written to be shared outside the team.
- **`docs/thesis.md`** — why an agent is a UI and its tools are APIs, where that analogy stops
  holding, and the conclusion that follows: lower the barrier to building a backend and you must
  raise the floor of its security by the same amount. For a white paper, a customer explanation,
  or settling a hard decision.

## Decisions, and why — do not re-litigate without reading these

- **The daemon is the product. The mcp is OPTIONAL** (2026-08-11). This revises the 2026-08-03
  "two parts, no owner interface" decision, and for the same reason that one revised 2026-07-30:
  the assumption about who the owner is changed again. Packaged for small vendors handed a
  binary, the owner does not have Claude Code or Goose and should not install one to finish
  setting up a phone answering service. So **setup no longer mentions an assistant** — its step 4
  is "finish", `init` runs the interview by default instead of deferring to the mcp, and `status`
  prints nothing when no assistant is registered rather than "nothing can drive this secretary".
  `agentduet-desktop connect` remains for whoever wants it, and the 44 mcp tools are unchanged.
  **Consequence: the site is load-bearing again, not transitional** — the August onboarding flow
  puts authorisation and WhatsApp verification in it, so something has to render them. The daemon
  must still not exit when it fails to bind.
- **`knowledge/` is one flat, public folder.** `public/` vs `partners/` is gone. A fact only one
  person may hear belongs in `people/<identity>.md`. Consequence: verified and unverified read
  the same documents — curate accordingly.
- **Settings live OUTSIDE `knowledge/`** (`settings.md`). They are parsed by heading, and a
  knowledge edit that renamed one silently emptied the never-say list. Keep the headings.
- **Format follows gated vs quoted.** A value that GATES an action is typed JSON, once
  (`capabilities.json`); a value that is only QUOTED is prose (`<capability>.md`). Where a value
  is both (hours), JSON owns it and both directions are guarded — `add_knowledge` refuses prose
  that contradicts a bound, `set_capability_bound` warns which prose went stale.
- **A capability is a named trio**: `capability.json` (do) + `<name>.md` (say) +
  `<name>.html` (click, optional → generic fallback). Same name is how the code finds one from
  the other.
- **The model reads, code decides.** Every judgement the model makes is checked mechanically
  before anything happens.
- **Setup is an interview, not a form**, and asks only what cannot be learned by running: name,
  pronoun, what the owner does. Availability, contacts and never-say emerge from use — the first
  unanswerable question escalates, the owner answers once, it is remembered.
- **Setup never grants authority.** The interview prompt forbids declaring a capability; only an
  explicit owner action installs one.
- **The pizza example is not installed.** A new owner should not inherit someone else's business.
- **Voice is SPEECH-TO-SPEECH, not a cascade** (decided 2026-07-31). A cascade (STT → the text
  model → TTS) would have preserved every invariant, because the brain would still see text and
  `brain.handle_query` would still run before anything was said. It was rejected on latency:
  CPU-only is far too slow and depending on the T4 box is not acceptable for a product. So a
  hosted realtime model answers calls directly.

  **Know what that costs.** The realtime model IS the agent on a call, so:
  - **Action stays code-enforced** — booking goes through `check_bounds` as a tool it must call,
    and code still decides. Invariants 2 and 3 hold.
  - **Disclosure becomes prompt-enforced on voice.** Nothing can intercept a sentence before it
    is spoken, so invariant 1 does NOT hold on this channel the way it does in text. The
    mitigation is detection, not prevention: give it `search_knowledge`, instruct it to answer
    only from what that returns, and use the transcript afterwards to flag ungrounded claims.
    Say this plainly to anyone who asks — do not imply the text guarantees carry over.

## Gotchas that cost hours

- **PyInstaller cannot see lazy imports.** `web`, `brain`, `tools` and the provider SDKs are
  imported inside functions; the binary builds clean and fails at runtime. The spec collects
  `agentduet_desktop` submodules explicitly. Never `collect_submodules("mcp")` — `mcp.cli` calls
  `sys.exit(1)` at import and aborts the build.
- **pywebview has no GUI backend inside a `--onefile` binary on Linux** (GTK/Qt Python bindings
  are system libraries). `webview.start()` raises; the window is optional and must fall back to
  the browser. Windows should be fine via WebView2.
- **`sm.run_forever()` must be called with `install_signal_handlers=False`.** The daemon runs on
  a WORKER thread (pywebview owns the main one), and the SDK's handler install calls
  `set_wakeup_fd`, which raises `RuntimeError` off the main thread. The SDK means to degrade
  gracefully — its docstring says to pass False off the main thread — but its guard catches only
  `(NotImplementedError, AttributeError, ValueError)`, so the RuntimeError escapes and kills the
  channel ONE LINE after logging "inbound is live". Symptom: connect → set triggers → drop, every
  5s forever. Hidden until 2026-07-31 because with no connector configured the code never reached
  `run_forever` at all. Candidate SDK issue: add `RuntimeError` to that except tuple.
- **Anything read from `.env` must be read from the ENVIRONMENT at use time, not captured at
  startup.** The settings page writes credentials into `os.environ` of the running process as
  well as to `.env`, so a startup-time snapshot makes the owner restart for no reason — or worse,
  shows a "not connected" state advising them to check a network that is fine. The channel loop
  polls `connector_ready()` every `CONNECTOR_POLL_SECONDS`.
- **macOS `security import` cannot read a MODERN PKCS#12.** An AES-256/SHA-256 `.p12` — what
  OpenSSL 3 produces by default — fails with `MAC verification failed during PKCS12 import
  (wrong password?)`, and the password is fine. It needs the legacy shape. The trap is that the
  obvious fix is wrong in the other direction: `openssl pkcs12 -export -legacy` uses **RC2-40**,
  which OpenSSL 3 cannot read BACK without the legacy provider, so you cannot verify what you
  built. The format that satisfies both is **3DES + SHA-1**
  (`-keypbe PBE-SHA1-3DES -certpbe PBE-SHA1-3DES -macalg sha1`): macOS accepts it, and 3DES is
  still in OpenSSL 3's default provider so it reads back locally. Verify the `.p12` opens and
  contains the leaf, the intermediate and one private key BEFORE uploading it as a secret.
- **`gh workflow run` builds the REMOTE, not your working tree.** A dispatch build fires against
  what is on `origin`, so a fix committed locally and not pushed is not in it. Caught after
  triggering a build to prove `app.css` was packaged, from a commit that did not have the fix —
  it would have gone green and proved the opposite of what was intended.
- **`pkill -f` matches your own command line**, including the shell running it. It has killed
  test blocks and daemons mid-run. Kill by PID or port.
- **SIGTERM is caught somewhere in the async stack** and does not always exit. `stop` verifies
  and escalates to SIGKILL; never report "stopped" on the strength of a signal sent.
- **`chmod 0600` is a no-op on Windows.** The model key in `$AGENTDUET_HOME/.env` is unprotected there.
- **Python ≥3.12** — the SDK requires it.

- **Local STT is faster-whisper on the CPU. The Apple Neural Engine is an INTENTION, not yet
  reachable** (checked 2026-08-17). CTranslate2, the runtime underneath, has CPU and CUDA
  backends only — no Metal, no Core ML, no ANE — so on a Mac it is CPU-only today and every
  measured number is a CPU number.
  **The mockup says "Apple Neural Engine" and that stays.** It is a target we have not hit yet,
  and it goes on the checklist rather than being edited out of the design. Removing it needs a
  reason it is IMPOSSIBLE plus a conversation with the team — not one engineer deciding the
  current implementation is the final one.
  Reaching it means CHANGING ENGINE, not setting a flag: `whisper.cpp` with a Core ML encoder is
  the realistic route, at the cost of a per-model `.mlmodelc` to generate and ship, a slow
  first-run compile on the user's machine, and a C++ dependency in a binary whose packaging was
  just settled. Apple's own `SpeechAnalyzer` uses the ANE with no download at all, but it is
  Apple's model rather than Whisper, gated on macOS version, and Mac-only — Linux and Windows
  would still need a second engine.
  **CORRECTION (2026-08-18): the encoder is NOT the bottleneck, and the earlier version of this
  entry said it was.** The claim came from `large-v3-turbo` not beating `medium`, which compares
  two different encoders and settles nothing. The right comparison is turbo against `large-v3`,
  since they share an encoder and turbo's decoder is cut from 32 layers to 4: on the clean 88s
  call that is 20.7s → 11.2s, so the DECODER was 46% of the work. Encoder is a bit over half,
  not dominant.
  **What that does to the case for Core ML:** it accelerates the encoder only, so the ceiling is
  roughly 1.3–1.5x overall, not the 3x an encoder-bound model would give. Weigh it against a
  C++ dependency, a per-model artifact, and a Mac-only second engine — for a job that already
  runs post-call on a queue where nothing waits for it. The honest argument for the ANE is
  power and heat on a laptop, not wall-clock.

## Open — the checklist

Last reviewed 2026-08-11, after the WhatsApp swap, the rename, and dropping the assistant from
setup. Items that existed only because of the owner interface were removed on 2026-08-03 — see
the Cleared note at the end.

**Deck alignment:** the August onboarding flow is tracked per-step in `docs/onboarding-gap.md`,
which says which side of the line each gap sits on. This list carries only the parts that are
ours to build.

**The recorder — every gap between the mockup and what runs**

From `agentduet_macos_app_ux_mockup.html`. Each of these is a STUB shipping now and a thing to
build, not a design to trim. Nothing here is optional-by-default: the mockup is what we agreed
the product is.

- [ ] **Single sign-on** — Apple, Google and Microsoft, which is how the mockup gets the owner's
      identity, phone number and connector without anyone typing a uuid. `connector.OAUTH_URL`
      and `oauth_available()` already gate the page on a backend that does not exist yet. Until
      it does, setup shows the three buttons and a manual path beside them.
- [ ] **Record Call has nothing behind it** — `carry.py` bridges and the recorders start, but the
      platform does not hand the app conference audio, so the directory the panel lists is empty.
      This is the mockup's FIRST service. Being added on the AgentDuet side (Dat).
- [ ] **Record Message (SMS) does not exist at all.** We have WhatsApp through the SDK, not SMS
      archiving. This is a channel we do not ingest, not a screen we have not drawn.
- [ ] **Connect AI is a SUMMARISER in the mockup** — transcripts go to a cloud model for action
      items and summaries, after the call. That is not what `llm.py` does today, which is drive a
      live agent. The providers and key handling carry over; the feature does not exist.
      Its provider list also differs (OpenAI is offered, Qwen is not).
- [ ] **Apple Neural Engine for transcription** — see the STT decision above. An engine change,
      and the encoder is the right thing to accelerate.
- [ ] **A chooseable storage folder.** The mockup lets the owner pick where recordings and logs
      go. Today `carry.RECORDINGS` is a module constant under `$AGENTDUET_HOME`, so the page can
      only SHOW the resolved path. Making it settable means reading it at use time — the same
      read-at-use-time rule that `local_model()` already had to learn.
- [ ] **Apple's own STT on the Neural Engine — TRY IT, measure two things.** The alternative to
      swapping Whisper's runtime is not using Whisper at all on a Mac: `SpeechAnalyzer` /
      `SpeechTranscriber` (macOS 26, WWDC 2025) is free, on-device, built for LONG-FORM audio,
      and uses the ANE fully with no model of ours to ship. The older `SFSpeechRecognizer` is
      not a candidate — it caps a request near a minute, which is useless for a call.
      **The blocker is the language, not the licence.** Using system frameworks in an app for
      Apple platforms is the normal permitted case, and it costs nothing. But `SpeechAnalyzer`
      is a Swift-first async API and this app is Python in a PyInstaller binary, so the shape is
      a **small Swift helper we bundle** — hand it a `.wav`, get text back — built on the macOS
      runner CI already has, and signed and notarized with the app.
      **Two measurements decide it, and both need a Mac** (queue behind the notarization check):
      1. **Accuracy on OUR audio.** Whisper called Singaporean English Malay at 0.95 confidence.
         Apple's model may be better or worse and nobody knows. Same recordings, same test.
      2. **Power and heat versus `medium` on CPU.** This is the real argument — transcription is
         post-call on a queue, so wall-clock barely matters, but fans and battery on a MacBook
         transcribing all day do.
      **Scope to be honest about:** macOS 26+ and Apple Silicon only, so Whisper stays for Linux,
      Windows and older Macs regardless. This is a second engine, not a replacement.
      The UI already offers it: `transcribe.ane_support()` reports whether a machine COULD run
      it, and the Transcribe panel disables the option with the reason where it could not.

- [ ] **Bundle Inter, JetBrains Mono and Material Symbols.** The pages now load all three from
      Google Fonts, as the mockup does. On a machine with no network the text falls back to a
      system font — fine — but **Material Symbols fails LOUDLY**: the ligature name renders as
      literal text, so a sidebar reads "grid_view call graphic_eq". Offline is most of what this
      product claims, so the font files belong in the binary. Not done yet because it is a
      packaging change and the design fidelity was the ask.

- [ ] **Per-service on/off toggles.** The mockup's overview switches each of the four services
      independently. We have one `## Calls` mode and a `## Record calls` boolean.

**Release blockers**

- [ ] **`init` cannot take a connector**, and secrets deliberately cannot go through the
      assistant (`save_connector` is outside `OWNER_TOOLS` — a credential typed into chat goes
      to the model provider and lands in `owner_chat.json`). With no interface that leaves no
      way to configure the product. `init` is the answer.
- [ ] **Connector provisioning.** Every install needs its OWN `AGENTDUET_CONNECTOR_UUID` — one
      client per connector, and a second races `call.answer()`. A new user installs cleanly and
      then stops dead waiting on a human. Still the biggest get-started gap.


- [x] ~~**Publish the SDK.**~~ **RESOLVED 2026-08-11, by dropping the requirement.** `agentduet`
      `1.0.0` shipped to PyPI on 2026-08-10 and still has **no DDUET at all** — its API is
      unchanged from `1.0.0b10`, so publishing did not help. DDUET lives only on
      `B3Networks/agentduet-sdk-python` `feature/dduet-channel` (`1.0.1b1`), a PRIVATE repo, which is
      why CI carried a committed wheel in `vendor/`.
      **So the channel was swapped to WhatsApp instead** — `Network.WA` + `SendWAMessage`, both
      in the released SDK. `vendor/` is deleted and `pyproject.toml` asks for `agentduet>=1.0.0`.
      Neither onboarding path in the August flow used DDUET anyway. This also removed the
      base-URL clash: DDUET needed a dev endpoint while voice needs prod, and one client has one
      base URL.
      **Watch the version ordering:** `1.0.1b1` (the DDUET branch) sorts ABOVE `1.0.0`, and when
      main ships a real `1.0.1` it will outrank the branch while still lacking DDUET. DDUET is a
      feature, so that branch ought to be renumbered `1.1.0b1`.
- [ ] **Windows binary.** Intel Mac was DROPPED 2026-08-04: `macos-13` is retired so the job
      never started, and a queued job holds its whole run open — finished builds looked
      unfinished for hours. A pre-2020 Mac cannot run our build; check the chip before sending.
- [ ] **Notarization.** The `.app` is unsigned, so first launch needs right-click → Open.
      **Machinery is BUILT and waiting on credentials** (2026-08-17): `build.yml` signs, notarizes
      and staples whenever `APPLE_CERT_P12` exists, and skips cleanly when it does not, so nothing
      changed for today's builds. `packaging/entitlements.plist` carries the hardened-runtime
      holes, of which `allow-jit` is the one to keep: **wasmtime JITs**, so without it the tool
      sandbox dies when a customer tool is first called.
      Blocked on three things from the Apple account (asked in `#Apple-Developer-Account`, and
      the account holder is now Luk): a **Developer ID Application** certificate, an App Store
      Connect API key (issuer id + key id + `.p8`), and the Team ID. A CSR is already generated
      at `~/.apple-signing/devid.csr`; the private key beside it never leaves this machine.
      **We are NOT going to the Mac App Store** — it requires the sandbox, which this app's
      loopback server, home-directory writes and model download would all have to be granted
      around. Deferred, not rejected.
      **A Mac is needed only to verify the result**, and it need not be ours: notarization can be
      accepted while the app still fails to launch, so someone must double-click the real DMG.
- [ ] **Propose/approve is NOT a fence.** Half done: **written down 2026-08-11** in
      `docs/design.md`, so the product no longer implies a protection it does not have. What
      remains is the mechanism — an approval an agent cannot perform. `toolstore.approve()` copies
      `pending/<name>.js` into `tools/`, so anything that can write `$AGENTDUET_HOME` installs a
      tool directly — no CLI, no `propose_tool`. Same for `permissions.json` (who gets which
      tool) and `capabilities.json` (the bounds). The CLI-only approval step therefore holds only
      against an assistant with neither shell nor file access to that directory: **Claude Code
      always has Bash; Cowork has folder write; Goose has `developer` one toggle away.** The
      control that actually matters is whether `$AGENTDUET_HOME` is reachable at all, which is
      the owner's host configuration and not something we enforce. Two honest fixes: say this
      plainly in `docs/design.md`, or make approval need something no agent can produce — a code
      shown on the owner's phone over the product's own channel, typed back. The second converges
      with outbound, which is unbuilt, and is why this stays open rather than shipping a weaker
      substitute. NOTE the first framing here was wrong: `design.md` never claimed this was a
      fence, it OMITTED the limit. Its stated property — the tool cannot choose a destination at
      call time — is real and enforced in `resolve_url`.
- [ ] **Pre-public scrub.** Going public leaks `stg.dduet.com` (×3) and
      `wss-dev.internal.b3networks.com`, and this file names colleagues and vendor limits — it is
      written for us, not for the world. No secrets are tracked (checked: the only `sk-` match is
      the deliberate `sk-LEAKED-CANARY` in `test_wasm.py`, and the numbers are placeholders).
- [ ] **OAuth for BOTH keys** — deck steps 3 and 4. Authorisation creates the AgentDuet key and
      auto-links the model key, which is the flow's whole claim and the two things it does not
      have. Ours is the receiving end only: anything read from `.env` is read from the
      environment at use time, so a key that arrives later needs no restart. See
      `docs/onboarding-gap.md`.
- [ ] **A B3-proxied model would DELETE deck step 4.** One credential instead of two: the owner
      authorises once and there is no model key to link, because we are the provider. Also keeps
      the knowledge inside B3's boundary, which the current "bring your own key" does not. The
      cost is that we pay for inference — a pricing decision, not a technical one. Recorded
      because a free third-party model is the obvious-looking alternative and is not one: the
      one evaluated (OpenCode's Big Pickle, 2026-08-11) still needs a signup with billing
      details, is free only "for a limited time", and says collected data may be used to improve
      the model — which contradicts the whole disclosure pitch, silently, on the owner's behalf.
- [ ] **Credential storage on Windows** — use the OS credential store, or say plainly that the
      key is plaintext protected only by file mode.

**Backend, not this package**

- [ ] Identity: does AgentDuet issue a stable identity, and carry the verified property?
- [x] ~~**Directory/discovery.**~~ **MOOT 2026-08-11.** This described the DDUET web-chat
      surface, which is gone. WhatsApp and voice both arrive on prod, so there is no base-URL
      conflict left and nothing to discover a per-agent URL for. Discovery becomes a real
      question again only if a web surface returns.
- [x] ~~**Reply over WhatsApp.**~~ **BUILT 2026-08-11** — WhatsApp is now the messaging channel,
      not an unhandled network. `on_incoming_message` accepts `Network.WA`, replies with
      `SendWAMessage` in the `wa_echo_bot.py` shape (`_wa_text`, one helper so the asker reply and
      the owner's queued reply cannot drift), and `default_verified("WA")` is **true** — the
      number is proven at registration, and `SELF_VOUCHING_NETWORKS` had said "WHATSAPP" for
      months while the SDK enum is "WA", so the intent had never fired. It grants the profile and
      their own history; `knowledge/` is public to everyone either way, so disclosure is unchanged.
- [ ] **Confirm the INBOUND WhatsApp payload shape.** Still not known. `wa_echo_bot.py` proves
      only the OUTBOUND shape — it replies with a fixed string and never reads a body.
      `_first_text` now accepts Meta flat (`text.body`), Meta wrapped (`messages[].text.body`) and
      the old Nexus `parts`, and **logs any payload it cannot read, in full**. Narrow it once a
      real message has been seen; not before.
- [ ] **Per-owner WABA.** **Shared sandbox number** — fine to test, unusable as product until it
      lands (~September, Cedric's release). Known ids: participant `6596918851`, subscriber (the
      WABA `phone_number_id`) `1151661421362480`.
- [ ] Outbound initiate: messaging is reactive, so held replies are delivered only when the
      person next writes. On WhatsApp there is a second limit — Meta's 24h customer-service
      window, after which a free-form reply needs an approved template we do not have.
- [ ] Unverified askers: `knowledge/` is flat and public. Decide the disclosure tier before
      strangers are in scope.
- [ ] **DashScope caps concurrent realtime connections per ACCOUNT** ("max_connections 100").
      It presents as SILENCE on the call. Raised with luk 2026-08-03.

**Voice**

- [ ] **The tool contract** — status-and-render landed 2026-08-05, so every OTHER tool now
      returns a status and the framework writes the sentence. `search_knowledge` is the
      exception: it still hands over 4,000 characters to paraphrase, because on a knowledge
      question the documents ARE the answer. Narrowing that to a sentence is the per-turn half
      of the fence a prompt cannot do, and the last piece of it.
- [ ] **Post-hoc grounding check** on the transcript. Nearly free once the tool contract lands —
      and cheaper still since 2026-08-14: every answered call now writes the caller's audio, the
      agent's audio and a turn-by-turn transcript, so checking whether the agent said something
      the knowledge does not support is a text comparison over data already on disk. Detection,
      not prevention, and the honest substitute for the cascade below.
- [ ] **Measure a hosted cascade.** Rejected 2026-07-31, but the recorded reason rejects a LOCAL
      cascade. It is the only option that restores every invariant.
      **NOT YET, decided 2026-08-14.** `AgentDuet/agentduet-pipecat` (Tuan, public) makes a
      cascade cheap to build — `AgentDuetTransport` drops a live Call into a Pipecat pipeline.
      But nothing today wants one: agent mode is speech-to-speech on a single realtime stream,
      and carry mode is STT only, post-call, on a queue. A cascade is the only thing Pipecat
      buys, and it would cost two more vendor credentials (their quickstart wants Deepgram AND
      Google) on a product that was just made to work with none, plus Pipecat's weight in a
      58 MB binary.
      **The trigger is specific:** someone needing a TEXT model on a live call — a customer
      bringing their own, a language the realtime model handles badly, or per-turn control the
      realtime path cannot give. Until then the cheaper route to most of the same benefit is the
      grounding check below, which today's recording work made nearly free.
- [ ] `_ring_owner` (the callback that rings the owner) has **never executed** — every other
      part of the callback is tested, and the ring is now rate-limited. It cannot be unit
      tested: it is a closure over the live `SessionManager`, and it opens a session, dials,
      and starts a second realtime model. The only way to exercise it is the real one — call
      your own number, ask for a callback, hang up, and see whether your phone rings.
      **Do that before anyone is told the callback works.**


**The trunk use case — we CARRY the call, we do not listen to it**

Slide 3 of the onboarding deck seeds the CPaaS path with "basic call transcription and recording
out of the box". Slide 4 of `AgentDuet (07 August 2026).pptx` ("Inbound = Ready NOW") gives the
topology, and it is **not** a forward and not a tap:

```
Telco ──▶ CPaaS Leg 1 ──▶ AgentDuet WSS ◀──▶ AgentDuet App   (the owner's machine)
                                │
                                ▼
                          CPaaS Leg 2 ──▶ PBX
```

**Two legs stitched through us — a back-to-back user agent.** Leg 1 terminates ON AgentDuet;
Leg 2 is ORIGINATED BY AgentDuet toward the PBX. Nothing is attached to somebody else's call,
because we are the junction. That is why recording is "out of the box": the media is ours by
construction, not by permission. It also explains the SDK surface — `call.caller` and
`call.callee` are simply the two legs, so "isolated per-party audio" is the natural shape rather
than a feature, and `connect()` takes no destination because the destination is Leg 2's
configured target on the connector.

It is a **different product from the secretary**: two humans talk, nobody is answered, nothing is
decided. None of the fence applies — no knowledge lookup, no disclosure decision, no
`check_bounds` — which is why it is shippable far sooner.

**But the custody question gets BIGGER, not smaller, and that is easy to get backwards.** The
secretary only ever holds what the owner told it to say. This holds everything anyone says — the
owner's customers, in conversations we are carrying. The topology answers it, and the answer is
worth being precise about rather than overclaiming: the App runs on the OWNER'S machine, so
recordings are **stored** only there. The media still transits B3's WSS to reach it, so "never
leaves your machine" is false; "stored only on your machine" is defensible and is the stronger
claim anyway, because it is the one a regulated buyer is actually asking about.

- [ ] **Recording is not built; the transcript is, for the wrong calls.** `voice._make_recorder`
      writes turns into `memory` and `brain.record`, so a call reads like any conversation — but
      it is a byproduct of OUR agent talking, from the realtime model's transcript events. We
      never touch audio: no `.wav`, no frames, nothing. `examples/connect_spy_isolated.py` is the
      working model — `connect(ring_time_seconds=…)` originates Leg 2, then both
      `call.caller.audio_stream()` and `call.callee.audio_stream()` are consumed to WAV. ("Spy"
      there is call-centre vocabulary for supervisor listen-in — `whisper()` speaks to the
      subscriber only, `barge()` to both — not stealth, and not the topology.)
      **It collides with the secretary:** `voice.register()` already claims `on_incoming_call`,
      and one connector has one handler. So this is a MODE, not an addition — the owner picks
      whether the agent answers or the call is carried through to them. Build it behind a
      setting, default off; rip nothing out until it is proven.
- [ ] **Consent gates this AND outbound campaigns, and neither has an answer.** Recording has
      jurisdiction-specific rules (PDPA here, two-party-consent regimes elsewhere); an outbound
      campaign needs to know who is on the list and whether they agreed. Same class of question
      — capturing or initiating without the other side having agreed — and no amount of the
      existing architecture addresses it, because every invariant we have governs what the agent
      may SAY or DO, not whether the other party consented to be in the conversation at all.
      Sharper on this path than on any other: carrying a call means holding both sides of a
      conversation neither party had with us.

**Engineering**


- [x] ~~**The asker allow-list should be data**, not hardcoded in `_tool_declarations()`.~~
      **WITHDRAWN 2026-08-04 — this was a bad idea and the reasoning was backwards.** It was
      proposed for tidiness. But the hardcoded list is the asker side's main protection, and it
      protects by being SLOW to change: adding a tool means editing code, passing tests and
      shipping a build — visible, reviewable, human. As data it becomes a file write. Anything the
      agent can reach that can write that file can grant the agent tools, and the asker agent
      reads text written by strangers. That is the OpenClaw failure shape (see `docs/tool-surface-
      risk.md`), reintroduced as a refactor. If it is ever revisited, the list must be BUILD-TIME
      data compiled into the binary, never read from `$AGENTDUET_HOME`.
- [ ] `agentduet-desktop tool <name> [--arg=value]` — one generic verb dispatching into the registry.
- [ ] `test_behaviour.py` is **flaky** (~2 in 5 on "bare revision keeps the negotiation
      classification"). A single red line there is a signal to replay, not a verdict.
- [ ] No behaviour test asserts the configured **pronoun** reaches an answer.
- [x] ~~`paths.legacy_leftovers()` misreports the shipped templates as deletable leftovers.~~
      **STALE — it was fixed and this line was not.** It returns `[]` unconditionally and its
      docstring explains why. Checked 2026-08-11. That makes **four** items in this file that
      claimed outstanding work already done, which is worth more attention than any of them:
      the fix is to clear an item in the same commit as the work, not at the next review.
- [ ] `README.md` still describes `secretary-sample`, not this package.

**Cleared 2026-08-03 — orphaned by the no-interface decision**

The native window and its third rendering engine, the tray-icon/presence question, close-means-
quit, the macOS window bundle, generic capability UX, owner sight of unverified askers, and
`sim.html`'s unguarded `localStorage`. The site remains as a transitional surface; none of these
are worth building for it.

**Cleared 2026-08-05 — done, and the checklist had not caught up**

- **Service tools** — `service_status/start/stop` are registered on the stdio mcp and have been
  for days; `design.md` already said so while this list still called it a release blocker.
- **Tag asker-authored content as untrusted** — done: escalations, threads, the `them:` side of a
  conversation, the digest and keyword search are all delimited, and the mark cannot be forged
  closed by an asker.

Both were true when written and stayed on the list after they stopped being true. That is the
third time in three days this file has claimed work was outstanding when it was finished — which
is worth more attention than either item: a list that lies gets skimmed, and then the real
blockers on it get skimmed too.

**Cleared 2026-08-03 — already done, listed as open by mistake**

"The daemon still dies with the site" was the first release blocker and described as the only
actively harmful item. It was already fixed: `main()` catches the failure and carries on with a
warning (`secretary_agent.py`, "This used to raise SystemExit(1)"). `docs/design.md` still lists
the same thing as Next 1, and its Next 2 (service tools) is also done — that document has not
been re-read since the work landed.
