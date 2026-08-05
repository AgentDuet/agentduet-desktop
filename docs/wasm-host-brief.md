# Brief: the WASM tool host

Draft 2026-08-05, for review before starting. Delete once the work lands — this is a work order,
not a design document. The design is `docs/design.md`; nothing here overrides it.

## What to build

A module that runs a customer-authored JavaScript tool inside a WASM sandbox and returns a status
the framework renders. Everything it needs is already decided; see "Settled" below.

## The one test that has to exist first

**Write this before the shim, and watch it fail.**

A JS tool whose body tries to read the environment — `DASHSCOPE_API_KEY`, `AGENTDUET_API_KEY` —
and returns whatever it finds. The test asserts it gets **nothing**.

This is the failure mode that matters. Our environment holds the model key and the connector
credential, `wasmtime`'s default `WasiConfig` inherits it, and the JS engine *requires*
`environ_get` to load at all. So a shim that looks correct and silently passes the environment
through is the realistic mistake, and it is the one no other test would catch. It has to be a red
line, not a judgement call.

Same shape, cheaper, for the rest: a tool that tries to open a file, and one that tries to reach
the network. Each must fail, and the test must show *how* it failed.

## Settled — do not re-decide these

From `docs/design.md`, all recorded with reasoning:

- **`wasmtime`**, in-process. `wasmer` will not import on this platform; `extism` needs a native
  library we would ship ourselves.
- **One instance per CALL**, not per asker. A per-asker instance persists between calls, so
  anything the tool caches becomes a channel from one caller to the next.
- **JavaScript**, via Javy's `plugin.wasm` (1.3 MB, one artifact for every platform). It exports
  `compile-src`, so JS SOURCE compiles inside the sandbox — no compiler ships, no build step at
  tool-install time.
- **Status-and-render.** A tool returns a status from a closed set; the framework writes the
  sentence. Already implemented for the built-in five (`voice.SAY`, `voice._render`) — customer
  tools meet the same contract, not a new one.
- **The grant is checked before the sandbox exists.** `permissions.tools_for(caller, verified)`.
  The sandbox never decides who may call what.
- **A tool never touches a file.** No mounts. It calls host functions we wrote, and those apply
  the caller's permissions and hand back data. Reading knowledge goes through
  `permissions.context_for`; the tool sees text, never a path.

## The actual work, in order

1. **The failing tests above.**
2. **The WASI shim.** The engine imports `environ_get`, `environ_sizes_get`, `clock_time_get`,
   `random_get` and a set of `fd_*`. Grant clock and random. `environ_get` returns empty. `fd_*`
   over no preopened directories. The sandbox is exactly as tight as this shim — it is the work,
   not a detail of it.
3. **Load and run a tool**: instantiate `plugin.wasm`, `compile-src` the JS, `invoke` it, get a
   result back.
4. **Host functions**, the hatch through which a tool reaches anything.

   A local-file lookup is the FIRST TEST, not the first useful tool — `search_knowledge` already
   reads the owner's documents with the caller's permissions applied, so a tool that does the same
   duplicates the product. Use it to prove the machinery end to end, then stop calling it a
   feature.

   The first USEFUL tool reaches a system we do not have, over an API. That means egress, and
   egress is settled in `design.md`: an owner-approved allowlisted host, never a URL the tool
   supplies (a tool-supplied URL is an SSRF).

   **Egress is OUT of scope for this build** — see "Done means". Design the host-function seam so
   it can be added without reshaping anything, and stop there.

   When it is built, test against `api.open-meteo.com` — free, no API key, real JSON, ~860 ms from
   here. No key matters: it keeps credential handling out of the egress test. It also gives the
   negative test free — any other host must be refused by the allowlist.
5. **The return contract**: a status from a closed set, rendered by the framework. A tool that
   returns anything else gets `unavailable`.

   Add a status for a tool that broke. The caller hears that there is a technical problem AND that
   the message is still being passed on — decided by Stanley 2026-08-05, in preference to hiding
   it behind the ordinary holding line. Roughly: "I'm having trouble with that just now, but I'll
   pass your message to {owner}." The owner sees the real failure in the log; the caller never
   sees an error.

