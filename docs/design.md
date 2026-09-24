# AgentDuet Desktop — the design

Single source of direction, 2026-08-03. Replaces `agents.md` and `service.md`, which were
written across a week of pivots and had begun to contradict each other and the code.

**Before you change something, read the section that decided it.** Every one records what would
reverse it, so the fastest way to make a bad change here is to skip that.

| about to touch | read |
|---|---|
| **anything at all** | **Two products, one daemon** — it decides whether the rest applies |
| the asker's five tools | The fence · The risk this design must not hide |
| anything a customer's tool can do | Customers will bring their own tools · WASM · Reaching the outside |
| who may read or do what | Tools are a granted resource · Knowledge splits |
| how a call answers a question | Two models on a call · Voice is weaker than text |
| "this will be too slow" | **Latency on a call is a UX problem, not a wall** |
| secrets, or adding a UI | A UI for the secrets is fine |
| the mcp, or starting/stopping | Two servers · Starting at login |

---

## Two products, one daemon

Recorded 2026-08-17, and it governs how the rest of this document is read.

**The recorder.** A call is carried through to the owner — the inbound leg terminates on us and
we originate a second leg onward — and both sides are recorded, then transcribed on the owner's
machine. **Two humans talk. Nobody is answered, nothing is decided.** This is what a new install
is, and what setup configures by default.

**The secretary.** The agent picks up, speaks for the owner, and may act. Everything below about
the fence, disclosure, bounds and escalation exists for this, and applies only to this.

**Why the distinction is load-bearing.** Every invariant in this document governs what an agent
may SAY or DO on someone's behalf. On the recorder path most of them have no subject at all —
nothing is disclosed, nothing is committed, no bounds are checked. Requiring a recording change
to satisfy a fence built for a different product is how a three-week feature becomes a
three-month one, and it is what kept the simpler product unshipped while the harder one was
being fenced.

So: **if a change has an agent speaking or acting, the fence is mandatory. If it does not, do not
reach for it.**

**Where they genuinely collide:** `voice.register()` claims `on_incoming_call`, and one connector
has one handler. Answering and carrying are therefore mutually exclusive per install — a MODE,
set in `settings.md`, not a preference.

**What this does not mean.** The secretary is not deleted and nothing here is relaxed.
`tests/test_rules.py` enforces it, and it all applies the day an agent speaks on a call.

### The daemon is the product

**The asker daemon.** Always on. Holds the AgentDuet connection, carries or answers calls and
messages from external parties, decides nothing it has not been authorised to decide. It takes
**tools** as its extension point. This is the product; everything else serves it.

**The owner mcp is secondary.** It is how the owner reads and drives the daemon, from an AI
assistant they already use. Administration, not the thing being sold. If it were the product, a
customer without an assistant would have bought nothing — and they have not.

**Two consequences of that ordering, both easy to get backwards:**

- **Administration cannot depend solely on the assistant.** The mcp is one route in, never the
  only one. The CLI
  (`init`, `status`, `connect`) and the site both exist because an owner may have no assistant at
  all, and the product still has to be theirs to run.
- **The tool extension point is a first-class product concern, not a later nicety.** We ship the
  ability to add tools, so the security of tools someone else writes is core work: it is the one
  place where using the product as intended can create the vulnerability. See the fence, the
  customer-tools section, and `docs/tool-surface-risk.md`.

```
   stranger ─phone/WhatsApp─>  ASKER DAEMON  <──MCP──  owner's assistant
                              always on               (Claude Code / Goose)
                              5 tools                  38 tools
                              no OS reach
```

### The owner surface, and why this has flipped three times

**Current answer (2026-08-11): the site is load-bearing, and the mcp is optional.**

