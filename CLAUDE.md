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

**The entry point is the recorder**, per the August UX design: sign in, choose a
folder, and four services — record calls, transcribe them, record messages, connect a model for
summaries. Setup asks nothing about a model or an agent. The secretary is configured later, by
someone who wants it, and is not on the path of a new install.

**THE DESIGN IS A SPEC OF INTENT, NOT A SET OF CLAIMS TO AUDIT.** Where it describes something
we have not built — single sign-on, SMS archiving, "Apple Neural Engine" — the answer is a STUB
and a checklist item, not an edit to the design. **We are stub-first: the gap is the work, and
the design is what says the work exists.** Quietly reworded to match today's implementation, it
stops being a target and becomes a description, and the thing we meant to build is lost without
anyone deciding to drop it. A line comes out only when it is genuinely IMPOSSIBLE, and after a
conversation with the team — never because one engineer found it inconvenient.
(Written 2026-08-17 after I proposed rewording the Neural Engine claim to match faster-whisper.)

**THE MOCKUP FILE IS GONE (removed 2026-09-08), SO THE CHECKLIST IS NOW THE RECORD.** It was
`agentduet_macos_app_ux_mockup.html`, and it was never tracked in this repo — every statement
about it here was written from a copy that has since been deleted. That moves the authority
rather than cancelling it: the recorder section of the checklist below IS the agreed product
now, and it is no longer checkable against anything. So it can only be shortened by the
conversation the paragraph above describes, and an item removed from it cannot be recovered by
reopening the design. Treat it as the more precious document, not the less.

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

The values came from the August UX design (its Tailwind config and the classes it used),
written out as plain CSS — `app.css` is now the only surviving copy of them, since that file is
gone. **Nothing in it may be fetched at runtime** — Tailwind arrived from a CDN in the design and
this app has to open on a machine with no network. The one exception is the two Google Fonts
links in each page's `<head>`, which is a known gap and on the checklist.

**The three traffic lights are ours, drawn in HTML.** In a browser they are the illusion the
design intends. In the native window macOS draws its OWN in the real titlebar, and two sets of
lights is worse than none — so `nativeChrome()` puts `.native` on `<html>` when
`window.pywebview` exists and `app.css` hides ours. It listens for `pywebviewready` too, because
the object is not always injected before the script runs.

## Releasing — build, VERIFY, then tag (learned the hard way on a6)

**THE TAG TRIGGERS THE BUILD HERE.** `build.yml` runs on `push: tags: ["v*"]`, so the tag is
what produces the DMG. The global rule that "the deploy pipeline creates the git tag, not you"
belongs to the microservices and is WRONG in this repo — do not reason with it here.

The consequence people get backwards: a tag is not a label you apply afterwards, it is a
trigger. So do not tag to mark a build you have already checked, and do not tag hoping the
build works.

**The order that works:**

1. `gh workflow run build.yml --ref main` — no tag, nothing promised.
2. **Verify the artifact.** THE RULE IS THAT THE ARTIFACT IS VERIFIED BEFORE THE TAG EXISTS —
   not that whoever tags did the verifying. This work runs across more than one machine, and
   only a Mac can run `spctl`/`stapler`/`codesign`; a Linux box cannot, and a Windows build can
   only really be exercised in a VM. Read as "the tagger must do all of it", this step blocks
   the machine that happens to be holding the release for a reason that has nothing to do with
   whether the build is good.

   So it splits by platform, and each half is checked wherever it can be:

   - **macOS** — on a Mac, per the checks below.
   - **Linux** — run the binary anywhere with a Linux box.
   - **Windows** — run the `.exe`; the VM at `~/VMs/agentduet-win11` exists for this.

   **What must hold, and is the whole point:** every artifact attached was verified by someone,
   and the RUN ID attached is the run that was verified. Carry the run id with the verification
   — "the Mac is done" is not enough, because a6's lesson is not that checks are nice but that
   the thing shipped must be the thing checked. Two different builds of the same commit are not
   the same file; PyInstaller output is not reproducible byte-for-byte.

   **What must never happen:** tagging with nobody having checked, or attaching a run that
   nobody checked.

   On a Mac: apply a quarantine attribute (only a
   browser sets one, so `gh run download` alone does not reproduce what a tester gets), then
   `spctl -a -t open --context context:primary-signature -vv` on the **`.app`**,
   `xcrun stapler validate` on the **DMG**, and **start the daemon**. `status` is not
   verification — see below.

   **THE TICKET IS STAPLED TO THE DMG, NEVER TO THE `.app`** — `build.yml` runs
   `xcrun stapler staple "$DMG"` and nothing else. So `stapler validate` on the app answers
   `does not have a ticket stapled to it` on a perfectly good build, while `spctl` on the same
   app says `accepted / source=Notarized Developer ID`. Those two readings look like a
   contradiction and are not: Gatekeeper is satisfied by the notarisation itself, and the
   stapled ticket only saves it an online check. Validate each on its own target or the check
   invents a defect. (Cost a scare verifying a12 on 2026-09-08.)

   **A fresh instance serves `setup.html` at `/`, so grepping the served page for a hub change
   proves nothing.** There is no separate hub route — `index` picks the page per load — and a
   throwaway `AGENTDUET_HOME` always fails `needs_setup()`. Check the packaged file inside the
   bundle instead (`Contents/Resources/agentduet_desktop/web.html`), which also catches the
   stale-build-venv trap below in one step: it should be byte-identical to the working tree.
3. Tag `v0.1.0aN` and create the release. A DRAFT release may name a tag that does not exist
   yet; publishing creates it.
4. `attach-release.yml` with that build's **run id** + tag. It attaches a build that has
   already happened, on purpose, so the thing shipped is the thing tested.

**Why this order and not the obvious one.** `v0.1.0a6` was tagged, released, signed, notarized
and handed to testers while its daemon could not start at all — a module-level SDK import raised
ImportError, so `run` died instantly. It passed CI because the smoke test only called `status`,
which imports almost nothing and reported providers, voice and engine all healthy. Tag-first
makes the promise before the check.

**So: a green `status` says nothing about whether the binary runs.** The smoke step now boots
the daemon and waits for it to bind, and that assertion is the one that matters. Keep it.

**Pushing the tag fires a SECOND build**, because of the trigger above. That is wasted minutes,
not a problem — but attach the artifacts from the run you VERIFIED, never from the tag's build.
PyInstaller output is not reproducible byte-for-byte, so they are different files.

