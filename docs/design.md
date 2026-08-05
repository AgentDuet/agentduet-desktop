# DDuet Desktop — the design

Single source of direction, 2026-08-03. Replaces `agents.md` and `service.md`, which were
written across a week of pivots and had begun to contradict each other and the code.

---

## The daemon is the product

**The asker daemon.** Always on. Holds the DDUET connection, answers calls and messages from
external parties, decides nothing it has not been authorised to decide. It ships with a working
secretary, and it takes **tools** as its extension point. This is the product; everything else
serves it.

**The owner mcp is secondary.** It is how the owner reads and drives the daemon, from an AI
assistant they already use. Administration, not the thing being sold. If it were the product, a
customer without an assistant would have bought nothing — and they have not.

**Two consequences of that ordering, both easy to get backwards:**

- **Administration cannot depend solely on the assistant.** "There is no owner interface" means we
  do not build and maintain one as a product surface — not that the only route in is MCP. The CLI
  (`init`, `status`, `connect`) and the transitional site both exist because an owner may have no
  assistant at all, and the product still has to be theirs to run.
- **The tool extension point is a first-class product concern, not a later nicety.** We ship the
  ability to add tools, so the security of tools someone else writes is core work: it is the one
  place where using the product as intended can create the vulnerability. See the fence, the
  customer-tools section, and `docs/tool-surface-risk.md`.

```
   stranger ──phone/DDUET──>  ASKER DAEMON  <──MCP──  owner's assistant
                              always on               (Claude Code / Goose)
                              5 tools                  33 tools
                              no OS reach
```

### Why no interface

Building one means building it three times — a browser page, a native window, and a macOS
bundle — and the native window is a third rendering engine that browser testing cannot check.
That was not theoretical: on 2026-08-03 a single missing global (`localStorage`, absent in
WebKitGTK) silently unwired the settings link, the quit button and the chat form, and the
unwired form destroyed the session token on the first keypress.

The owner already has an assistant. Riding on it costs one MCP server.

### The reversal, and what would reverse it again