The history matters only because the *reason* keeps being the same one. MCP-first, then
site-first on 2026-07-30 (*"MCP needs the owner to already have an AI app; the site needs
nothing"*), then MCP-first on 2026-08-03, then site-first again on 2026-08-11.

**What changes each time is the assumption about who the owner is.** Packaged for a small vendor
handed a binary, the owner does not have Claude Code or Goose and should not install one to
finish setting up a phone answering service. So setup no longer mentions an assistant,
`init` runs the interview itself, and `status` says nothing when none is registered.

If that assumption changes again, revisit this decision — not the plumbing under it.

**The cost of a UI is real and has not gone away**, so it is bounded deliberately: a browser page
is the surface, and the native window renders the same pages rather than being a second
implementation. The hazard that proved this is worth remembering — on 2026-08-03 a single missing
global (`localStorage`, absent in WebKitGTK) silently unwired the settings link, the quit button
and the chat form, and the unwired form destroyed the session token on the first keypress.
Anything the pages rely on must survive three engines, or be avoided.

**Which surface is documented depends on the platform** (2026-08-14). macOS and Windows set up in
the page; **Linux sets up in the console**, because that is a machine someone reached over ssh and
opening a loopback browser page is the awkward path there. Both surfaces ship everywhere.

**The consequence that bites: `init` must cover what the page covers.** It has drifted in both
directions — the page gained a mode question and a speech-model download while `init` had
neither; later `init` gained a language question the page lacked, and language decides whether an
English call comes back as fluent Malay. `tests/test_rules.py` now checks the two cover the same
fields, because remembering did not work.

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
opposite. Today a customer **declares** — an action from a closed `ACTIONS` set, with bounds from a
closed vocabulary — so there is no path from a declaration to the machine, and that is why a
five-tool fence holds. If customers author tools, that property is gone.

**The consequence that is easy to miss: tool returns become untrusted input.** We trust returns
absolutely because we wrote them. A customer tool reading their CRM returns whatever is in that CRM,
including text a stranger put there — arriving as a *tool result*, which a model weights heavily.
Same injection class as an asker message, through the channel we guard least.

So a customer's tool gets the treatment an asker's message gets, plus four rules, each detailed in
its own section below:

- **Sandboxed, never with our privileges** — a WASM instance per call.
- **A status from a closed set; the framework writes the sentence.** A key whitelist stops extra
  fields, not a leaked value in an expected one. If a handler cannot author caller-visible text, it
  cannot leak into it.
- **Authority stays ours.** Anything that commits goes through `check_bounds`, whatever the tool
  claims to have decided.
- **It reaches the outside by NAME, never by URL**, and a tool declaring no shape gets nothing.

The customer is writing an API without API-security experience, for a client that is an
attacker-steered model. Assume they will get it wrong and make that survivable.

### Reaching the outside: the owner names the destination, the tool names the name

Corrected 2026-08-05 after Stanley pushed on it. An earlier draft had US implementing a closed
set of request kinds — a "weather" kind, a "stock" kind. That is too restrictive to be the
product: every customer has a different system, and they would all be waiting on us to support
theirs.

**The security property is narrower than that.** What prevents SSRF is not that we choose the
destination. It is that **the tool cannot choose it at call time, while a stranger is talking to
it.** The destination has to be fixed in advance — and the person who should fix it is the owner,
who is already deciding they want this tool.

So the endpoints are part of the proposal, and approving the tool approves them together:

```
Proposed 'weather_check'. NOT active.
It wants to reach:
    forecast  ->  https://api.open-meteo.com/v1/forecast

Approve with:  agentduet-desktop tools approve weather_check
```

**What the approval step is, and what it is not.** It is the moment the owner fixes the
destinations — that is the whole SSRF property above, and it is enforced in code: `resolve_url`
resolves a name against the approved manifest and ignores a `url` field entirely.

It is **not** a barrier against the owner's own assistant. `approve()` copies a file from
`pending/` into `tools/`; anything that can write `$AGENTDUET_HOME` installs a tool without
going near the CLI, and the same is true of `permissions.json` and `capabilities.json`. Being a
CLI command only keeps it out of reach of an assistant that has neither a shell nor write access
to that directory — and Claude Code always has Bash, Cowork writes to folders the owner
connects, and Goose's shell is one toggle away. **The control that matters is whether
`$AGENTDUET_HOME` is reachable by the assistant at all**, and that is the owner's host
configuration, not something this package enforces.

This is worth stating rather than leaving implied, because the owner's assistant reads
asker-authored text — escalations and threads — so it is a prompt-injection target, and
"approval is a separate step" would otherwise read as a defence it is not. Making it one means
requiring something no agent can produce: a code delivered on the owner's phone over the
product's own channel and typed back. That converges with outbound, which is unbuilt.

At runtime the tool asks by NAME:

```js
need({ kind: "fetch", endpoint: "forecast", params: { city: INPUT.city } });
```

**Why a name and not an allowlisted URL.** If the tool passed a URL and we checked it against a
list, we would be filtering attacker-influenced input, and URL checking is a classic place to be
fooled — redirects, encodings, hostnames that resolve to an internal address. Passing a name
means there is no URL to check: the tool cannot express `192.168.1.1` at all, because it cannot
express a URL.

It also puts the decision in one place. The owner sees the code AND what it may reach in the same
act of approval, rather than in a separate config that drifts away from the tool it governs.

### Google Calendar and Gmail: a LINK, and the same property one layer out (2026-09-09)

The assistant can put a prefilled Google Calendar event or an email draft on the owner's screen.
Both are links. Neither creates or sends anything: the owner presses Save in Google Calendar, or
Send in their mail client.

**Why not the APIs.** Both need an OAuth token with a scope Google treats as sensitive
(`calendar.events`, `gmail.send`), which means a verified app and a consent screen — and our
sign-in is FEDERATED. The app authenticates to wss-edge and wss-edge talks to Google, so what an
install holds is an AgentDuet token, never a Google one. There is nothing here to call Google
with. A link needs nothing: the owner is already signed in in their browser.

**The security property is the one above, moved one layer out.** `wasm_host.resolve_url` holds
that a tool cannot express a destination because there is no field in which a URL means anything.
`links.py` holds the same thing for the owner's own screen: the tool passes TYPED FIELDS — title,
start, end, notes, recipient, subject, body — and our code builds the URL from literals. So a
title carrying `&action=DELETE` and an attacker's address arrives as one percent-encoded value.
There is deliberately no public `open_url(url)` anywhere in the tree; the opener is private to
that module, for the same reason `reveal.py` refuses to open a path it is handed.

Both tools are owner-side (absent from `_tool_declarations()`) and in `NEEDS_OWNER`, so once a
stranger's words are in the assistant's context they can only be PROPOSED. They commit nothing
either way — but a draft that appears unasked-for in front of the owner reads as one they
half-remember writing, and that is enough to be worth a click.

**What would reverse it.** Attachments, or a transcript that has to travel. A link cannot carry
either: a mailto URL is refused past ~1,800 characters, and a three-minute call is about 3,400
encoded. The day someone needs the transcript emailed, the answer is not a longer URL — it is a
real Gmail API call, which means a Google token, which means either a verified app of our own or
wss-edge brokering the scope. That is a platform decision, not a desktop one.

### Detecting a new release: tell, do not update (decided 2026-09-09)

Three stages, and each is separately useful: **detect and tell**, then **download and verify**,
then a self-updater (Sparkle) only if the second proves not to be enough. Stage one shipped
alone, because it is the only one that cannot go wrong on the owner's machine — it changes
nothing, so the worst case is a stale line.

The constraints are what shape it, and they come from the product rather than from GitHub: the
daemon must bind with **no network at all** (a self-hosted box with no route out is a supported
install, not a broken one), the API allows **60 requests an hour** unauthenticated for a whole
NAT, and nothing may act **during a call**. So the check is a task that sleeps first, `state()`
reads a cached file and never opens a socket, and the answer lives in `run/update.json` where
the macOS shell reads it directly instead of polling GitHub a second time.

**Two API traps, both of which return a confident wrong answer rather than an error.**
`/releases/latest` excludes prereleases, and every release of this project is one — so it 404s
here and a check built on it reports "no releases" forever. And a REUSED TAG defeats a version
comparison: a13 was overwritten rather than superseded, so an install can be behind the release
carrying its own version number. The build stamp (`__built__`, baked in by the spec) is what
separates them.

**It says nothing when there is nothing to say.** No "up to date" line — that is a claim about
GitHub made from a cache, and on a machine that has never reached it, a wrong one.

**What would reverse the stop-at-stage-one part:** testers not updating. If a13 is still in use
a month after a14 ships, telling them was not enough and stage two earns its keep. Stage two is
also where "never during a call" stops being free — there is no in-call flag today, and whoever
builds it adds one.

### Tools are a granted resource, per caller

A colleague's proposal, 2026-08-04, accepted: *"Folder A can have tools XYZ. Folder B can only have tool
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

Decided 2026-08-05, built the same day. The alternatives were a sandboxed subprocess and a webhook
to the owner's server; both are real isolation and both lose on the same grounds.

**WASM fails closed.** A module has no syscalls at all, so a capability we forget to grant makes the
tool break rather than escape. A subprocess is the reverse — it inherits our environment, files and
network, each of which must be remembered and stripped. For owners with no security engineer the
default matters more than the ceiling. It also costs no process spawn on the voice path, and works
offline, which a webhook does not.

**One instance per CALL, not per asker.** Two layers, and conflating them is how a sandbox leaks:
the **grant** is per asker (may this caller invoke this tool), the **sandbox** is per call (what the
code may touch while it runs). A per-asker instance persists between calls, so anything the tool
caches becomes a channel from one caller to the next.

**JavaScript**, because the author is not a programmer: an AI writes JS fluently, and Javy's engine
is 1.3 MB — one artifact for every platform, and it compiles JS source *inside* the sandbox, so no
compiler ships with the product.

**Tools are TWO-PHASE, because a tool cannot call us.** Javy's plugin imports only WASI: there is no
host-function namespace, so the "host functions are the only doors" model this document once
described is not available. A tool that needs something asks for it and is run again with the
answer:

```js
if (!ANSWERS.stock) need({ kind: "stock", item: INPUT.item });
else                result({ status: ANSWERS.stock > 0 ? "in_stock" : "out_of_stock" });
```

Stricter than host functions, not weaker. The tool never initiates anything; the `kind` comes from a
closed set we implement, so it cannot invent a capability by naming one; a refusal arrives as an
absent answer it must cope with; and rounds are capped, because an unbounded ask/answer loop is the
ring-limit problem in another costume. The price is that a tool is re-run per round and must be a
pure function of `INPUT` and `ANSWERS` — which it could not avoid anyway, each round being a fresh
instance.

**A tool never touches a file.** No mounts. Reading knowledge goes through the same
`permissions.context_for` that governs the built-in tool, and returns text, never paths.

#### Three things that will bite whoever touches this next

- **The sandbox is only as tight as the WASI shim.** "Grant nothing" is not available: the JS engine
  *requires* `environ_get`, `clock_time_get`, `random_get` and a set of `fd_*` to load at all. So the
  guarantee is what sits behind each one — `environ_get` returns EMPTY, and no directory is
  preopened. `wasmtime`'s default config inherits the parent environment, which holds the model key.
- **Every JS-level denial test passes with the shim wrong**, because QuickJS exposes no `process`
  whatever WASI holds. Those probes prove the engine is minimal, not that we configured anything.
  The shim is pinned by a source check for that reason.
- **A wasmtime panic ABORTS THE PROCESS** — SIGABRT, uncatchable, not a Python exception. In-process
  therefore means a runtime bug takes the daemon down mid-call. This is the one finding that argues
  against the decision above. It does not reverse it, but the in-process choice is a bet that we call
  the API correctly, and it should be re-weighed if a panic ever appears outside deliberate misuse.

Packaging is a trap and the remedy is in `packaging/agentduet-desktop.spec`: `--collect-all wasmtime`
does not bundle the native library, and the build succeeds and dies at the first real call. `status`
therefore runs a real tool rather than importing the runtime.

### Carrying a call: we are the junction, not a tap

Recorded 2026-08-14, built the same week. The topology is a **back-to-back user agent**: the
inbound leg terminates on us and we originate a second leg onward to the owner's phone or PBX.

```
Telco ──▶ Leg 1 ──▶ AgentDuet WSS ◀──▶ the app on the owner's machine
                          │
                          ▼
                       Leg 2 ──▶ the owner
```

**Nothing is attached to somebody else's call, because we are the junction.** That is why
recording is available at all rather than being a permission we must obtain — the media is ours
by construction, and `call.caller` and `call.callee` are simply the two legs. `connect()` takes
no destination, because the destination is the connector's configured target.

**None of the fence applies here**, and that is the point of the split at the top of this
document: no knowledge lookup, no disclosure decision, no `check_bounds`, because nobody is
answered.

**The custody question gets BIGGER, not smaller, which is easy to get backwards.** The secretary
holds only what the owner told it to say. This holds everything both parties say — the owner's
customers, in conversations we are carrying. Be precise about the answer rather than
overclaiming: the app runs on the owner's machine, so recordings are **stored** only there. The
media still transits the platform to reach it, so *"never leaves your machine"* is false and
*"stored only on your machine"* is both true and the stronger claim.

**Consent is unresolved and is not addressed by anything else in this document.** Recording two
people has jurisdiction-specific rules, and every invariant here governs what the agent may say
or do — none of them ask whether the other party agreed to be in the conversation. Same class of
question as outbound campaigns. Open.

**Transcription is post-call, on a queue, and the queue is the filesystem** — a `.wav` with no
sibling `.txt` is pending work, which is restart-safe with no state to corrupt. It runs on the
owner's machine by default; a model key only switches it to a hosted engine.

**Set the language.** Left to guess, both shipped speech models identified a Singapore English
call as Malay — at 0.95 and 0.87 confidence — and did not garble it. They TRANSLATED, fluently,
and the meaning inverted: *"can I waive my credit card bill"* came back as *"can I pay my bill"*.
A wrong transcript that reads perfectly is worse than a broken one, because nothing about it
looks wrong.

### Two models on a call, and why one instruction could not do both jobs

Two models are needed for a call, and they are not two brains. One holds the conversation; the
other does the looking up.

- **The voice model** — a realtime omni model over a WebSocket. Holds the call.
- **The knowledge model** — an ordinary completion model. Answers one question at a time.

The text model cannot process audio, so this is not a choice. But the more useful point is that
their INSTRUCTIONS have almost nothing in common:

| | the voice model's instruction | the knowledge model's instruction |
|---|---|---|
| about | **manner** — how to behave on a call | **substance** — what may be said |
| contains | speak briefly, say the name as written, never invent, never agree, offer a callback | these documents, this caller's permissions, is it answerable or must it escalate |
| set | **once**, at call start — it cannot be changed mid-call | **rebuilt every request** |
| so it can hold | standing rules only | this specific question, and this caller's material |

That last row is why they could not be one instruction even if one model could do both jobs. One
is fixed for the whole call; the other is fresh each time.

**The second model is stateless and does not see the call.** It is the receptionist pausing to look
something up in a book: the book does not know who is on the phone or what was said a minute ago.
So the voice model must ask a SELF-CONTAINED question — "do you offer weekend appointments", not
the caller's "what about the other one?". A tool call already forces exactly that, which is why the
mechanism needed is the mechanism we have.

**Same line as the knowledge split below, drawn once:** the core goes in the voice instruction and
answers with no round trip; the pile is the knowledge model's job, prefaced with "let me check".

**The objection that survives is not cost or latency.** A text call is fractions of a cent, and
latency is answerable by speaking first. What remains is the DashScope **per-account** connection
cap — we hit `max_connections 100` in testing, and it presents as SILENCE on the call rather than
an error. A second connection per knowledge question doubles what each call consumes against that
cap, and unlike latency no amount of "one moment" fixes it. Measure that before committing.

### Latency on a call is a UX problem, not a wall

Recorded 2026-08-05 after Stanley challenged it. "A caller cannot wait" was used across one
session to reject a second model call, agentic search, and a hosted cascade. It is wrong as
stated, and it was doing real damage as a reason.

**What a caller actually cannot tolerate is SILENCE, not delay.** A receptionist saying "one
moment, let me look that up" is normal, and the caller waits. So the test for "is this too slow"
is not *does it add time* — it is:

- **Can we say something before it happens?** The realtime model speaks first, then we work.
- **Can the caller interrupt?** Realtime models support barge-in, so a caller who does not want to
  wait can say so. Worth confirming behaviourally rather than assuming.
- **Is the delay bounded?** An unbounded wait is silence eventually, however it began.

This reopens three things that were rejected on the old reasoning: routing voice knowledge through
`brain.handle_query`, iterative search, and the hosted cascade. None of them are settled; the
argument against them was weaker than it looked.

What does NOT change: an unbounded ask/answer loop is still capped, because "we will tell them to
wait" is not a licence for a runaway.

### Knowledge splits into what is always loaded and what is searched

Stanley's proposal, 2026-08-05. Two kinds of thing live in `knowledge/` and they want different
treatment:

- **A small core, always loaded** into the session instruction: who the owner is, the handful of
  facts every second caller asks. No search, available in the first sentence.
- **Everything else, searched on demand**, prefaced by "let me check".

**The core is assembled PER ASKER.** Stanley's correction, same day: an earlier draft said the
core must contain only default-grant content, because it is sent to everyone. That is only true of
a single shared core. Built per asker, it can carry granted material — because it is built from
*that caller's* grants, by `folders_for(asker, verified)`, the same function that already gates
retrieval. The core becomes another consumer of the existing gate rather than a way around it.

Which unifies something that was three separate mechanisms. A caller's world is:

| | per asker, decided by code |
|---|---|
| which folders may be read | `permissions.folders_for` |
| which tools may be invoked | `permissions.tools_for` |
| **what is in the opening instruction** | **the same two, assembled** |

So a verified partner opens with more context than a stranger, and neither is a special case.

Two things this must not lose:

- **Verification still decides.** A grant applies only to a VERIFIED identity, so an unverified
  caller claiming a partner's address gets the stranger's core. `folders_for` already enforces
  this; the core inherits it by construction rather than by remembering to check.
- **The tool list in the instruction INFORMS; dispatch still enforces.** Telling the model which
  tools this caller has stops it promising a booking it cannot make. It does not replace the check
  at dispatch, and every caller still gets a refusal rather than a silently absent capability.

It needs a hard size cap regardless, or it becomes the pile again by accretion.

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

The asker daemon is the product; nothing about an owner surface may end it. `secretary_agent.py`
once raised `SystemExit(1)` when the owner site failed to bind — it now warns and carries on.
Keep it that way: the site being load-bearing for SETUP does not make it load-bearing for
answering.

---

## Part 2 — the owner mcp

### 38 operations, derived not enumerated

Registered from `tools.OWNER_TOOLS`, never listed by hand. Hand-listing drifted to 16 of them —
and worse, imported a module `mcp` 2.x had renamed, so the face exposed nothing at all.

### Two servers, split by lifetime, not audience

Both serve the same host. They are separate for one reason: **a service cannot report being
stopped over its own endpoint.**

| | transport | lifetime | what it does |
|---|---|---|---|
| **service tools** | stdio, spawned by the host | per session | `service_status`, `service_start`, `service_stop`, login-item toggle |
| **secretary tools** | the registry operations | see below | read and drive the instance |

On 2026-08-03 the daemon stopped and nobody noticed for twelve minutes — the log ends cleanly on
`inbound is live`. It was found by accident. Service tools are the fix, and for an owner driving
this from an assistant they are the only way to know it is alive.

**Open: does the secretary tools face need long-lived HTTP at all?** The HTTP argument assumed
the daemon already runs an aiohttp app for the site — which it does again, so the argument is
live rather than moot. Per-session stdio may still be sufficient and simpler. Decide before
building.

If it does become HTTP, one thing is not optional: **stdio is implicitly access-controlled and
HTTP is not.** Only the spawning process can talk to a stdio server; a loopback HTTP endpoint is
reachable by anything running as the same user, including a script or a package `postinstall`.
Unauthenticated, that hands 38 owner operations to any local process.

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
| model key, connector credential | `agentduet-desktop init`, at a terminal | a secret in a chat box goes to the model provider and lands in `owner_chat.json` |
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
assistant with shell access rather than a purpose-built chat with 38 scoped tools. "A human is
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

## The model: local, and the machine picks it (decided 2026-09-23)

**The text model runs on the owner's machine, and the app chooses it.** The owner is not asked.
The hosted providers (Gemini, Claude and the rest) are QUARANTINED in the backend — behind
`llm.CHOICE_QUARANTINED`, code kept. The picker and hosted cards are GONE from the pages
(2026-09-24): the wizard names the machine's pick and starts fetching it as soon as that step is on
screen, with nothing to change — the hub's Assistant tab shows the progress,
and Settings shows the model in use. A developer overrides the pick by Hugging Face name in an
"Advanced (For developer)" dialog opened from the foot of Settings. Lifting the quarantine now
means rebuilding a page, not flipping a flag — `git log` holds the old one. Stanley's call, made
while streamlining for consumer laptops and desktops.

**Why the owner is not asked.** `best_of` already said it: *which weight is an arithmetic question
about their machine, and it has a right answer we can compute.* A list of 21 models asked an owner
to do that arithmetic, and to pick a vendor on top of it. For a consumer install neither question
is theirs.

**The boundary — this is the TEXT model only.** It covers the assistant, summaries, drafting and
calendar suggestions. **Answering a call is still hosted**: voice runs `qwen3.5-omni-flash-realtime`
over DashScope on its own key, it was never chosen in the overlay, and a local cascade was rejected
on latency (see *Voice is weaker than text*). So "local" is a claim about the recorder and the
owner's assistant, not about the secretary on a call. Do not describe the product as local-only
without that sentence.

**Quarantine means quarantine** — the rule the Apple STT flag set: *a quarantine an owner can step
around by typing a setting is not a quarantine.* An install configured for Gemini or Claude falls
back to the local pick on upgrade, rather than keeping its hosted model. A release that ships this
says so in its notes, because it changes what an existing tester is running.

**Which model is in use is decided in ONE place, `llm.current_model()`.** Ten call sites used to
read `SECRETARY_MODEL` each with its own fallback — two of them a Gemini name Google does not
serve, one captured at import — so the secretary and the owner's assistant could disagree about
what was running. Under the quarantine the answer is, in order:

1. **the pick, if it is downloaded;**
2. otherwise **the configured local model, if it is downloaded** — an install already running
   Qwen3 8B keeps running it until the better pick is on disk, rather than being left with
   nothing until someone clicks Download;
3. otherwise **the pick, not yet downloaded**, so every surface offers exactly that download.

A hosted name gets no client even when passed explicitly, so no caller can reach around the
quarantine. The shared assistant resolves on every turn and rebuilds when the answer changes, so a
finished download is picked up without a restart.

### How the pick works — fit AND speed

Capacity decides whether a model fits; memory bandwidth decides how fast it answers. Both are needed.

- **Fit — and Metal's limit is a CEILING, not the budget.** On Apple the GPU may use up to
  `recommendedMaxWorkingSetSize` (12.7 GB on a 16 GB M5). Gemma 4 12B fitted inside that, and
  still pushed the machine from 0 to 1.9 GB of swap under ordinary use, with 7.5 GB free when it
  started: the owner's own apps are the real constraint. `machine.verdict()`'s existing margin
  separated the two correctly — E4B "fits", 12B "tight" — so the pick takes only "fits", and the
  Metal figure is the hard stop above that. On a CPU-only machine the budget is two thirds of RAM.
- **Never size by VRAM on the CPU wheel.** Windows and Linux install llama-cpp-python from the
  `/whl/cpu` index, which cannot offload, so an NVIDIA card there is invisible to the engine. The
  boundary: whether a GPU is USABLE is answered by `machine.can_offload()`, which asks the engine
  itself, never by whether `nvidia-smi` finds a card — and both the budget and the layer count go
  through it.
- **Speed: decode ≈ bandwidth ÷ active-weight bytes.** Measured, not assumed: Qwen3 8B decodes at
  21.7 tok/s from a 4.68 GB file on the M5 — **101.6 GB/s** effective, against 99.9 GB/s from a copy
  benchmark. It held for every dense model tested (94–108 GB/s). This is why MoE models matter:
  Gemma 4 26B-A4B and Qwen3.5 35B-A3B fit big machines and decode at the cost of their ~4B active
  weights.
- **Gemma's E-models break the file-size estimate, in their favour.** E4B decodes at 32.9 tok/s from
  a 4.8 GB file — about 3.1 GB read per token, with the rest of the file off the hot path. llama.cpp
  honours that. So the estimate must use ACTIVE bytes, or it rejects the fastest option as slow.
- **Reading costs as much as writing.** The same model spent 6.9 s prefilling a 1,542-token call
  before its first word — as long as generating the whole 150-token reply. Prefill is compute-bound,
  not bandwidth-bound, so a speed floor that looks only at decode undersells a long transcript.
- **Where the bandwidth figure comes from.** A CPU copy benchmark is a fair proxy on Windows, where
  inference runs on the CPU. It is a weak one on Apple, where inference runs on the GPU through a
  path the CPU cannot see — more threads measured SLOWER there (99.9 → 66.7 GB/s). So it is used to
  choose, and the choice is confirmed by one real timed generation after download, cached; below the
  floor, the pick steps down a tier.
- **No cap.** The tiers continue past 24 GB and 12B, to 64 GB machines and above.
- **The Metal ceiling never binds with this budget**, so nothing needs to query it: two thirds of
  RAM is always below Metal's working-set limit (0.66 against ~0.79 of RAM on the M5).

What the rule picks (`models.pick()`), across machines, with the bandwidths assumed:

| RAM | Apple (120 GB/s; 400 from 48 GB) | CPU-only, 40 GB/s |
|---|---|---|
| 16 GB | E4B | E2B |
| 24 GB | 12B | E2B |
| 32 GB | 12B | E2B |
| 48 GB | 26B MoE | 26B MoE |
| 64 GB+ | 31B | 26B MoE |

**Two things in that table rest on what has not been measured.** The CPU column's jump from E2B
straight to the 26B at 48 GB depends entirely on the 26B's active bytes, which are an estimate (4B
of 26B) because it cannot be run on a 16 GB machine; if it reads more per word than that, those
machines get a model slower than predicted. And the CPU column's E2B at 16–32 GB comes from a
single-threaded probe that likely under-reads an x86 laptop, where llama.cpp uses every core. Both
err toward a smaller or faster model. The confirmation after download is what corrects them, and
it is not built.