## Working rules

- **Never wipe `$AGENTDUET_HOME`.** No `rm -rf` on it to "start clean" — correct the specific
  file or key instead. **To rehearse a fresh install, `./rehearse.sh park` / `restore`** moves it
  aside and back; a same-volume rename, so 16 GB of models is instant and nothing is destroyed.
  It touches ONLY what the app owns: `~/.connector` and `~/.agentduet` are never written by this
  app — `connector._from_file` only reads them, to prefill the wizard — so parking them defeats
  the purpose of the reset they look like part of. The first version of that script moved them,
  and the 2026-09-08 rehearsal duly reported the prefill as broken when the script had hidden
  its own input. Clearing it destroys the owner's setup, the knowledge they have built up
  by using the agent, and `run/secretary.pid`, after which a second launch cannot see the first
  and both fight for port 8899. Use a throwaway `AGENTDUET_HOME=/tmp/...` for experiments.
- **Check what is already running before starting anything** (`status`, or `ss -ltnp | grep 8899`).
  `--onefile` shows TWO processes per launch — a bootloader and its child — which is normal, not
  a duplicate.

## Git and releases — THIS REPO OVERRIDES THE GLOBAL RULES

The global conventions are written for the B3 microservices: several teams, a monthly deploy
train, and a prod that other people depend on between trains. None of that is true here. This is
a pre-1.0 product with one maintainer, no train, and releases cut by hand when there is something
worth shipping. Three of those rules are therefore OFF, and the reasons are not interchangeable —
do not generalise one into the others.

- **PUSH WITHOUT ASKING.** The global rule gates `git push` on the user typing "push", because
  there push is what lands work on a shared master that a train will carry to prod. Here the loop
  is push → CI → download → test, and a Windows or macOS binary CANNOT be built or tested any
  other way — PyInstaller does not cross-compile. Asking before every push makes a round trip out
  of a build step. So: commit finished work and push it.
- **STILL DO THE SQUASH REVIEW.** This is the part that stays. Before pushing, read
  `git log origin/main..HEAD` and reshape what should be reshaped — the test is unchanged: commits
  where a later one replaces what an earlier one built are one arc and belong together, and a
  commit a bisect should land on stays separate. Losing the ceremony around push is not licence
  to push a mess. Note that an already-pushed commit is NOT squashable, so the review is worth
  something only while the work is still local.
- **NO RELEASE-NOTES DRAFT TO MAINTAIN.** There is no per-push obligation to update a GitHub
  draft. Release notes here are written when a release is actually cut, from what the release
  contains. (The published notes are owner-facing prose — see *Releasing* above — not a changelog
  assembled a line at a time.)
- **NO `DEPLOY_CHECKLIST.md`.** There is no scheduled deploy, so there is no window between merge
  and deploy for an out-of-band step to get lost in. Anything that must happen around a release is
  in *Releasing* above, or it is an issue. Do not create the file.

**What does NOT change:** verify what shipped rather than trusting a version label, and test the
artifact rather than the source — both learned here and both still the whole point. See
*Releasing — build, VERIFY, then tag*.

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
8. **The owner site binds loopback only**, with a per-machine token. **THE SITE IS NO LONGER
   THE ONLY DOOR, since 2026-09-07** — a WhatsApp message from the number in `## Phone` reaches
   the owner's assistant, which holds the owner's tools. That door is authenticated by caller
   id: a real claim, since Meta authenticates the sending account, and weaker than the token,
   because a hijacked WhatsApp account inherits it. Stanley's call, made explicitly. It is
   WhatsApp only (a DDUET participant is an account uid, never a number), it fails closed with
   `## Phone` empty, and it logs every time it fires. Do not describe the owner surface as
   loopback-only without this sentence.
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
  `agentduet-desktop connect` remains for whoever wants it, and the 38 mcp tools are unchanged.
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
- **A LOCAL build takes CODE from `src/` but DATA from site-packages, so the build venv goes
  stale silently** (found 2026-09-08). The spec sets `pathex` to `src/`, so PyInstaller imports
  the current modules — but `collect_data_files("agentduet_desktop")` resolves the INSTALLED
  distribution, and `.venv-build` held `0.1.0a7`. Every local bundle I had made was current
  Python code running against **a7-era `web.html`, `app.css`, prompts and templates**. Nothing
  warns: the build is green, `--version` reads correctly off the source, and only the pages
  betray it.
  **So `pip install --force-reinstall .` into the build venv before any local build you intend
  to believe**, and treat a page or prompt that "did not change" in a frozen build as this
  first. CI is unaffected — it installs fresh from the checkout every run — which is precisely
  why it never surfaced.
  Corollary for a new data file: it must be listed in **both** `pyproject.toml`'s
  `package-data` and the spec's `collect_data_files` includes. The spec alone does nothing,
  because the file it is globbing is the installed copy.

- **`gh workflow run` builds the REMOTE, not your working tree.** A dispatch build fires against
  what is on `origin`, so a fix committed locally and not pushed is not in it. Caught after
  triggering a build to prove `app.css` was packaged, from a commit that did not have the fix —
  it would have gone green and proved the opposite of what was intended.
- **`timeout` IS NOT ON macOS.** It is GNU coreutils. A Mac with Homebrew coreutils has it, so
  it works locally and in every local script here, and a clean `macos-26` runner says
  `timeout: command not found`. The a11 build failed on exactly this: the smoke test's new TLS
  gate wrapped the download in `timeout 45`, the command did not exist, the download never ran,
  and the gate then reported "no bytes arrived over TLS — the binary reached nothing". **A false
  failure that reads exactly like the defect the gate exists to catch**, and it passed on Linux,
  where coreutils is standard. Background the command and poll for the condition instead — which
  is better anyway, since it can stop the moment the condition holds.

- **`pkill -f` matches your own command line**, including the shell running it. It has killed
  test blocks and daemons mid-run. Kill by PID or port.
- **SIGTERM is caught somewhere in the async stack** and does not always exit. `stop` verifies
  and escalates to SIGKILL; never report "stopped" on the strength of a signal sent.
- **`chmod 0600` is a no-op on Windows.** The model key in `$AGENTDUET_HOME/.env` is unprotected there.
- **Python ≥3.12** — the SDK requires it.

