# CLAUDE.md — agentduet-desktop

The release package for **AgentDuet Desktop**: a secretary that runs on the owner's machine,
answers external parties over WhatsApp and by phone, escalates what it should not decide, and
can act only inside limits the owner declared.

Split out of `../secretary-sample/` on 2026-07-30. That folder still exists and still runs the
POC demo — **do not assume a change here is there, or vice versa.** They will diverge; this one
is the deliverable.

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

## Working rules

- **Never wipe `$AGENTDUET_HOME`.** No `rm -rf` on it to "start clean" — correct the specific
  file or key instead. Clearing it destroys the owner's setup, the knowledge they have built up
  by using the agent, and `run/secretary.pid`, after which a second launch cannot see the first
  and both fight for port 8899. Use a throwaway `AGENTDUET_HOME=/tmp/...` for experiments.
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
- **`pkill -f` matches your own command line**, including the shell running it. It has killed
  test blocks and daemons mid-run. Kill by PID or port.
- **SIGTERM is caught somewhere in the async stack** and does not always exit. `stop` verifies
  and escalates to SIGKILL; never report "stopped" on the strength of a signal sent.
- **`chmod 0600` is a no-op on Windows.** The model key in `$AGENTDUET_HOME/.env` is unprotected there.
- **Python ≥3.12** — the SDK requires it.

## Open — the checklist

Last reviewed 2026-08-03, after consolidating the plans into `docs/design.md`. Items that
existed only because of the owner interface were removed — see the Cleared note at the end.

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
      `B3Networks/wss-sdk-python` `feature/dduet-channel` (`1.0.1b1`), a PRIVATE repo, which is
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
- [ ] **Post-hoc grounding check** on the transcript. Nearly free once the tool contract lands.
- [ ] **Measure a hosted cascade.** Rejected 2026-07-31, but the recorded reason rejects a LOCAL
      cascade. It is the only option that restores every invariant.
- [ ] `_ring_owner` (the callback that rings the owner) has **never executed** — every other
      part of the callback is tested, and the ring is now rate-limited. It cannot be unit
      tested: it is a closure over the live `SessionManager`, and it opens a session, dials,
      and starts a second realtime model. The only way to exercise it is the real one — call
      your own number, ask for a callback, hang up, and see whether your phone rings.
      **Do that before anyone is told the callback works.**


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
- [ ] `paths.legacy_leftovers()` misreports the shipped templates as deletable leftovers.
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