**A GPU IS REQUIRED — decided 2026-09-24 (Stanley).** The software targets high-spec machines: a
usable GPU with at least 8 GB. So the CPU-only column above describes an unsupported configuration,
and nothing needs to be measured without a GPU. On macOS the requirement is already met by every
machine we ship to — the build is Apple Silicon only, each has a Metal GPU, and none has under 8 GB
— so there is nothing to enforce there. **Focus is the Mac**; Windows calibration is deferred.

**What that moves:** the bandwidth probe is a CPU copy, and on Apple the GPU does the work. On the
base M5 the two agreed (102 GB/s probe, 101.6 effective decoding), but on Pro, Max and Ultra chips
the GPU reads memory faster than one core can copy it, so the probe under-reads and the pick leans
a rung SMALL — a 64 GB Max would likely get the 26B MoE rather than the 31B. Safe, never swaps, and
it may leave quality unused. Unverified, since only a base-chip Mac has been tested. The correction
is one timed generation on the GPU after the first download, and on a Mac the direction that
matters is UP.

**The speed floor is a product call, not arithmetic.** At 40 GB/s E4B predicts ~13 tok/s, just
under the 15 floor, so a CPU-only laptop gets E2B at ~20. Whether an owner would rather wait longer
for a better model is a judgement about the product; `SPEED_FLOOR_TPS` is where it lives.
**Set at 15 for now (Stanley, 2026-09-24), to be revisited** — a constant, so moving it is one line
and a release. With a GPU required, the CPU case that forced the question no longer ships.