This has flipped twice. It was MCP-first, then site-first on 2026-07-30 (*"MCP needs the owner to
already have an AI app; the site needs nothing"*), then MCP-first again on 2026-08-03.

Neither reasoning was wrong. What changed was the **assumption about who the owner is**. If that
assumption changes, revisit this decision — not the plumbing under it.

---

## Part 1 — the asker daemon

### The fence

The asker agent has **five** operations and no path to anything else: `search_knowledge`,
`book`, `request_callback`, `transfer_to_owner`, `escalate`. Adding a sixth is a change to the
fence and should be reviewed as one.

Enforcement is **in code, in the daemon**. Not MCP: MCP is a protocol between a host and a
server, and here the host would be our own voice loop — JSON-RPC to ourselves, on the call path,
for no boundary. An allow-list filtered in code gives the identical guarantee.

The principle underneath does not change: **the model reads, code decides.** Every judgement the
model makes is checked mechanically before anything happens.

### Customers will bring their own tools — and that inverts the fence

Recorded 2026-08-04, having been absent from this document while every decision in it assumed the
opposite.

Today a customer **declares**: an action from a closed `ACTIONS` set, with bounds from a closed
vocabulary. They parameterise our verbs and cannot supply behaviour. That is why the fence holds
with five tools — there is no path from a declaration to the machine.

The product intent is that customers author tools. Two things follow, and both are new classes:

**Their code runs sandboxed, never with our privileges.** Decided 2026-08-05: a WASM instance
per call — see below. In-process is fine; unsandboxed is not.

**Tool returns become untrusted input.** This is the one that is easy to miss. We currently trust
returns absolutely because we wrote them. A customer tool that reads their CRM returns whatever is
in that CRM, including text a stranger put there — arriving as a *tool result*, which a model
weights heavily. Same injection class as an asker message, through the channel we guard least.

So a customer's tool gets the treatment an asker's message gets, plus:

- **Handlers return a status from a closed set; the framework renders the sentence.** A key
  whitelist is not enough — it stops extra fields, not a leaked value in an expected one. If the
  handler cannot author caller-visible text, it cannot leak into it.
- **Authority stays ours.** Anything that commits goes through `check_bounds`, whatever the tool
  claims to have decided.
- **No egress by parameter.** A customer-supplied URL is an SSRF. Egress means an owner-approved
  allowlisted host.
- **Default deny.** A tool that declares no shape gets nothing.

The customer is writing an API without API-security experience, for a client that is an
attacker-steered model. Assume they will get it wrong and make that survivable.

### Tools are a granted resource, per caller

KC's proposal, 2026-08-04, accepted: *"Folder A can have tools XYZ. Folder B can only have tool
Z."* Disclosure already follows a per-caller grant. Authority follows the same grant.

`permissions.json` is already per-asker with a default, so this is `"tools"` beside `"folders"`.

Enforced at dispatch, as **two** checks that mean different things:

```python
if name not in ASKER_TOOL_NAMES:                    # does the product offer this at all
if name not in permissions.tools_for(caller, ...):  # may THIS caller use it
```

The registry is the fence and is fixed at build time. The grant is per caller and the owner sets
it. Neither substitutes for the other.

Three decisions inside it:

- **The default is `search_knowledge` and `escalate`.** Every stranger starts here, so it is the
  only setting that matters at scale. Never `book`, never `transfer_to_owner` by default.
- **It composes with `verified_only`, and does not replace it.** The grant says this caller may
  call `book`; `check_bounds` still says within these hours, this quantity, verified only.
- **`escalate` is not revocable.** Remove it and an agent facing a question it cannot answer has
  no legitimate move left — which is exactly when a model invents one. The safety valve must sit
  outside the system that can withdraw it.

### Customer tools run in WASM, one instance per call, written in JavaScript

Decided 2026-08-05. The alternatives were a sandboxed subprocess and a webhook to the owner's
server. Both are real isolation; both lose on the same grounds.

**WASM fails closed.** A module has no syscalls at all, so a capability we forget to grant makes
the tool break rather than escape. A subprocess is the reverse — it inherits our environment,
files and network, each of which must be remembered and stripped. For owners with no security
engineer the default matters more than the ceiling. It also costs no process spawn on the voice
path, and works offline, which a webhook does not.

**One instance per CALL, not per asker.** Two layers, and conflating them is how a sandbox leaks:
the **grant** is per asker (may this caller invoke this tool), the **sandbox** is per call (what
the code may touch while it runs). A per-asker instance persists between calls, so anything the
tool caches becomes a channel from one caller to the next. The per-asker part lives in what is
passed *in* — arguments, and host functions scoped to that caller.

**JavaScript**, because the author is not a programmer: an AI writes JS fluently, and a JS engine
in WASM is a tenth the size of a Python-in-WASM runtime every install would carry. The stack is
daemon → WASM runtime → JS engine → tool: two sandboxes, the engine's inside WASM's, on the same
assume-the-inner-one-breaks reasoning a browser uses.

**A tool never touches a file** — and, discovered 2026-08-05, it cannot call us either.

Javy's plugin imports **only WASI**. There is no host-function namespace, so the "host functions
are the only doors" model this document described is not available in this engine: a tool has
`data in -> compute -> data out` and nothing else.

**So tools are TWO-PHASE.** A tool that needs something it was not given asks for it and is run
again with the answer:

```js
if (!ANSWERS.stock) need({ kind: "stock", item: INPUT.item });   // round 1
else                result({ status: ANSWERS.stock > 0 ? "in_stock" : "out_of_stock" });
```

We see the request, decide whether to fulfil it, fulfil it ourselves with the caller's permissions
applied, and run the tool again with the answer added. Each round is a fresh instance.

This is stricter than host functions, not weaker. The tool never initiates anything — it states a
need and we choose. The `kind` comes from a closed set we implement, exactly like `ACTIONS`, so a
tool cannot invent a capability by naming one. And rounds are capped: an unbounded ask/answer loop
is the ring-limit problem in another costume.

The price is that a tool is re-run per round, so it must be a pure function of `INPUT` and
`ANSWERS`. It cannot hold state between rounds — which it could not anyway, since each round is a
fresh instance.

This is the part that matters more than the sandbox. A mount would hand over everything in the
folder for the whole call and make the sandbox responsible for security. Because tools only ever
get functions, the sandbox is the **second** line — the first is that the dangerous thing was
never handed over. Which is just as well, given the panic below.

Two things that surface late if not planned for:

- **No ambient JavaScript.** No DOM, Node, `fetch` or `require` — only host functions we grant. An
  AI will reach for `fetch` first, so the tool-writing prompt must enumerate what exists, or every
  generated tool fails with nothing the owner can act on.
- **The runtime is a native extension** — see the spike.

#### What the spike established (2026-08-05)

Run before building, precisely because the two risks below would have been expensive to find late.

- **`wasmtime` is the only option.** `wasmer` refuses to import on this platform ("not available
  on this system"); `extism` is a 0.1 MB wrapper over a `libextism` we would have to ship.
- **Deny-by-default is real, and it fails at LOAD.** A module importing a WASI syscall with
  nothing granted is refused with "expected 1 imports, found 0" — not at call time, when it would
  already be mid-answer. Granting WASI explicitly instantiates it. A computation-only tool runs
  with zero capabilities.
- **PyInstaller: `--collect-all wasmtime` DOES NOT WORK**, and fails in the worst way — the build
  succeeds, the binary is suspiciously small, and it dies at runtime on
  `Failed to load dynlib _libwasmtime.so`. The library is loaded through `ctypes` from a computed
  path, so PyInstaller never sees it. What works is explicit:
  `--add-binary "<site-packages>/wasmtime/linux-x86_64/_libwasmtime.so:wasmtime/linux-x86_64"`.
  Verified end to end in a frozen onefile build.
- **Cost: +9.4 MB** to a onefile binary (7.4 → 16.8 in the probe), better than the 28.5 MB the
  package occupies on disk.
- **A wasmtime panic ABORTS THE PROCESS.** Not a Python exception — SIGABRT, exit 134, uncatchable.
  Triggered accidentally during the spike by misusing the `Store` API. In-process therefore means
  **a runtime bug takes the daemon down mid-call**, and no `try/except` prevents it.

- **No JS compiler needs shipping.** Javy's `plugin.wasm` is **1.3 MB**, one artifact for every
  platform, and exports `compile-src` as well as `invoke` — so JS SOURCE can be compiled inside
  the sandbox at runtime. The alternative was shipping their 13 MB compiler per platform and
  making tool installation a build step. The JavaScript decision therefore costs 1.3 MB, not 13.
- **But the engine itself demands WASI**, and this qualifies the deny-by-default claim above.
  `plugin.wasm` imports `environ_get`, `environ_sizes_get`, `clock_time_get`, `random_get` and a
  set of `fd_*`. A bare module can be given nothing; a JS ENGINE cannot. So the guarantee is not
  "we grant nothing" — it is **what we put behind each import**:
  - `environ_get` must return EMPTY. Our environment holds the model key and the connector
    credential, and a default `WasiConfig` inherits it. This is the one that would leak silently.
  - `fd_*` over no preopened directories, so there is no filesystem to reach.
  - `clock_time_get` and `random_get` are harmless; grant them.

  The sandbox is exactly as tight as that shim. Writing it is the build, not a detail of it.

That last one is the only finding that argues against the decision above. It does not reverse it —
the subprocess variant costs a spawn on the voice path and inherits our environment by default —
but it means the in-process choice is a bet that we call the API correctly, and it should be
re-weighed if a panic is ever seen in normal use rather than in deliberate misuse.

### Voice is weaker than text, and says so

In text, `brain.handle_query` runs retrieval, the disclosure gate and `check_bounds` **before**
anything is said. Speech-to-speech has no such ordering — nothing can inspect a sentence before
it is spoken.

What survives, and what does not:

- **Action stays code-enforced.** Booking is a tool the model must call, and it runs the same
  `check_bounds` path as text. It cannot exceed the owner's declared limits however it is asked.
- **Disclosure becomes prompt-enforced.** Say this plainly to anyone who asks; do not imply the
  text guarantees carry over.

The fence on voice is therefore two pieces, and only one is a prompt:

| what | mechanism | when |
|---|---|---|
| standing rules — who you are, never invent, use these tools | prompt template → the session's `instructions` | once, at session open |
| per-answer grounding — say *this* | the **return value of a tool** | every turn |

`instructions` is set once, so a template cannot do per-turn grounding. **The open work is the
tool contract**: `search_knowledge` should return the sentence to say, not documents to
paraphrase. That shrinks the ungoverned step from "anything it knows" to "did it say what it was
handed", and makes a post-hoc check nearly free.

**Prompts are versioned files with declared parameters** (`prompts/asker-voice.md`,
`prompts.py`). The loader refuses blank, bracketed and placeholder-shaped values, because the
defect that reached a real caller was not a missing parameter but a plausible wrong one — the
agent introduced itself as `[Owner's Name]`.

Still open: the **hosted cascade** (STT → `brain.handle_query` → TTS) is the only option that
restores every invariant. It was rejected on 2026-07-31, but the recorded reason rejects a
*local* cascade — CPU too slow, T4 unacceptable. A hosted one has never been measured. It should
be given a number before it stays rejected.

### The daemon must not die with a UI

Today `secretary_agent.py` raises `SystemExit(1)` if the owner site fails to bind: *"This is the
owner's primary surface — stopping."* Under this design that is backwards. The asker daemon is
the product; nothing about an owner surface should be able to end it.

---

## Part 2 — the owner mcp

### 33 operations, derived not enumerated

Registered from `tools.OWNER_TOOLS`, never listed by hand. Hand-listing drifted to 16 of 33 —
and worse, imported a module `mcp` 2.x had renamed, so the face exposed nothing at all.

### Two servers, split by lifetime, not audience

Both serve the same host. They are separate for one reason: **a service cannot report being
stopped over its own endpoint.**

| | transport | lifetime | what it does |
|---|---|---|---|
| **service tools** | stdio, spawned by the host | per session | `service_status`, `service_start`, `service_stop`, login-item toggle |
| **secretary tools** | the 33 registry operations | see below | read and drive the instance |

On 2026-08-03 the daemon stopped and nobody noticed for twelve minutes — the log ends cleanly on
`inbound is live`. It was found by accident. Service tools are the fix, and with no UI they are
the **only** way to know it is alive.

**Open: does the secretary tools face need long-lived HTTP at all?** The HTTP argument assumed
the daemon already ran an aiohttp app for the site. Without the site that assumption is gone,
and per-session stdio may be sufficient and simpler. Decide before building.

If it does become HTTP, one thing is not optional: **stdio is implicitly access-controlled and
HTTP is not.** Only the spawning process can talk to a stdio server; a loopback HTTP endpoint is
reachable by anything running as the same user, including a script or a package `postinstall`.
Unauthenticated, that hands 33 owner operations to any local process.

### Starting at login, without building a persistence primitive

Per platform, all user-scope: a launchd plist, a systemd user unit, a Task Scheduler entry. It
registers the **daemon**.

Offered as a tool, it takes **no parameters**:

```
install_login_item()      # installs THIS binary, at its own resolved path
remove_login_item()
login_item_status()
```

Writing a launch agent is how software survives a reboot, which is how malware does too.
Parameterised and exposed to an agent that reads asker-authored text (see below), it is a path
from prompt injection to persistent autostart. With no arguments the blast radius is one
boolean. The tempting "accept a path for flexibility" version *is* the vulnerability.

It must report the file it wrote, and be idempotent.

---

## Configuration: the assistant does the interview, the terminal holds the secrets

**Secrets cannot go through the assistant.** `save_connector` is deliberately outside
`OWNER_TOOLS`: typing a credential into a chat box sends it to the model provider and writes it
to `run/owner_chat.json` in plaintext. That reasoning holds and is not overturned by removing
the UI.

But that is only true of **secrets**. Everything else the interview asks — name, pronoun, what
you do, what you may act on — goes through `set_setting`, `add_knowledge`, `declare_capability`
and `grant_folder`, all of which are in the registry. The assistant can run the whole interview,
and a conversation is a better interview than a list of CLI prompts ever was.

So the split is:

| | where | why |
|---|---|---|
| model key, connector credential | `dduet-desktop init`, at a terminal | a secret in a chat box goes to the model provider and lands in `owner_chat.json` |
| everything else | the assistant, via the mcp | it is a conversation, which is what the interview wanted to be |

### A UI for the secrets is fine — the rule is narrower than "no interface"

The rule is: **a secret must never enter a model's context.** A chat box violates it — the text
becomes prompt, reaches the provider, and is written to `owner_chat.json`. A browser form does
not: browser → daemon → `.env`, no model in the path.

So the decision is not "no UI ever" but **no full owner UI in three engines**. A single-purpose
secrets form is a different thing, and the site already implements it correctly.

Not relied on: `mcp` 2.x can request input from the user via the host (`elicitation`). The model
does not generate the value, but whether it lands in the model's context is the host's
implementation detail — too uncertain for a credential.

`setup_status` joins the two: it reports what is configured and echoes **no values**, not the key,
the connector uuid, or the owner's number, because anything it returns travels to a provider.

---

## The unresolved product question

**How does the owner learn about an escalation?**

Today: the site, or `notify.py`, which is Linux-only. With no site and no notifications, an
escalation reaches the owner **only when they think to ask their assistant.**

For "answers while you sleep" that may be fine — you check in the morning. But it is currently
implicit, and it is the core value of the product. Decide it deliberately: pull is acceptable,
or push is required and notifications must work on all three platforms.

---

## The risk this design must not hide

**Asker-authored text flows into the owner agent's context.** Escalation questions, briefings,
people notes, call transcripts — all written by strangers, all rendered into the prompt of the
agent with broad authority.

So the attack is not "convince the fenced agent". It is:

> a caller says something shaped like an instruction → it is recorded verbatim → the owner later
> asks "what's waiting?" → the privileged agent reads it.

**This design makes it worse, not better**, because the owner agent is now a general-purpose
assistant with shell access rather than a purpose-built chat with 33 scoped tools. "A human is
present" is thinner than it sounds: the human is reading a summary produced by the model that
just read the injection.

Therefore, a property of the mcp and not of either agent:

- **Everything asker-authored is returned tagged as untrusted content**, structurally delimited,
  never as prose the host agent could mistake for its own instruction.
- **The owner-facing prompt states the rule**: an instruction inside quoted asker content is
  content to report, never to follow.

This is the strongest reason the mcp is a good boundary — it is a place where sanitising can
actually be enforced.

---

## Installers

Per-platform, and the delivery concern now that there is no UI to carry the first run.

| | state |
|---|---|
| macOS arm64 | CI builds a `.app` in a DMG, smoke-tested for providers and voice. Unsigned — first launch needs right-click → Open |
| macOS Intel | **dropped 2026-08-04.** `macos-13` is retired, so the job never started — and a queued job holds its whole run open, making finished builds look unfinished |
| Linux x86_64 | CI builds it |
| Windows | not started |

Notarization needs an Apple Developer ID. Acceptable for a colleague, not past that.

**DDuet is the product; installing an assistant is optional.** A boundary, because it is easy to
drift across: we detect, offer, and configure what the owner chooses. If a change ever makes Goose
required or bundles it, that has crossed this line and needs deciding again.

Within that, **we do offer to install one** — having said "not doing" while shipping the opposite.
The objection stands (it picks a winner, and makes us a distributor of someone else's CVEs), but
an assistant is the only *comfortable* way to drive the daemon daily, so "bring your own" is a
dead end for someone who has never installed one. Not the only way: the CLI exists. Detection wins
by default. Their prebuilt release, never from git, nothing bundled. **Not Goose Desktop on
Linux** — deb/rpm only, both need root, and nothing else here does.

---

## Decisions

Only the ones without a section of their own — the rest are argued above and were being restated
here, which is how this document grew a second copy of itself.

1. **Prompts are versioned templates** with declared parameters and value checks.
2. **Voice grounding moves into the tool contract**; prompts carry standing rules only.
3. **The owner mcp is derived from `tools.OWNER_TOOLS`**, never enumerated.
4. **Service tools are separate from secretary tools**, because one must work when the daemon
   does not.
5. **The login-item tool takes no parameters.**
6. **Secrets never enter a model's context.** A terminal (`init`) or a single-purpose browser form
   are both fine; a chat box is not.
7. **The site is transitional, but the CLI is not.** The site stays until `init` covers first-run
   configuration, and is already not load-bearing — the daemon does not exit when it fails to
   bind. The CLI is permanent: with the mcp secondary, an owner with no assistant must still be
   able to run, configure and inspect their own product.

## Open

- Does the secretary tools face need HTTP, or is per-session stdio enough?
- Push or pull for escalations?
- Is the hosted cascade actually too slow? Unmeasured.
- Map our controls to the OWASP API Security Top 10 item by item? The thesis shows every issue we
  found lands on a named category; a formal mapping is what an audit tier would be sold on.

## Next

1. **Egress for tools.** A tool cannot call out at all today, so the first genuinely useful
   tool — one that reaches a shop system — is not yet possible. Settled shape: an owner-approved
   allowlisted host, never a URL the tool supplies. `api.open-meteo.com` is the test target (free,
   no key). Note this is now a REQUEST the tool makes and WE fulfil, not egress from the sandbox.
2. **Login-start units**, then the Windows installer.

Done items are not listed here. `git log` has them.

---

**Editing this document.** It records live decisions and the reasoning that would reverse them —
not history, not completed work, not a summary of its own sections. Each of those grew back at
least once.

**Sections are not dated, deliberately.** A date says when something was written, not whether it
is still true, and it would have caught none of the staleness found on 2026-08-04: this document
claimed we did not install an assistant while the product shipped one. Three things work better:

- **Date reversals, not sections** — where two statements contradict, the order is the information.
- **Anchor a decision to a test.** "The fence is five tools" cannot drift silently, because
  `test_asker_tool_surface` names those five.
- **State the boundary, not the behaviour.** A description of what we do now cannot be
  contradicted; a boundary can.