- **Assembling the native shell: hand it the `.app`, and sign AFTER assembling.** Two traps,
  each of which produces a launch failure that blames the wrong thing.
  **(1) Not the COLLECT directory.** Since macOS went `--onedir`, PyInstaller's bootloader sees
  it is inside a bundle (its own path contains `Contents/MacOS`) and loads Python from
  `../Frameworks` — so the libraries must be in `Contents/Frameworks`, not in the `_internal/`
  sibling the COLLECT tree ships. Hand `make-macos-app.sh` the COLLECT dir and the app launches,
  finds nothing and dies with `Failed to load Python shared library
  '.../Contents/Frameworks/libpython3.12.dylib'`, which reads as a broken build.
  **(2) A SIGNED daemon cannot be re-bundled.** A Mach-O at `Contents/MacOS/` has the bundle's
  `Info.plist` sealed into its signature. Copy one out of a signed bundle into a bundle with a
  different plist — the shell's declares `CFBundleExecutable=AgentDuet Desktop` — and the kernel
  SIGKILLs it: `exit=137`, and `codesign -v` says `invalid Info.plist (plist or signature have
  been modified)`. Nothing in the message suggests the cause. So: build unsigned, assemble, then
  sign the finished bundle. `build.yml` already runs in that order; it was a local sequence that
  got it wrong.

- **`--onefile` COSTS ~3.6 SECONDS ON EVERY LAUNCH, and that is the "slow app" complaint**
  (measured 2026-09-02 on an M-series Mac, a7). Time from launch to a bound owner site:
  **3.87s frozen, 0.23s from source** — the same code, so none of it is Python being slow.
  A onefile binary unpacks its whole 91 MB bundle into a temp directory before anything runs and
  then imports back out of a compressed archive, per launch, every launch. `--version` alone is
  0.82s vs 0.01s, and the gap grows with how much gets imported, which is why `run` is worse.
  **THE FIX IS `--onedir`, NOT SWIFT.** This is the one to say out loud, because the instinct is
  that a native shell would feel faster: `Daemon.swift` starts the same frozen binary, so the
  3.6s happens identically behind a nicer window. onedir lays the libraries out inside
  `Contents/` and nothing unpacks — which is also what a `.app` is supposed to BE, a directory of
  files rather than a self-extracting archive.
  **Linux must stay onefile.** INSTALL.md promises "the binary is a single file" and you cannot
  `chmod +x` a directory, so the spec needs a per-platform branch rather than a flag flip.
  Two consequences to plan for: signing covers many inner binaries instead of one (already
  handled — `sign-macos.sh` signs inner binaries first, deepest-last), and anything reading
  `sys._MEIPASS` now points at the bundle directory instead of a temp copy, so check
  `paths.EXAMPLES` and the wasm plugin path before believing a green build.

- **Local STT is faster-whisper on the CPU. The Apple Neural Engine is an INTENTION, not yet
  reachable** (checked 2026-08-17). CTranslate2, the runtime underneath, has CPU and CUDA
  backends only — no Metal, no Core ML, no ANE — so on a Mac it is CPU-only today and every
  measured number is a CPU number.
  **The design says "Apple Neural Engine" and that stays.** It is a target we have not hit yet,
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

## Where things are written down

- **This file** — how to work here: layout, working rules, invariants, gotchas that bite.
- **`docs/design.md`** — decisions and what would reverse them.
- **`docs/platform.md`** — what we know about Nexus, wss-edge and the SDK, and how we learned
  each thing. **Reference about somebody else's system**, which is why it is separate: it cannot
  be read off our code, and it is the most expensive knowledge here to re-derive.
- **The checklist below** — what is not built, who owns it, what would unblock it.

**The checklist keeps lying, and that is the thing to watch.** Five items in it have claimed
outstanding work that was already finished, and the OAuth entry said "ours is not built" for a
week after both sides were built — nearly costing a request to another team to build what they
had shipped. A list that lies gets skimmed, and then the real blockers on it get skimmed too.
Clear an item in the same commit as the work, not at the next review.

## Open — the checklist

Last reviewed 2026-09-07, after a8 shipped and a day of real WhatsApp traffic. Before that,
2026-08-11, after the WhatsApp swap, the rename, and dropping the assistant from
setup. Items that existed only because of the owner interface were removed on 2026-08-03 — see
the Cleared note at the end.

**Deck alignment:** the August onboarding flow is tracked per-step in `docs/onboarding-gap.md`,
which says which side of the line each gap sits on. This list carries only the parts that are
ours to build.

**The recorder — every gap between the agreed design and what runs**

Each of these is a STUB shipping now and a thing to build, not a design to trim. Nothing here is
optional-by-default: this list is what we agreed the product is, and since the mockup it was
derived from no longer exists, it is the only place that says so.

- [ ] **Single sign-on — PROVEN END TO END ON PROD, 2026-09-10. What is left is one deployed
      environment variable.** Tuan Vo deployed the prod endpoint (`AGENTDUET_OAUTH_URL=
      https://auth.agentduet.com`, no VPN) and it was driven through on a PARKED, genuinely
      empty instance: one click, and the install had an identity, a connector and a credential
      with **nothing typed and no `.env` at all** — `api_key=None`, `connector_uuid=None`, the
      connector arriving as a claim inside the token, channel live. That is the design's promise
      met. `about` reports `auth.agentduet.com (sign-in)`.
      **THE CONNECTOR CLAIM RIDES THE TOKEN FAMILY, which cost an afternoon.** Sign-in kept
      returning `9410b337-…` — the connector the DEV server minted on 2026-08-31 — instead of
      the production `bb27e3d4-…`, and it CONNECTED to `wss-prod` happily, so nothing looked
      wrong. Tuan Phan: "the connectorUuid is bound to token and i forgot to revoke the old
      token". Re-signing-in reissues the same claim until the old refresh chain is revoked. So
      after any connector change upstream, the token family must be revoked or the app keeps
      the stale connector — and a signed-in install IGNORES `AGENTDUET_CONNECTOR_UUID`, so
      there is no local override to fall back on.
      **Still to prove: that a WhatsApp message actually ARRIVES on a signed-in install.** A
      connector with no business account bound connects perfectly and receives nothing (see the
      2026-09-07 note under Connector provisioning), so "signed in and live" is not the same as
      "usable".
      Apple and Microsoft stay refused upstream — Entra does not issue `email_verified` — and
      both stay visible per the agreed design, saying so when pressed. The skip past them
      remains for an install where the variable is unset.