Measured 2026-09-23 on the 16 GB M5 under ordinary use, loaded exactly as the app loads a model
(Metal, all layers, 8k context), on a 1.6k-token call transcript plus "summarise and draft a reply":

| | Qwen3 8B | Qwen3.5 9B | Gemma 4 E2B | **Gemma 4 E4B** | Gemma 4 12B |
|---|---|---|---|---|---|
| file | 4.68 GB | 5.29 GB | 3.12 GB | 4.80 GB | 6.50 GB |
| decode | 21.7 tok/s | 20.4 | 62.9 | **32.9** | 14.5 |
| prefill | 225 tok/s | 207 | 763 | **388** | 140 |
| read the call + write the reply | 13.8 s | 15.2 s | 4.5 s | **8.7 s** | 21.9 s |
| peak memory | 5.87 GB | 5.77 GB | 3.49 GB | **5.55 GB** | 7.47 GB |
| a Vietnamese paragraph | 88 tokens | 78 | 80 | 80 | 80 |
| swap | — | — | — | — | 0 → 1.9 GB |

(E2B was timed a day later, 2026-09-24, on the same machine. It is the fastest here and not the
16 GB pick: the pick takes the largest model that fits and clears the speed floor, and E4B does
both — speed above the floor is not traded for a smaller model.)

