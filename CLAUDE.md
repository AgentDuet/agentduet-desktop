# CLAUDE.md — dduet-desktop

The release package for **DDuet Desktop**: a secretary that runs on the owner's machine, answers
external parties over the DDUET channel, escalates what it should not decide, and can act only
inside limits the owner declared.

Split out of `../secretary-sample/` on 2026-07-30. That folder still exists and still runs the
POC demo — **do not assume a change here is there, or vice versa.** They will diverge; this one
is the deliverable.

## Layout

```
src/dduet_desktop/        the framework — 23 modules, package-relative imports
  web.html sim.html       the owner's view, the channel simulator
  setup.html              first-run setup (2 steps)
  canvas-default.html     generic asker-facing surface for a capability with no page of its own
  templates/              seeded ONCE into $DDUET_HOME on first run, then owned by the owner
  examples/               working capabilities to copy from — NEVER installed
tests/                    test_rules (model-free), test_behaviour (drives a real model), test_isolation
packaging/                PyInstaller spec
entry.py                  frozen-binary entry point (a spec cannot use a console-script name)
```

Instance data lives in **`$DDUET_HOME`** (default `~/.dduet`): `settings.md`, `knowledge/`,
`canvas/`, `people/`, `permissions.json`, `capabilities.json`, `.env`, `run/`.

Build: `uv build --wheel` · `pyinstaller --distpath dist-bin packaging/dduet-desktop.spec`
(needs a build venv with the working SDK — see Blockers). ~35s, output `dist-bin/dduet-desktop`.

**Don't rebuild to iterate.** `./dev.sh` restarts from source in ~3s against the same
`$DDUET_HOME`, and the pages (`web.html`, `settings.html`, `setup.html`, `sim.html`) are
`read_text()` **per request** — an HTML change needs only a browser refresh, no restart and no
build. Rebuild only to test the real install or to ship. The site token survives a restart, so
an open tab keeps working either way.

## Working rules

- **Never wipe `$DDUET_HOME`.** No `rm -rf ~/.dduet` to "start clean" — correct the specific
  file or key instead. Clearing it destroys the owner's setup, the knowledge they have built up
  by using the agent, and `run/secretary.pid`, after which a second launch cannot see the first
  and both fight for port 8899. Use a throwaway `DDUET_HOME=/tmp/...` for experiments.
- **Check what is already running before starting anything** (`status`, or `ss -ltnp | grep 8899`).
  `--onefile` shows TWO processes per launch — a bootloader and its child — which is normal, not
  a duplicate.

## Invariants — enforced in code, not by convention

Break one of these and the product is a different product.

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

`tests/test_rules.py` covers most of these and runs with **no model and no venv** (127 checks).

## Decisions, and why — do not re-litigate without reading these

- **The site is the PRIMARY owner surface; MCP is secondary** (reversed 2026-07-30). MCP needs
  the owner to already have an AI app *and* configure it; the site needs nothing and is up
  whenever the daemon is. A dead site is therefore **fatal**, not a warning.
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
  `dduet_desktop` submodules explicitly. Never `collect_submodules("mcp")` — `mcp.cli` calls
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
- **`pkill -f` matches your own command line**, including the shell running it. It has killed
  test blocks and daemons mid-run. Kill by PID or port.
- **SIGTERM is caught somewhere in the async stack** and does not always exit. `stop` verifies
  and escalates to SIGKILL; never report "stopped" on the strength of a signal sent.
- **`chmod 0600` is a no-op on Windows.** The model key in `$DDUET_HOME/.env` is unprotected there.
- **Python ≥3.12** — the SDK requires it.

## Open — the checklist

**Release blockers**

- [ ] **Decide which repo is the SDK and publish it.** DDUET support exists only in
      `../wss-dduet` (`agentduet 1.0.1b1`); `../wss-sdk-python` (`1.0.0b9`) has `Network` but
      **no `SendDduetMessage`** and no DDUET anywhere. Neither is on PyPI. `1.0.1b1` sorts ABOVE
      `1.0.0b9`, so a version range would resolve to the older fork. Until then `pyproject.toml`
      cannot declare a real dependency and the binary bundles whatever the build venv has.
- [ ] **Windows and macOS binaries.** No cross-compiling; needs CI on each OS (GitHub Actions is
      disabled by org default — an admin must enable it). macOS needs notarization (Apple
      Developer ID) or Gatekeeper blocks it; Windows shows SmartScreen without a signing cert.