- [x] ~~**Record Call has nothing behind it.**~~ **THE AUDIO ARRIVED, 2026-09-09 12:28.** The
      first carried call to produce anything: a 12.6s caller leg and a 13.1s callee leg, both
      transcribed, merged into one stereo file and one labelled transcript. It was an OUTGOING
      call, which is also the first time `on_outgoing_call` has fired in anger. So the platform
      side of this is done and the entry that said otherwise is cleared — every empty recording
      before that date was this gap, not a defect in `carry.py`.
      **What it also proved, because none of it had ever run on real audio:** the `.start`
      sidecars earned their place (0.400s between the two legs' first frames, so the merge
      padded and made 13.1s of stereo from a 12.6s leg), and the index had a live bug — it
      globbed the owner's folder after the legs moved out of it, so the row named no files and
      the hub reported "No recording." over 1.2 MB of audio.
      **INBOUND IS PROVEN TOO, 2026-09-10.** This said it was unproven and had been true for a
      day. `calls.jsonl` holds two INBOUND carried calls from 2026-09-09 (12:48:40 and
      15:22:57), each with merged audio — 1.8 MB and 2.3 MB — and a labelled transcript, beside
      three outgoing ones. So `far, near` swapping by role is exercised in both directions and
      the direction-specific code is no longer written from one case only.
- [ ] **Record Message (SMS) does not exist at all.** We have WhatsApp through the SDK, not SMS
      archiving. This is a channel we do not ingest, not a screen we have not drawn.
- [ ] **Connect AI is a SUMMARISER in the design** — transcripts go to a cloud model for action
      items and summaries, after the call. That is not what `llm.py` does today, which is drive a
      live agent. The providers and key handling carry over; the feature does not exist.
      Its provider list also differs (OpenAI is offered, Qwen is not).
- [ ] **Accelerating Whisper — MEASURED 2026-09-09, and the answer is METAL, not Core ML.**
      This item argued a 1.3-1.5x ceiling because Core ML accelerates the ENCODER only. That
      reasoning is sound and it was aimed at the wrong route: `ggml` has a full **Metal**
      backend, so the whole model runs on the GPU rather than just the encoder.

      Measured on the real 2026-09-08 leg (18.0s of audio, the SAME `large-v3-turbo` we run,
      resampled to 16 kHz):

      | | wall | CPU | realtime |
      |---|---|---|---|
      | faster-whisper (current, CPU) | 10.69s | **36.40s** | 1.7x |
      | whisper.cpp + Metal | **0.90s** | **0.07s** | 19.9x |

      **12x the wall time and 520x the CPU**, and 0.07s of CPU is Apple's own 0.06s — so this
      is not a smaller win than Apple's engine, it is the same win with every Whisper language.
      `CTranslate2`, which faster-whisper runs on, has CPU and CUDA backends only and no Metal
      path: `get_cuda_device_count()` is 0 here and asking for CUDA compute types raises "not
      compiled with CUDA support". That is the whole reason today's numbers are CPU numbers.

      **`pywhispercpp` (MIT) is the shape to take**, and the packaging objection above mostly
      dissolves: PyPI carries prebuilt cp312 wheels for macOS arm64, Linux x86_64/aarch64,
      musllinux AND Windows, so there is no C++ build in CI and no per-model `.mlmodelc` to
      generate — `libggml-metal.dylib` ships in the wheel and links `Metal.framework`, and the
      runtime log says `use gpu = 1`. Dependencies are `numpy, requests, tqdm, platformdirs`
      against faster-whisper's ctranslate2 + tokenizers + onnxruntime. It REPLACES the engine
      rather than adding one, so it stays a single engine on every platform — Metal on macOS,
      CUDA/Vulkan on Linux, CPU everywhere.

      **And it carries per-segment timings** (`t0`/`t1` on every segment), which is the exact
      turn order the merged transcript wants — the whole approximate path in `transcribe.py`
      (mono downmix, difflib alignment, confidence floor, sentence snapping) exists only
      because an engine did not report them.

      Two things to check before committing to it: it refuses audio that is not **16 kHz**
      (we record at 24 kHz, so a resample goes in front of it — `audioop.ratecv` does it and is
      deprecated in 3.13, so pick a replacement), and on this one sample it transcribed a
      phrase belonging to the other party, which is either bleed in the recording or a
      hallucination and needs a second look on real audio. `mlx-whisper` is also MIT and
      Metal-native but pulls **torch** and is Apple-only, so it would reinstate two engines.
- [x] ~~**A chooseable storage folder.**~~ **DONE 2026-08-27.** `carry.RECORDINGS` was a module
      constant, which is exactly why the page could only display it — every importer froze it at
      import. It is `carry.recordings()` now, answered by `owner.recordings_dir()` at use time,
      set from the settings page through a native folder chooser (`reveal.pick_folder`), and
      `reveal.open_folder` opens it in the file manager.
- [x] ~~**Apple's own STT on the Neural Engine.**~~ **SHIPPED 2026-09-03, as the macOS default
      for English.** `SpeechAnalyzer` runs from a bundled Swift helper (`agentduet-stt`, its own
      SwiftPM target, copied into `Contents/MacOS` beside the daemon). Measured on a real
      222-second call from the bank sample:

      | | wall | CPU | chars |
      |---|---|---|---|
      | faster-whisper `large-v3-turbo` | 21.5s | **88.5s** | 729 |
      | Apple `SpeechAnalyzer` | **1.1s** | **0.06s** | 617 |

      19x faster for about a fifteen-hundredth of the CPU, comparable output, and it formats
      what a transcript needs: spoken digits became `91234567`, a spoken domain `b3networks.com`.
      A bare `en` resolves to the machine's own region, so a Singapore Mac gets **en-SG** — the
      accent Whisper has mistaken for another language.

      **THE LANGUAGE DECIDES THE ENGINE, and that is the whole design.** Apple has thirty
      locales — no Malay, Vietnamese, Tamil, Thai, Indonesian or Hindi — and NO language
      detection: told the wrong language it returns fluent nonsense rather than an error.
      Verified on a Vietnamese call in the same sample: Whisper produced a coherent transcript
      including the caller's name, Apple produced "wife guy, 18 charge book". So `## Transcription`
      left empty means "Apple where it has your language, else Whisper", naming a Whisper model
      means Whisper, and no setting can override the Language setting.

      Whisper therefore stays for every language Apple lacks, every older Mac, Linux and Windows
      — and for the pywebview fallback build, which compiles no Swift at all. `status` names the
      engine that will actually run, and says why when a Mac falls back.

      **What the language sweep found**, running Whisper's detector over all 29 real calls: one
      outright misdetection (Vietnamese at 0.577) and roughly eight more where English scored
      under 0.6. The detector is close to coin-flipping on this audio, which is the argument for
      naming the language rather than guessing it.

