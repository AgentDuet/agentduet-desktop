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

**Their code must not run in our process.** Sandboxed with no filesystem, network or environment,
or a webhook on their side. Not in the daemon.

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
becomes prompt, goes to the provider, and is written to `owner_chat.json`. A browser form does
not: the value goes browser → daemon → `.env`, and no model is involved.

So the decision is not "no UI ever". It is **no full owner UI in three engines**. A
single-purpose secrets form is a different thing, and the site already implements it correctly
(`/api/setup/model`, `/api/setup/connector` are direct POSTs, no model in the path).

**Planned: a transient secrets-only page.** `init` opens the browser to one form, takes the two
credentials, done. No window, no pywebview, no third engine — and it beats a terminal for the
actual task, which is pasting two 40-character keys where a no-echo prompt hides typos.

Not relied on: `mcp` 2.x has an `elicitation` module through which a server can request input
from the user via the host. The model does not generate the value, but whether it lands in the
model's context is the host's implementation detail. Too uncertain for a credential.

`setup_status` is what joins them: it reports what is configured, echoes **no values** — not the
key, not the connector uuid, not the owner's own number, because anything it returns travels to
a model provider — and names the one command the assistant cannot run itself.

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

**DDuet is the product. The assistant is the owner's, and installing one is optional.** Stated as
a boundary because it is easy to drift across: we detect, we offer, we configure what the owner
chooses — we do not ship an assistant as part of what DDuet is. If a future change makes Goose
required, or bundles it, that has crossed this line and needs deciding again.

Within that: **we do offer to install one**, having said "not doing" while shipping the opposite.
The objection stands — it picks a winner and makes us a distributor of someone else's CVEs — but
an assistant is the only *comfortable* way to drive the daemon day to day, so "bring your own" is
a dead end for someone who has never installed one. Not the only way: the CLI exists, and an owner
with no assistant still has a working product. Detection wins by default; Goose is the
alternative an owner picks. Their prebuilt release, never from git. Nothing bundled. **Not Goose
Desktop on Linux** — deb/rpm only, both need root, and nothing else here does.

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
- Sandbox or webhook for customer-authored tools?
- Map our controls to the OWASP API Security Top 10 item by item? The thesis shows every issue we
  found lands on a named category; a formal mapping is what an audit tier would be sold on.

## Next

1. **Tag asker-authored content as untrusted** in whatever the mcp returns.
2. **Status-and-render for tool returns** — handlers return a status, the framework writes the
   sentence. Prerequisite for customer-authored tools, and cheapest to do while one action exists.
3. **Per-caller tool grants** — `"tools"` in `permissions.json`, checked at dispatch.
4. **Login-start units**, then the Windows installer.

Done items are not listed here. `git log` has them, and a "Next" list that keeps its own history
stops being read.

---

**Editing this document.** It records live decisions and the reasoning that would reverse them.
Not history, not completed work, and not a summary of its own sections — every one of those grew
back at least once and had to be cut again on 2026-08-04.

**Keeping it true.** Sections are not dated, deliberately: a date says when something was written,
not whether it is still so, and it would not have caught a single one of the staleness bugs found
on 2026-08-04 — this document claimed we did not install an assistant while the product shipped
one, and CLAUDE.md listed a fixed bug as the worst open item. Someone still has to notice. Two
things work better, and both are used here:

- **Date reversals, not sections.** A date earns its place exactly where two statements
  contradict — "reversed 2026-08-03", "withdrawn 2026-08-04" — because then the order is the
  information.
- **Anchor a decision to code that asserts it.** "The fence is five tools" cannot drift silently,
  because `test_asker_tool_surface` names those five: add a sixth and the suite fails, so both
  have to be edited. Prefer this wherever a decision is mechanically checkable.
- **State the boundary, not just the behaviour.** "DDuet is the product, the assistant is
  optional" is what lets a later reader see that bundling Goose has crossed a line. A description
  of current behaviour cannot be contradicted; a boundary can.