**So a 16 GB Mac gets Gemma 4 E4B.** A tier up is not a free upgrade there: the 12B takes two
and a half times as long, swaps, and misses a 15 tok/s floor. Qwen3.5 9B is dense and reads all
5.29 GB per token, which is why it came in slower than the Qwen3 8B it replaces. It does not
think by default in this build — the same 15 tokens with or without `/no_think`.

### Why Gemma 4 — the record for the announcement

Chosen 2026-09-23, by Stanley, after the measurement above. Written for whoever announces it, so
the reasons stay attached to their evidence and the limits travel with them.

**The reasons, in the order they matter to an owner:**

1. **It answers faster.** On a typical 16 GB Mac, reading a call and drafting the reply took
   **8.7 s**, against **13.8 s** for the model it replaces (Qwen3 8B) — about 40% less waiting.
   Reading alone went from 6.9 s to 4.1 s.
2. **It is lighter on the machine.** The smallest memory footprint of the four tested (5.6 GB),
   and it did not push the machine into swap — the owner's other apps keep their memory.
3. **Vietnamese costs almost nothing extra.** It spends 80 tokens on a Vietnamese paragraph against
   75 for the same English. The model it replaces spent 88 against 71, a ~24% penalty on every
   Vietnamese message, paid in time.
4. **It comes from Google, built to run at this size.** Google publishes the files itself, trained
   to run compressed to 4-bit rather than compressed afterwards, so less quality is lost to fitting
   it on a laptop. Not a community repackage.