- [ ] **A designed app icon, and a transparent logo.** `agentduet-logo.png` landed 2026-09-08
      as the brand mark in all three pages, the favicon, and a generated `.icns` — replacing the
      letters "AD" in a blue square and, for the icon, replacing NOTHING: the bundle declared no
      icon at all, so Finder, the Dock and the DMG showed the blank generic application icon.
      Two limits in the asset itself, both wanting a designer rather than an engineer:
      its ground is **opaque white, not transparent**, so the pages put it on a white tile —
      keying the white out is not available, because the robot's eyes and the inside of the 'a'
      are white too and would be punched through; and the icon is therefore a **hard white
      square** where macOS convention is a rounded squircle, since an `.icns` defines its own
      shape and nothing masks it. A 1024x1024 icon with its own shape and a transparent mark
      fixes both, and then `app.css`'s `.brand .ad` becomes `background:none`.
- [ ] **Bundle Inter, JetBrains Mono and Material Symbols.** The pages now load all three from
      Google Fonts, as the design did. On a machine with no network the text falls back to a
      system font — fine — but **Material Symbols fails LOUDLY**: the ligature name renders as
      literal text, so a sidebar reads "grid_view call graphic_eq". Offline is most of what this
      product claims, so the font files belong in the binary. Not done yet because it is a
      packaging change and the design fidelity was the ask.

- [ ] **Per-service on/off toggles.** The design's overview switches each of the four services
      independently. We have one `## Calls` mode and a `## Record calls` boolean.

**Reaching out — links now, APIs when a link cannot carry it**

- [x] ~~**Post to Google Calendar, and draft an email.**~~ **DONE 2026-09-09**, as LINKS.
      `links.py` builds a `calendar/render` URL and a `mailto:` from TYPED FIELDS — the tool
      never passes a URL, which is `wasm_host.resolve_url`'s property moved to the owner's own
      screen. Both are owner-side and in `NEEDS_OWNER`. Verified end to end: Google Calendar
      opened the event editor prefilled, with the UTC range landing on the right local hour.
      **Neither creates nor sends anything** — the owner presses Save or Send. That is the
      feature's limit and the whole of its safety argument.
- [ ] **Emailing a transcript needs a real API, and therefore a Google token.** A `mailto:` is
      refused past ~1,800 characters (a three-minute call is about 3,400 encoded) and cannot
      carry an attachment at all. Our sign-in is federated, so this install holds an AgentDuet
      token and never a Google one — the route is `gmail.send` through a verified app of our
      own, or wss-edge brokering the scope. A platform decision; ask before designing around it.

**Updating — stage one of three is in**

- [x] ~~**Detect that a newer release is out.**~~ **DONE 2026-09-09.** A worker reads
      `/releases` four times a day (NOT `/releases/latest`, which excludes prereleases and so
      404s for this repo), caches to `run/update.json`, and the hub, the menu bar and `status`
      read that file. Nothing blocks startup, nothing polls on a request path, and a reused tag
      is caught by comparing the build stamp — a13 was overwritten, so version alone would have
      called a stale copy current.
- [ ] **Download and verify the DMG** (stage two), and a self-updater (stage three) only if
      stage two proves insufficient. **The trigger for stage two is testers not updating** — if
      a13 is still in use a month after a14, telling them was not enough.
      **Stage two is where "never act during a call" stops being free.** There is no in-call
      flag today; nothing needs one while nothing acts. A download competing with live call
      audio, or a restart prompt over a conversation, is the failure to design out — so whoever
      builds it adds the flag first.