6. **How long a tool may take, and what the caller hears meanwhile.**

   The tool declares **`quick` or `slow`** — not a number. Decided 2026-08-05.

   A number is something a tool author guesses wrong, and it would still be wrong on someone
   else's network. The declaration is used for exactly one thing: whether to say "let me check
   that" before calling. `quick` says nothing and answers; `slow` speaks first.

   It fails safe. A tool wrongly marked `quick` costs a short silence and our cap catches it; a
   badly guessed worst-case number would have us announcing a wait on every fast call.

   The code decides everything else, and an untrusted tool must never be able to hold a caller on
   the line — that is the ring-limit problem in another form.

   - Past a short threshold (~2s), say something rather than leave silence.
   - Past a hard cap, stop waiting and hand over. The declaration never raises the cap.
   - On a message channel nobody is waiting, so the cap can be looser.

   **Unknown, check before promising it:** whether the SDK can play a hold message or music
   mid-call at all. If it cannot, the answer is a spoken line, not silence.
7. **PyInstaller.** `--collect-all` DOES NOT bundle wasmtime — the build succeeds and dies at
   runtime on `_libwasmtime.so`. Use
   `--add-binary "<site-packages>/wasmtime/<platform>/_libwasmtime.so:wasmtime/<platform>"`,
   verified in the spike. `plugin.wasm` is package data and needs collecting too. **Smoke-test a
   tool CALL in CI, not an import** — an import proves nothing here.

## Installing a tool needs the owner, explicitly

The assistant may WRITE a tool and show it. It may not switch it on. Installing is an explicit
owner action, like declaring a capability.

This is not ceremony. If an assistant can install a tool by itself, then anything that can talk to
the assistant can add one — and the assistant reads escalations written by strangers. That is the
failure the two-part split exists to prevent, arriving through the back door.

## Every tool call is logged

Which tool, which caller, what it returned, how long it took. Cheap to add now, impossible to
reconstruct later, and the first thing an audit asks for.

## Stop and report if any of these happen

Not "work around it". Stop.

- **The shim cannot deny `environ_get` without the engine failing to load.** That is a real
  conflict between the security property and the engine, and it needs deciding, not solving
  quietly.
- **The frozen build needs more than `--add-binary`** — a hook, a spec restructure, a vendored
  library. That changes the packaging story for every platform.
- **A wasmtime panic appears outside deliberate API misuse.** A panic aborts the process
  (SIGABRT, uncatchable), so in normal use it means the daemon dies mid-call. That reopens the
  in-process decision.
- **Any decision not already in `design.md`.** The design was settled deliberately over two days;
  an unrecorded choice made mid-build is how it gets quietly reversed.

## Rules

- **Never wipe or edit `$DDUET_HOME`.** It holds the owner's real model key and a connector uuid
  only a backend team can reissue. Use a throwaway `DDUET_HOME=/tmp/...`.
- **Do not start a second daemon.** One client per connector; a second breaks the live one.
  `SECRETARY_CHANNEL=0` for anything that needs the process but not the channel.
- **Do not `git push`.** Commit freely.
- **Do not `pkill -f`** — it matches its own command line and has killed the shell three times in
  this project. Kill by pid.
- Run `tests/test_rules.py` (202 checks, no model, no network) before every commit.
- Keep `.venv-build` intact — it holds the working SDK. Install experiments elsewhere.

## Done means

Scoped deliberately at the TEST tool. This build proves the sandbox, the shim, the return contract
and the frozen build — the part where a mistake is a credential leak. Egress is a separate, calmer
piece with its own decisions (allowlist format, who approves a host, what a timeout means when
someone else's server is slow).

- The three denial tests pass, **and were seen to fail first**.
- A JS tool runs end to end and returns a rendered status.
- The frozen binary runs a tool CALL, not just an import.
- Every tool call is logged; installing needs an explicit owner action.
- `docs/design.md` updated only where the build contradicted it.

NOT in this build: egress, a second host function, customer-facing installation UX.