5. **It is open and free to ship.** Apache 2.0, no sign-up, no licence to accept — so the app can
   download it on the owner's behalf.
6. **One family covers every machine.** Larger Gemma 4 models exist for bigger machines — 12B, a
   26B mixture-of-experts, 31B — so the app can step up without changing vendor.

**Why not Qwen3.5**, which was the other candidate: at the same tier it was the slowest of the
four (15.2 s), because it is dense and reads all of its weights for every word; and its files come
from community packagers. It is still a strong multilingual family and stays the fallback — not
rejected, not chosen.

**Newer Qwen exists, and does not change this for a 16 GB Mac** (checked on Hugging Face
2026-09-24). Qwen3.6 (April) ships 27B and a 35B-A3B mixture-of-experts; Qwen3.8 (August) ships
27B, a 180B "Flash-Next" under a custom licence, and a 2.4T. Nothing at 9B or below, and no GGUF
from Qwen itself — so Qwen3.5 9B is still Qwen's newest model for the machines most owners have.
**Qwen3.6 35B-A3B is the one to measure** against Gemma 4 26B-A4B on 48 GB+ Macs: both are
mixtures of experts with 3–4B working per word, and neither has been timed here.

**What the announcement must NOT claim:**

- **"Better" or "smarter".** Quality has not been measured. Every reason above is speed, size,
  efficiency or provenance. A blind comparison on our own calls is still to come, and could change
  the choice.