**Being a Mac app** (decided 2026-09-02 — see `docs/design.md`, "Being a Mac app, not a
binary in a folder"). Ordered; each is worth doing alone.

- [x] ~~**`--onedir` on macOS**, onefile elsewhere.~~ **DONE 2026-09-02.** Launch to a bound
      owner site went 1.89s warm (3.87s cold) to **0.22s**, which is what the same code does
      from source — so the frozen overhead is gone, not reduced, and it holds even running off
      a read-only DMG. `Contents/MacOS` is a 17 MB launcher over 219 MB in `Frameworks`.
      Cost: the DMG grew 94 MB to 118 MB.
- [x] ~~**Menu bar item + `LSUIElement=1` + survive window close.**~~ **DONE 2026-09-02**, in
      the Swift shell. Stanley confirmed the icon appears, closed the window, and the daemon
      kept answering. The state line refreshes on every menu open — it was a launch-time
      snapshot at first, which would have said "Answering" with the daemon dead.
- [x] ~~**Launch the Swift shell on a Mac at all.**~~ **DONE 2026-09-02** — the first time it
      has ever run. Builds with `-warnings-as-errors`, starts its own bundled daemon, and signs
      as an assembled bundle (139 inner binaries) passing `--verify --deep --strict`.
- [x] ~~**`SMAppService` for login at start.**~~ **BUILT 2026-09-02**, as a "Start at Login"
      toggle in the menu bar menu. `SMAppService.mainApp`, so macOS launches the app itself —
      no plist to embed and no path to go stale — and it appears in System Settings → General
      → Login Items. It removes the legacy `~/Library/LaunchAgents` plist when enabled, because
      both registered means two daemons at login and the loser of the port race exits silently.
      **The toggle itself is not yet clicked**, so `.requiresApproval` handling is unproven.

- [ ] **A styled DMG window** — a background image with an arrow, and the window size and icon
      positions saved into the volume's `.DS_Store`. The `/Applications` alias landed 2026-09-03,
      which is the part that made the drag possible at all; this is the part that makes it
      obvious. Needs a designed image and Finder scripting to save the layout, so it is a
      separate job from the alias rather than the other half of one.

- [x] ~~**SHIP IT: `build.yml` still defaults to `shell=pyinstaller`.**~~ **DONE 2026-09-03.**
      A release now carries the Swift shell, so a tester gets the menu bar item, no Dock icon,
      the window surviving being closed, Start at Login and the 0.22s launch.
      **Flipping the default was NOT enough, and the reason is worth keeping:** a
      `workflow_dispatch` input default does not apply to a `push` event, and the release
      trigger is a tag push — so `inputs.shell` is empty there and `== 'native'` would have
      skipped the shell on every release while the default said otherwise. The gates read
      `!= 'pyinstaller'` for that reason, which `tests/test_rules.py` now pins.
      pywebview stays as the Windows and fallback path, so `[ui]` stays too, and
      `macos-shell.yml` is now redundant as a compile check — left in place rather than deleted,
      since it costs a couple of minutes and catches a Swift break without a full build.

**Release blockers**

- [x] ~~**`init` cannot take a connector.**~~ **FALSE, and it was false when written.**
      `init.connect()` prompts for the uuid and the key and verifies the pair with B3 before
      saving, and `init.main` reaches it at `connected = sign_in(interactive) or
      connect(interactive)`. All three surfaces take the pair: the settings page, the wizard's
      sign-in screen (since 2026-09-07), and the console. Checked 2026-09-08.
      Secrets still deliberately cannot go through the assistant — `save_connector` is outside
      `OWNER_TOOLS`, because a credential typed into chat goes to the model provider and lands
      in `owner_chat.json`. That part was and remains true.
      **This is the SEVENTH item in this file to claim outstanding work that was already
      done**, and it was found by spot-checking three open items at random — one of the three.
      That is the number to act on, not this item: the register is read to decide what to build,
      so a third of it being wrong is worse than any single entry on it.
- [ ] **Connector provisioning.** Every install needs its OWN `AGENTDUET_CONNECTOR_UUID` — one
      client per connector, and a second races `call.answer()`. A new user installs cleanly and
      then stops dead waiting on a human.
      **An answer is designed, not built** (2026-08-18): wss-edge auto-provisions a connector on
      first sign-in, keyed to the verified email, with no org involvement.
      **BUT OAUTH DOES NOT CLOSE THIS ON ITS OWN — proven 2026-09-07.** Signing in minted
      `bff72a4e-…`, which verified and connected perfectly and received NOTHING: three WhatsApp
      messages were delivered by Meta and vanished, because no business account is bound to
      that connector. A connector is necessary and not sufficient. The binding lives outside
      `wss-edge` (whose `ingestWAMessage` is TOLD the uuid by its caller) — `inbox` receives
      Meta's webhook at `POST /public/whatsapp/webhooks` and decides which connector to forward
      to. So provisioning must ALSO bind the new connector to a BA, or a signed-in owner gets a
      working channel that no message can reach. Ask Hallie, who owns the WhatsApp side.


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
      **UPDATE 2026-08-27: the renumber happened and DDUET is RELEASED.** `agentduet` `1.1.0b1`,
      `b2` and `b3` are on PyPI with `Network.DDUET`, `DduetMessage` and `SendDduetMessage` — so
      the reason this channel was dropped (a private branch) is gone. **The trap moved rather
      than closing:** they are PRE-releases, so PyPI still serves `1.0.0` as latest and our
      `agentduet>=1.0.0` resolves a clean install to a version with no DDUET at all. Our build
      venv has `1.1.0b3` only because it was installed explicitly. Pin it before relying on the
      channel — the failure is silent absence, not an error.
- [ ] **Windows binary.** Intel Mac was DROPPED 2026-08-04: `macos-13` is retired so the job
      never started, and a queued job holds its whole run open — finished builds looked
      unfinished for hours. A pre-2020 Mac cannot run our build; check the chip before sending.
- [x] ~~**Notarization.**~~ **DONE, and VERIFIED ON A MAC 2026-09-02.** The credentials were
      already in CI — the a6 release notes said "Signed and notarized" while this item still said
      the app was unsigned, which is the sixth time this file has claimed outstanding work that
      was finished. a7 ships signed, notarized and stapled: Gatekeeper answers `accepted /
      source=Notarized Developer ID` with a quarantine attribute applied, `stapler validate`
      passes, and it boots. INSTALL.md no longer tells anyone to right-click.
      Signing also works LOCALLY now (`packaging/sign-macos.sh`), so an artifact can be verified
      before a tag exists rather than after — see the Releasing section.
      **We are NOT going to the Mac App Store**, unchanged: it requires the sandbox, which this
      app's loopback server, home-directory writes and model download would each have to be
      granted around. Deferred, not rejected.

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
- [x] ~~**Pre-public scrub.**~~ **DONE 2026-08-26.** The two internal hostnames lived in a
      README that still described `secretary-sample` and the dropped DDUET channel, so it was
      rewritten rather than patched. No credentials were ever tracked — the only `sk-` match is
      the deliberate `sk-LEAKED-CANARY` in `test_wasm.py`, and the uuid and phone numbers are
      placeholders. A real colleague used as sample data is now a made-up one; `Pauline` stays,
      being fictional and the canonical example across five files. **This file is tracked on
      purpose:** ~99% of it is why-the-code-is-like-this, which is what a contributor needs.
      Only live identifiers are withheld.
- [ ] **OAuth sign-in — BUILT ON BOTH SIDES. What is missing is a deployed URL.**
      **This entry said "the backend contract is SETTLED, ours is not built" until 2026-08-31,
      and both halves were wrong by then.** wss-edge merged the whole thing to `main` on
      2026-08-25 — `vonhutuan-b3`, PR #53, "desktop OAuth sign-in — PKCE + federated login,
      rotating refresh tokens, Bearer at the SM-WS and REST doors", 33 files under
      `server/.../oauth/`, plus `./gradlew :server:oauthE2eTest` covering authorize, callback,
      provisioning, code exchange, refresh, PKCE-negative, code-replay and
      stale-refresh-revokes-family against a stub IdP. Tuan said he would build independently
      first and he did; nobody closed the loop back to us.
      **Ours is built too:** `oauth.py` has PKCE `begin()`, the token exchange, the store,
      `signed_in()`, `sign_out()`, `connector_uuid()`, and a loopback `/callback` on an
      ephemeral port — which is why their side carries a `LoopbackRedirectUriValidator`.
      **PROVEN WORKING END TO END, 2026-08-31, against the dev server.** Not a claim from
      reading code any more: a real Google consent screen, `SIGNED IN AS stanley@b3networks.com`,
      and a connector **auto-provisioned** — `9410b337-f753-4ab4-a566-48bbe6a62aaf`, minted by
      `createUserOwnedAppConnector` because that identity had none. Run in a throwaway
      `AGENTDUET_HOME`; the real instance kept `bb27e3d4-…` and grew no token store.
      **So the gap is one environment variable, and one deploy.** `AGENTDUET_OAUTH_URL` is unset,
      so `oauth.available()` is false and the sign-in buttons stay hidden.
      **Tuan, 2026-08-31: it is on the DEV server, behind VPN, "not on prod yet".** Being behind
      a VPN is what makes dev useless as the product path — the whole point is that an owner
      installs the binary and clicks Google, and only B3 staff can reach a VPN'd host.
      **"Needs VPN" is not true from THIS machine.** `wss-dev.internal.b3networks.com` resolves
      to `100.100.221.234` and answers on **:8080 over plain HTTP** — the same SD-WAN route that
      reaches `internal-apigw-eks` and the T4 box. Port 443 times out, which is what makes it
      look unreachable if you only try HTTPS. A GET to `/oauth/authorize` with our real
      `client_id` returns `302 → /oauth2/authorization/google`, so **`agentduet-desktop` is
      already a registered client there** and the loopback redirect passes validation.
      **THE TRAP, and it is a live-money one:** sign-in provisions a connector on WHATEVER
      ENVIRONMENT that URL points at (`createUserOwnedAppConnector`, only when the user has
      none). Point it at dev or staging and this install silently moves off its production
      connector — the one the DID and the production BA route to. Check
      `connector.environment()` before and after.
      **What it closes:** the "Connector provisioning" release blocker above. A new user signs
      in and gets a connector instead of stopping dead waiting on a human.
      Credential storage on Windows remains the open risk — see `docs/onboarding-gap.md`.
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

- [x] ~~Identity: does AgentDuet issue a stable identity, and carry the verified property?~~
      **ANSWERED 2026-08-28, from the Nexus protos — YES to both, and the shape matters.**
      - **Stable identity = `account_uid`.** Always on the relay as `senderAccountUid`, and the
        2026-08-10 design makes it required: "Every identity decision keys on an account uid."
      - **Verified property = `kyc_status`** (`NONE` / `VERIFIED`) on `BaChatUserInfo`. It is
        NOT on the relay — it comes from `GetBaChatUserInfo`, which the connector plane does not
        expose yet, so it is unreadable today.
      - **No anonymous sender exists.** `AccountType` is exactly `{UNSPECIFIED, PERSONAL, BA}` —
        no guest type — and reaching a BA through its public slug requires SSO. So a stranger
        arriving at a slug is PSEUDONYMOUS, not anonymous: a stable id always, a readable name
        sometimes, a verified identity only when KYC says so.
      **THE TRAP, and it will bite before anything else does: the email is a LABEL, not a KEY.**
      `people/<identity>.md` and `who_is(asker)` key on "their email or number" — fine for TELCO
      and WA, wrong for DDUET. The relay's `user_metadata` `{email, name}` is `orElse(null)`,
      absent entirely on BA-authored relays ("a BA account holds no email row"), and the design
      doc says outright that "`userEmail` is no longer a dependable identity" because it FLIPS to
      the staff member's address when a colleague replies as the BA. Key on `account_uid` and
      treat the email as display and matching only — otherwise a colleague's reply silently
      writes into the wrong person's file.
      `SELF_VOUCHING_NETWORKS` gained `"DDUET"` on the strength of the SSO requirement. That is a
      claim about AUTHENTICATION, not identity verification; `kyc_status` is the stronger signal
      and should override it the day it is readable.
- [ ] **Directory/discovery — REOPENED 2026-08-27, then DEFERRED 2026-09-09 with the people
      list above (same decision, same reason: no effort now, no use case yet).** It was closed on the
      belief that the DDUET surface was gone. It is not: `PostBaChatMessageRequest.profile_url`
      in `wss-edge`'s `nexus/mono/bachat/ba_chat_http.proto` is a **public BA slug**
      (`dduet.com/<slug>`), and a message may be minted against it INSTEAD of an account uid. So
      discovery is a URL anyone can hold, and the question is ours again: which slug an install
      gets, who mints it, and whether a stranger reaching it is a verified asker.
- [ ] **The DDUET people list and history exist in Nexus; the connector cannot reach them.**
      Found 2026-08-27 by reading `wss-edge`'s vendored
      `server/src/main/proto/nexus/mono/bachat/ba_chat_http.proto` rather than the SDK. Three RPCs
      are defined: **`GetBaChatUserInfo`** (`ba_uid` required; a blank `account_uid` returns the
      BA's WHOLE user list, paginated, sortable by display name), **`ListBaChatSessions`** (the
      inbox — members, title, `last_message_at`) and **`QueryBaChatMessages`** (history; a blank
      `session_uid` means every session the caller is a member of).
      **`wss-edge` wires exactly ONE baChat path — `internal/baChat/v1/agentPostMessage`.** The
      2026-08-10 adaptation design puts `agentListSessions`/`agentQueryMessages` under "Out of
      scope, deliberately", so this is a plumbing gap, not a missing capability.
      **DEFERRED BY DECISION, 2026-09-09 (Stanley, from the meetings): the SDK is NOT going to
      list people.** Not because it cannot — the RPCs above exist and the gap is plumbing — but
      because the effort is not going there now and NO USE CASE HAS TURNED UP YET. So there is
      no ask outstanding upstream; do not raise one, and do not design against its arrival.
      **What reopens it is a use case, not a capability** — the RPCs are not the blocker and
      never were. Kept in full rather than deleted, per the stub-first rule at the top of this
      file: the facts above cost a proto read to derive and this is a priority call, not an
      impossibility.
      **One limit is deliberate and worth not designing around:** `BaChatUserInfo.emails` is
      populated ONLY on a single-user lookup — "so a connector can turn one relayed userUid into
      an emailable participant, not so a whole customer list can be harvested in one call".
      **SO THE PEOPLE LIST IS OBSERVED-ONLY BY DESIGN NOW, not by limitation.** `list_people`
      derives from `tools.rows()` — the local `run/queries.jsonl` — unioned with whatever
      profiles the owner wrote, and `who_is` only ever reads `people/<identity>.md` off disk.
      Neither has ever called the platform, so the decision above needed no code change
      (checked 2026-09-09). There is no backfill path anywhere in the package.
      **The consequence to design around instead: UPTIME IS DATA.** Anything that arrives while
      the app is off is invisible permanently, `people/` is empty on a fresh install and always
      will be — there is no "import your history" step and now never will be — and a daemon that
      dies mid-day leaves a hole that NOTHING RECORDS. That last one is the same silent-failure
      shape as the rest of this file's 2026-09 findings: the gap produces nothing rather than
      something. Recording up/down spans so a thread can say "not recording between 14:02 and
      16:40" is the honest version, and is not built.
- [ ] **DDUET is BUSINESS-account chat, not the person-to-person app.** The distinction cost a
      wrong answer on 2026-08-27, so: `AddressNetwork` is exactly `{WA, TELCO, DDUET}`, and DDUET
      IS BaChat. `BaChatUserInfo` is documented as "the field set of
      `friend.FriendWithoutIdentitiesResponse`" — so Nexus has a separate **friend** module for
      person-to-person, and **`grep -rni friend` across all of `wss-edge` returns nothing**. A
      message someone sends you as a friend in the mobile app is never relayed to a connector.
      What DOES arrive is anything addressed to a BA the connector is a member of — including a
      human colleague's reply sent AS that same BA from web or mobile, which lands as ordinary
      inbound with `senderAccountUid == baUid`. Demoing this means messaging the BA, not the
      person.
- [x] ~~**Reply over WhatsApp.**~~ **BUILT 2026-08-11** — WhatsApp is now the messaging channel,
      not an unhandled network. `on_incoming_message` accepts `Network.WA`, replies with
      `SendWAMessage` in the `wa_echo_bot.py` shape (`_wa_text`, one helper so the asker reply and
      the owner's queued reply cannot drift), and `default_verified("WA")` is **true** — the
      number is proven at registration, and `SELF_VOUCHING_NETWORKS` had said "WHATSAPP" for
      months while the SDK enum is "WA", so the intent had never fired. It grants the profile and
      their own history; `knowledge/` is public to everyone either way, so disclosure is unchanged.
- [x] ~~**Confirm the INBOUND WhatsApp payload shape.**~~ **DONE 2026-09-07, and all three
      guesses were wrong.** `wss-edge` passes Meta's webhook envelope straight through
      (`WaInboundController` forwards `request.content.content`), so the body is four levels
      down: `entry[0].changes[0].value.messages[0].text.body`. `participant` is
      `contacts[0].wa_id`. None of the shapes `_first_text` accepted — flat `text.body`, a
      top-level `messages` array, the old Nexus `parts` — matched it, so the first real message
      would have been logged as unreadable. Read out of the platform's own logs rather than
      guessed, and the payload is a verbatim fixture in `tests/test_rules.py` because the
      nesting IS the finding. Every level is iterated rather than indexed at [0]: Meta batches
      entries and changes under load. Status webhooks (delivered/read) share the envelope with
      no `messages` array and are dropped by `wss-edge`, so we never see a delivery receipt.
- [ ] **Per-owner WABA.** **Shared sandbox number** — fine to test, unusable as product until
      per-owner numbers land (~September, on the platform side). The sandbox participant and
      `phone_number_id` are live identifiers and are kept out of this file; ask the platform team.
- [ ] Outbound initiate: messaging is reactive, so held replies are delivered only when the
      person next writes. On WhatsApp there is a second limit — Meta's 24h customer-service
      window, after which a free-form reply needs an approved template we do not have.
      **DDUET does not have either limit** (checked 2026-08-27): the 2026-08-10 BaChat adaptation
      gave the connector proactive send — omit `session_uid`, pass the target's account uid as
      `participant`, and Nexus mints the session. No window, no template. That makes DDUET the
      cheaper route to held-reply delivery than WhatsApp, and it is the strongest argument for
      carrying both channels rather than treating WA as the replacement.
- [ ] Unverified askers: `knowledge/` is flat and public. Decide the disclosure tier before
      strangers are in scope.
- [ ] **DashScope caps concurrent realtime connections per ACCOUNT** ("max_connections 100").
      It presents as SILENCE on the call — no error, the caller just hears nothing.

**Voice**

- [ ] **The tool contract** — and note the quarantine moved the subject: `search_knowledge`
      lives on the ASKER side (`voice.py`, `permissions.py`, `wasm_host.py`), not in
      `secretary_tools.py`, so this is a voice-path item and not an owner-surface one.
      status-and-render landed 2026-08-05, so every OTHER tool now
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
      **NOT YET, decided 2026-08-14.** `AgentDuet/agentduet-pipecat` (public) makes a
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

- [x] ~~**Carrying a call is built; what is missing is the platform handing us its audio.**~~
      **CLOSED 2026-09-09 — the audio arrived.** See the recorder section above for the call and
      what it proved. Kept in place rather than deleted because the topology notes below it are
      still the reference for how the two legs exist at all.
      **REWRITTEN 2026-09-08. This item used to say "We never touch audio: no `.wav`, no frames,
      nothing", and that has been false since 2026-08-14** — `voice._Recorder` wraps the
      ModelSession and writes `<stamp>-<call>-caller.wav` and `-agent.wav` for every ANSWERED
      call, tapping the SDK's own bridge so it needs no second consumer on the audio stream.
      The file contradicted itself about it: the grounding-check item above says "every answered
      call now writes the caller's audio, the agent's audio and a turn-by-turn transcript", 58
      lines earlier. An engineer trusting this entry would have built a recorder that exists.
      **What is actually left** is the CARRY path, not the answer path: `carry.py` originates
      Leg 2 and starts its recorders, but the platform does not hand the app conference audio,
      so the directory the panel lists stays empty — the same gap as "Record Call has nothing
      behind it" in the recorder section, which is where it is tracked.
      `examples/connect_spy_isolated.py` remains the working model — `connect(ring_time_seconds=…)`
      originates Leg 2, then both `call.caller.audio_stream()` and `call.callee.audio_stream()`
      are consumed to WAV. ("Spy" there is call-centre vocabulary for supervisor listen-in —
      `whisper()` speaks to the subscriber only, `barge()` to both — not stealth, and not the
      topology.)
      **The mode collision is still real and still unresolved:** `voice.register()` claims
      `on_incoming_call`, and one connector has one handler, so answering and carrying are
      mutually exclusive per install. That is a MODE, not a preference.
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
- [x] ~~`README.md` still describes `secretary-sample`, not this package.~~ **STALE — it was
      rewritten in the 2026-08-26 pre-public scrub and this line was not cleared.** It opens
      "Answers your phone, or records it — on your own machine" and mentions `secretary-sample`
      nowhere. That makes FIVE items in this file that claimed outstanding work already done.

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