- [ ] **Connector provisioning.** Every install needs its OWN `AGENTDUET_CONNECTOR_UUID` — one
      client per connector, and a second one races `call.answer()`. `init` should obtain one.
- [ ] **Notifications are Linux-only** (`notify.py` → `notify-send`, falls back to stdout). No
      escalation alert on macOS or Windows, which for this product reads as broken.
- [ ] **Nothing starts at login.** "Answers while you sleep" needs launchd / Task Scheduler /
      systemd-user. Not written.
- [ ] **Credential storage on Windows** — use the OS credential store, or say plainly that the
      key is plaintext protected only by file mode.

**Backend, not this package** (one handoff document, four items)

- [ ] Identity: does DDUET issue a stable identity, and carry the verified property?
- [ ] Directory/discovery — how does someone find an owner to write to?
- [ ] Outbound initiate: DDUET is passive, so an agent cannot start a conversation. Held replies
      are delivered only when the person next writes.
- [ ] Unverified askers: with `knowledge/` flat and public, an unverified visitor reads
      everything. Decide the disclosure tier before strangers are in scope.

**Voice — decided, not started** (the phone number on the connector reaching the model)

- [ ] **Pick the realtime model and add a second slot** (`SECRETARY_VOICE_MODEL`). The attached
      text model cannot do audio, so calls need their own. Note: `qwen3.5-omni` supports tool
      calling and **`qwen3-omni` does not** — and tool calling is exactly what keeps a booking
      inside its bounds.
- [ ] **Wire an adapter from `../agentduet-adapters`** (`gemini.py`, `grok_voice.py`, `qwen.py`,
      `nova_sonic.py`). They already emit `TranscriptDelta(role="user"|"agent")` per completed
      turn and support function tools — this is integration, not invention. The SDK exports the
      whole call surface (`Call`, `CallAudioConfig`, `CallState`, `IncomingCallNotification`) on
      the SAME connector, so no second client is needed — confirm call events and DDUET messages
      coexist on one `SessionManager`.
- [ ] **Transcripts into the existing record**, via `memory.append` + `brain.record`, so a call
      appears in the conversation view, the escalation queue and the digest like any other
      exchange. Caller identity is the E.164 from caller ID and arrives VERIFIED (`TELCO` is in
      `people.SELF_VOUCHING_NETWORKS`); the conversation id is the call id.
- [ ] **A tool bridge to the same gates**: `search_knowledge` for grounding from granted folders,
      the capability path through `check_bounds` + `schedule.book`, and an `escalate` tool that
      records the item and returns something to say.
- [ ] **Decide what an escalation SOUNDS like.** "Stanley has it and will come back to you" is
      fine in chat and unacceptable with someone on the line: a holding phrase while it checks,
      then an answer or a callback promise. Also decide what happens when the owner IS at the
      machine and could take the call. This is the real design work, and it is larger than
      picking a model.
- [ ] **Post-hoc grounding check** on the transcript — flag claims not supported by what
      `search_knowledge` returned. The honest limit of speech-to-speech.

**Product**

- [ ] **Generic UX for managing capabilities and their forms/canvases** (deferred 2026-07-30 —
      the reason step 3 was cut from setup). `tools.list_examples` / `install_example` and the
      `/api/setup/example*` endpoints are what it should call.
- [ ] Owner sight of unverified askers — currently filtered out of the people list by default.
- [ ] Broaden the canvas offer: it fires only on the incomplete-capability path, so a bounds
      refusal never mentions the page.

**Engineering**

- [ ] **MCP face has drifted: 16 of 34 registry operations exposed.** Register from
      `tools.OWNER_TOOLS` instead of listing by hand. (Parked — MCP is low priority.)
      Also needs `mcp.server.fastmcp`, absent from the installed `mcp`.
- [ ] `dduet-desktop tool <name> [--arg=value]` — one generic verb dispatching into the registry,
      so every operation is scriptable without a second implementation.
- [ ] `test_behaviour.py` is **flaky** (~2 in 5 on "bare revision keeps the negotiation
      classification"): the capability extractor and the policy gate compete for the message. A
      single red line there is a signal to replay, not a verdict.
- [ ] No behaviour test asserts the configured **pronoun** reaches an answer — the bug that
      shipped, where `SYSTEM_PROMPT` had no pronoun slot at all.
- [ ] `paths.legacy_leftovers()` now misreports the shipped templates as deletable leftovers.
- [ ] `README.md` still describes `secretary-sample`, not this package.