- **"Everything runs on your machine."** The TEXT model does — summaries, drafts, the assistant.
  **Answering a call is still a hosted model.**
- **One set of numbers for every machine.** These are one 16 GB M5 Mac under ordinary use. A
  Windows laptop without a graphics card will be slower; a bigger Mac faster; a 16 GB Mac may get a
  different model than a 32 GB one — that is the point of picking automatically.
- **The 26B's speed.** "About as fast as a 4B model" is what its design predicts — only ~4B of it
  works on each word — and it has not been measured.
- **That it has been measured on quality, or on any Mac but one.** It SHIPPED in 0.1.0b3
  (2026-09-24); this line said "designed, not built" until then.

Re-run the numbers with `docs/bench-models.py` before quoting them in anything that will be read
after another model release.

**The download stays one explicit click.** The app decides *which*; the owner still decides
*whether* to pull several gigabytes.

**The developer override** takes llama.cpp's own naming, `owner/repo` or `owner/repo:QUANT`
(e.g. `unsloth/Qwen3.5-9B-GGUF:Q4_K_M`). With no quant it takes Q4_K_M, or the vendor's QAT file.
It builds on `models.add_custom`, which only ever fetches from huggingface.co and builds the URL
itself rather than accepting one. Saving it only registers the model; the Model card then offers
its download, and it becomes the model in use once it is on disk. **A registered model is local
whatever its name contains** — `provider()` checks every model we hold before it looks at vendor
prefixes, or a repo called `…Qwen…` would be sent to DashScope and refused by the quarantine.

### Where the model list comes from

**Ask Hugging Face what is newest, not only what size.** The catalogue was refreshed on
2026-08-27 to Qwen3 and Gemma 3 — six months after Qwen3.5 (Feb 2026) and Gemma 4 (Mar 2026)
shipped. Every size in it was read from the HF API and was right; the families were chosen before
asking, and were a generation behind. The same stale generation was recommended again, from
memory, in the conversation that produced this section. Sorting the search by release date
answers it in one request.

**From the vendor where the vendor publishes it; never by download count.** Google publishes Gemma
4's 4-bit GGUFs itself, quantisation-aware trained, Apache 2.0 and ungated — an app downloading on
the owner's behalf should take those. The most-downloaded Qwen3.5 9B GGUF is a community
*uncensored* merge.

**What would reverse this:** the confirming generation coming in below the floor on most real
installs — local too slow for the median owner machine — or a B3-proxied model (see the
checklist), which would restore a hosted option as one we operate rather than a vendor picker.

## Installers

Per-platform, and the first thing a new owner touches.

| | state |
|---|---|
| macOS arm64 | CI builds a `.app` in a DMG, smoke-tested for providers and voice. **Signed, notarized and stapled** since 2026-08-18 — a normal double-click opens it |
| macOS Intel | **dropped 2026-08-04.** `macos-13` is retired, so the job never started — and a queued job holds its whole run open, making finished builds look unfinished |
| Linux x86_64 | CI builds it |
| Windows | not started |

**Signing is automatic and conditional.** Every step is gated on the certificate secret existing,
so a fork and a credential-less build still produce a working unsigned DMG. Two things worth not
rediscovering: the hardened runtime needs `allow-jit`, because **wasmtime JITs** and without it
the tool sandbox dies the first time a customer tool is called; and macOS `security import`
cannot read a modern AES-256 PKCS#12 — it wants 3DES + SHA-1, while the obvious `-legacy` flag
produces RC2-40, which OpenSSL 3 cannot read back to verify.

### Being a Mac app, not a binary in a folder (decided 2026-09-02)

Two complaints arrived on the day the first Mac was available — "it is really slow" and "the
binary is not usually the same file as the DMG" — and they are one finding. Measured against
Dropbox on that machine:

| | Dropbox | us, a7 |
|---|---|---|
| Dock icon | none (`LSUIElement: 1`) | shown |
| persistent home | menu bar item | nothing |
| `Contents/MacOS/` | a **108K launcher** | a **92M** executable |
| `Contents/Frameworks/` | 344M, laid out | **0B** |
| login at start | a Login Item, visible in System Settings | our own LaunchAgent plist |
| delivery | one `.dmg` | one `.dmg` |

So the DMG was never the problem — Dropbox ships one too, and a `.pkg` would buy nothing for an
app that drags into `/Applications`. The problem is the shape INSIDE the bundle, and the absence
of anywhere for the app to live while it has no window.

**`--onedir` on macOS, `--onefile` everywhere else.** onefile unpacks its whole bundle to a temp
directory on every launch: 3.87s from launch to a bound owner site, against 0.23s for the same
code from source. That is not Python being slow, and **a native shell would not fix it** — the
Swift shell starts the same frozen binary and would pay the same seconds behind a nicer window.
Linux keeps onefile because a single file is what INSTALLER docs promise there and you cannot
`chmod +x` a directory.

**The menu bar is where a phone-answering app lives.** It answers while the owner is away, so "no
window, still running" is its NORMAL state — and today that state is illegible (a Dock icon like
a document app) and fragile (closing the window ends the run; the Swift shell terminates on last
window close). Three things land together or not at all: `NSStatusItem`, `LSUIElement=1`, and
surviving window close. Dropping the Dock icon without a menu bar item leaves a running app
nobody can find.

**The Swift shell is the vehicle, for LIFECYCLE and not for widgets.** This reverses the earlier
"make it justify itself beyond a window": a window is cosmetic, presence is not. It already owns
`NSApplication`, where `LSUIElement`, a status item and "do not quit on last window" are nearly
free; pywebview's loop assumes windows exist, so a windowless-but-alive agent works against the
library. **The UI stays one HTML codebase** — both shells load the same local page into the same
WKWebView, so this is not a second interface, it is ~400 Mac-only lines of shell. pywebview
remains the Windows path and the fallback when the Swift shell is not in a build.

**Login at start becomes `SMAppService`** on macOS, so it appears in System Settings → Login
Items and can be switched off there. This REFINES "Starting at login" above rather than replacing
it: the interface is still `install_login_item()` with no parameters, and the reasoning for that
is unchanged. Only the macOS mechanism moves, from a plist we write to a registration the OS
owns. Worth noting Dropbox's own LaunchAgents are for its updater, not its app.

**We are not going to the Mac App Store.** It requires the sandbox, which this app's loopback
server, home-directory writes and model download would each have to be granted around. Deferred,
not rejected.

**AgentDuet Desktop is the product; installing an assistant is optional.** A boundary, because it is easy to
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
7. **The site and the CLI are both permanent; the mcp is optional.** Revised 2026-08-11 — the
   site was "transitional" while the owner was assumed to have an assistant. They set up in the
   page on macOS and Windows and in the console on Linux, so both are first-class and must cover
   the same ground. Neither may be load-bearing for ANSWERING: the daemon carries on when the
   site fails to bind.
   **Revised 2026-09-23: Linux is no longer a design target.** Hosting moves to a separate team
   and project, so this product is macOS and Windows. Linux keeps building and must not break, but
   it stops shaping decisions — and with it goes the reason the console had to cover everything
   the page does. The existing parity test stays, so nothing already covered regresses; new work
   is designed for the page.
8. **A new install carries calls; answering is opt-in.** The recorder is the product a new owner
   gets, and it needs no model key. Choosing to answer requires one.
9. **The owner reaches their assistant from their own WhatsApp number** (2026-09-07). A message
   from the number in `## Phone` goes to the owner's assistant instead of being filed as a
   person to answer, and the reply comes back on that channel. The two surfaces share ONE
   assistant object, so it is one thread whether it was typed in the window or on a phone —
   both persist to the same file, and a second instance would silently overwrite the owner's
   own history.
   **What this costs, stated plainly:** the owner's assistant holds the owner's tools, and this
   is a second door to it, authenticated by caller id rather than by the loopback token. Meta
   authenticates the sending account, so the claim is real; a hijacked WhatsApp account inherits
   it, so it is weaker than the token. Taken deliberately, on Stanley's explicit call. WhatsApp
   only — a DDUET participant is an account uid, never a number — fails closed when `## Phone`
   is empty, and logged every time it fires.
10. **"Send it" is code on every surface, never a model tool** (2026-09-07 — the principle is
   older, the sharing is new). An assistant that has read a stranger's message and can also send
   is how a stranger's words reach the wire, so the model has no send capability and the send is
   a keyword handled by `assistant.send_if_asked`: a send-only instruction, an existing draft,
   and a recipient that resolves. It lived inside `web.make_app`, so the owner asking from their
   phone got a model turn and was told the assistant can only read — true of the model and false
   of the product. An explicit "send to <name>" must resolve to somebody who has written in,
   because "send to Bob and tell him we close at six" parses as a recipient with a sentence for
   a name, and refusing the whole thing is the safe reading.

## Open

- Does the secretary tools face need HTTP, or is per-session stdio enough?
- Push or pull for escalations?
- Is the hosted cascade actually too slow? Unmeasured — and the reason it was dismissed no longer
  holds (see "Latency on a call is a UX problem"). For scale: `brain.handle_query` itself measured
  **~2.0s** on an escalation and **~0.6s** when it answered (2026-08-05, DashScope). Too slow to
  hide in silence; fine once the agent says "let me check". So the speak-first mechanism has to
  exist BEFORE the second model does, not after.
- **Retrieval on the TEXT path.** Word overlap fails when the caller's words differ from the
  document's, and this is observed rather than theorised: a real query about "walk-ins" escalated
  with `missing_knowledge` while the model's own drafted reply was "We do not accept walk-ins" — it
  knew, and the search did not find it (2026-08-05). On VOICE this is now mitigated by a prompt
  line: the model calls `search_knowledge` itself, so it can be told to try different words before
  escalating. On TEXT it cannot — `brain` runs the search, so a retry means `brain` reformulating
  the query, which needs a model round trip or embeddings. Open, with evidence.
- **Gemma 4 leads the pick — on SPEED, which is measured; QUALITY is not.** E4B beat Qwen3.5 9B
  by ~60% on both reading and writing on the M5, with vendor-published QAT files under Apache 2.0.
  Nothing yet shows it writes better summaries or drafts. The honest test is a blind comparison on
  our own calls — the 29 from the language sweep include a Vietnamese one — and it can still
  overturn this.
- Map our controls to the OWASP API Security Top 10 item by item? The thesis shows every issue we
  found lands on a named category; a formal mapping is what an audit tier would be sold on.

## Next

1. **The Windows install.** The binary builds, runs and shipped in b2; setup through to the hub is
   verified on Windows 11. What remains is the install story — issue #7: where the instance
   directory lives, an installer, and code signing. On file permissions the fear was worse than the
   fact: under the user profile the credential store inherits an owner-only ACL, so it is
   protected in practice, but only by LOCATION — a data folder moved outside the profile inherits
   `Users: read`, silently.
2. **Conference audio for carried calls.** The bridge works and both people can talk, but the
   platform does not hand the app the media yet, so the recorder — the first thing a new install
   is for — records nothing. Not ours to build.
3. **A DEPLOYED OAuth endpoint** — that is the whole remainder, and this item used to say the
   token store was still to build. It is built: `oauth.py` has `_read`/`_write` with an atomic
   write (a torn write is a sign-out, because the refresh token rotates), `refresh()`,
   `token_provider()`, and `secretary_agent` passes that callback to the SDK. It was driven end
   to end against the dev server on 2026-08-31.
   **And a deployed endpoint alone still will not finish onboarding**: sign-in mints a connector
   with no business account bound to it, which receives nothing while reporting healthy — see
   `docs/platform.md`. Provisioning has to bind as well as create.

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
