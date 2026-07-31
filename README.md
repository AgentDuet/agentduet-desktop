# secretary-sample — desktop personal-secretary agent (POC)

An agent that runs on **your own machine**, answers queries from **verified external
parties** over the AgentDuet **DDUET** channel, **escalates** what it shouldn't answer,
and gives you a **daily digest**.

The bet: every desktop-assistant product answers questions; none of them know **who is
actually asking**. Verified inbound identity + the owner's context staying local is the
part that is B3's to own.

## What this POC is trying to prove

Not answer quality — the loop:

1. A verified external party reaches an agent running on a laptop.
2. It answers only what it should.
3. It escalates the rest **to a human**, correctly.
4. The owner gets a useful report.

**The metric that matters is escalation precision/recall**, not eloquence. Over-escalate
and it's a notification app; under-escalate and it answers wrongly *as you*.

## Design constraints (learned from the SDK spec, not assumed)

- **DDUET is passive.** The SDK can only reply to a `dduet_session_uid` seen on an inbound
  message — it can never mint one or start a chat. So the agent cannot message the owner,
  and cannot follow up later. Escalation therefore goes out as a **desktop notification**
  (`notify.py`), which is natural since we're already on the owner's machine.
- **Availability.** A desktop agent is up only while the laptop is. Queries arrive anyway.
  Not solved here — a real product needs a server-side queue and an explicit
  "owner offline" reply. Flagged as the main structural gap.
- **"Local" is about custody, not inference.** Owner context (`knowledge.md`) never
  leaves the machine; the query itself still goes to the model provider. Real local
  inference needs a GPU — CPU-only Ollama can't hold a conversation.
- **Authority.** The agent speaks *as the owner*. It never commits, prices, or schedules
  (see `policy.py`), and the POC's safe default is a holding reply plus escalation.

## Files

| File | What it does |
|---|---|
| `secretary_agent.py` | The daemon — DDUET inbound → gates → answer or escalate → log |
| `permissions.py` | **ACCESS** — folder allowlist, per verified identity |
| `policy.py` | **AUTHORITY** — hard rules first, model abstention second |
| `permissions.json` | Who may be answered from which folders |
| `knowledge/` | The readable world: `public/` for everyone, scoped folders per grant |
| `tools.py` | **The owner tool surface** — one implementation, two faces |
| `secretary_mcp.py` | Face 1: MCP, for your existing LLM app |
| `web.py` + `web.html` | Face 2: local site (dashboard + prompt window + live push) |
| `test_isolation.py` | Asserts an external party can never reach an owner tool |
| `notify.py` | Escalation → desktop notification |
| `digest.py` | Daily report of who asked what |

## Who may read and who may write

| | knowledge | actions |
|---|---|---|
| **external-facing agent** | READ only — answers from the documents, can never change them | none: it may only act inside a declared capability's bounds |
| **owner's assistant** | READ (to know the context) + WRITE (to improve it) | full: grant folders, reply, resolve, declare capabilities |

The external parties are not necessarily strangers — Pauline and Celine are known contacts.
"External" is about which side of the boundary they sit on, not how well the owner knows them.

## Two owner surfaces, one core

Both are thin wrappers over `tools.py`. Implementing a command twice is how faces
drift, so they don't.

| | MCP (`secretary_mcp.py`) | Local site (`web.py`) |
|---|---|---|
| Available | only while your LLM host is open | **whenever the daemon runs** |
| Good at | ad-hoc reasoning over state | showing state; **pushing** alerts |
| Model | your host's (Claude) | `SECRETARY_MODEL` — one model, both surfaces |
| Holds secrets | no | no — replies go via the daemon's outbox |

**Security invariants** (`test_isolation.py` enforces the first two):

1. The external-facing path never imports `tools`/`web`/`secretary_mcp`. The owner
   surface can grant folder access and send as you — a prompt-injected inbound message
   must not be able to reach it.
2. The owner assistant is a **different agent**: own system prompt, full access, owner
   tool registry. Same model family is fine; a shared code path is not.
3. The site binds **127.0.0.1 only** with a per-run token (`.run/web-token`). On
   `0.0.0.0` it would be a privilege-escalation target on any shared network.

### One model, both surfaces

`SECRETARY_MODEL` serves the external-facing agent and the owner's assistant alike.

There used to be an `OWNER_MODEL` split, on the theory that the owner side does harder work
(tool selection, multi-step triage) and that a paid model there keeps owner chats off the
free-tier quota real queries depend on. Removed 2026-07-29: the second reason is billing, not
architecture, and the first did not survive testing — the cheap model handled the owner tool
protocol 6/6 (single-step reads, a two-argument write, multi-step search). Meanwhile the split
cost real time, with the daemon silently running a different model from the one we believed.

## The security model — disclosure vs action

The seam is **disclosure vs action**, not "two filters over the same content":

| | Question | Decided by | Grantable? |
|---|---|---|---|
| **DISCLOSURE** | may this fact be said? | **the folder grant, entirely** | ✅ that *is* the grant |
| **ACTION** | may it bind the owner? | `policy.COMMITMENT_RULES` | ❌ never |

**A grant is the whole disclosure decision.** If you granted a folder, its content is
answerable — no keyword rule second-guesses it. An earlier version filtered content
keywords (money, legal) *on top of* the grant, which was incoherent: you grant a price
list precisely so the price can be given, then a keyword refuses it.

**Actions are ungrantable**, because they aren't about documents at all:

```
"What is your list rate?"   -> disclosure. Answered if a granted folder says so.
"Can you do it for $5k?"    -> action. Never, however readable the price list is.
```

Your calendar could be fully readable and the agent still must not accept a meeting.

**Consequence: the folder boundary IS the security boundary.** Since nothing
second-guesses a grant, granting a repo root is a real decision — prefer a curated
subfolder. (`test_isolation.py` covers the identity side, not your folder choice.)

**Per-person `## Always escalate`** sits on top as an explicit owner override for one
person — deliberately not a content filter. Keyword-based, so it catches "roadmap" but
not "what are you building next year?"; treat it as a preference, not a boundary.

## Identity — the channel issues it, and it carries the verified flag

**Decided 2026-07-29 (DDUET backend side).** Verification is a property **of the identity**,
not of a message:

- The **channel** issues every identity: the real one for a logged-in person, a transient one
  for a walk-up visitor. It marks each verified or unverified.
- A **message** carries an identity reference. There is no per-message verification flag.

**Why this beats labelling messages:** the sender never chooses their identity. An unverified
visitor typing *"I am Pauline"* is emitting **text**, not a claim on her identity — so it
cannot be filed against her, and there is no earlier verification for it to inherit.

An earlier draft of this design put the flag on the message, arguing that a durable flag would
let a later unverified claim inherit trust. That only holds if the SENDER supplies the
identity — a weakness of this POC's simulator, not a property of the channel. Superseded.

**What it fixed here.** The owner's view showed `13 open` against Pauline: one ask from her,
twelve from senders who merely typed her number. Verification had specifically *not*
established any link between those twelve and her, and beyond being wrong it let an unverified
sender inflate a real contact's badge — cheap misdirection of the owner's attention. Now an
unverified sender gets their own identity and their own row:

```
Pauline                                    2 open
unverified visitor (claims +6591234567)    <- own row, own history, public folders only
```

`identity.py` holds the model. Until the channel issues transient ids, an unverified sender is
namespaced (`visitor:<claimed>`) so it can never collide with the verified identity of the
same name; when DDUET starts issuing ids, `claimed` becomes that id and the prefix falls away.
A visitor identity never resolves to the *profile* of whoever it claimed to be.

**Two decisions deliberately left OPEN** (tracked in `../my-agenda.md`; do not resolve them in
code until the backend needs them):

1. **Transient identity lifetime** — per session, or stable across visits? Per-session means a
   visitor has no history and can hold no grants; stable makes it a soft identity that can be
   stolen.
2. **Upgrade path** — a visitor logs in mid-conversation and is now Pauline. Link/merge, or
   switch? Without a merge signal the earlier turns are orphaned under the transient identity;
   with a naive merge, a session that began anonymous absorbs verified history and grants.

Still needed from the backend either way: which field carries the identity's verified state,
and whether it is a boolean or a **strength level**. SSO, SMS OTP and a carrier-verified number
are not equivalent; `verified_only` is binary today, which is fine for taking a pizza order and
probably not for releasing a contract draft.

### ACCESS — folder-scoped, per identity
- Everyone gets `default.folders` (`knowledge/public`). Individuals can be granted more.
- **Per-asker scoping only works because inbound identity is verified** — that's the
  part of this design that is B3's to own.
- **Enforced in code, not the prompt.** The model only ever sees text `context_for()`
  handed it; roots are realpath-resolved, so symlinks and `..` cannot escape an allowed
  folder (tested).
- No permitted folders → escalate everything, without calling the model.
- Answers record their **sources**, so the digest shows what grounded each reply.

### ACTION — escalation model
Deliberately **not** "ask the model if it can cope" — LLMs are confidently wrong exactly
when a handoff matters. Two passes:

1. **Commitment rules** (`policy.COMMITMENT_RULES`) — commitment, negotiation,
   scheduling, legal-binding. These never reach the model's judgement and no grant
   overrides them. Stems, not exact words: `\bagree\b` misses "agreeing" (the same bug
   once let "pricing" through as `\bprice\b`).
2. **Grounded answer or abstain** — the model answers only from permitted sources, and
   emits the literal token `ESCALATE` when they don't cover the question.

Note there is no longer a content-keyword pass. Disclosure follows the grant.

## Profiles — knowing *who* is asking

`people/<identity>.md`, one per person. This is what makes it a secretary rather than an
FAQ bot: a real secretary knows who someone is and adjusts.

```markdown
# +6591234567
## Who
Aimee Hoang — PM, bss-oss.
## Comms
Lead with the decision up front, detail after. Don't open with questions.
## Folders
- knowledge/partners
## Always escalate
- roadmap
## Observed          <- accumulated; only written when the owner accepts
```

**Verified identities only.** A profile grants tone, scope *and* access, so applying one
to a self-declared identity is an impersonation vector — worse than having no profile.
`people.TRUSTED_NETWORKS` gates it; anonymous web chat gets the public default and
nothing else. Enforced in `test_isolation.py`: the *same* identity claimed over DDUET
gets no profile, no profile folders and no person rules.

**Curated vs Observed never mix.** Everything above `## Observed` is yours and is never
written by code or model. Suggestions come through `profile_suggestions()`; you accept
them with `accept_observation()`. That is what stops a profile drifting into fiction.

**Never quoted back.** The profile changes *behaviour* only. The prompt forbids
revealing, summarising or confirming it — "my notes say you're a non-native speaker" is
exactly the failure to avoid.

**Per-person policy.** `## Always escalate` stacks *on top of* the global hard rules,
never instead of them. `## Folders` is where a person's grants live, so one file holds
everything about them.

### Permissions grow with use
Every escalation is a prompt to grant a little more. From your own chat window:

- `pending_escalations()` — see what someone needed and couldn't get
- `grant_folder(asker, folder)` — give that identity the folder that would have answered it
- `add_knowledge(fact)` — or just teach it a public fact

Next time, that question self-answers. The digest surfaces recurring escalation reasons
precisely so this loop has something to act on.

## Running it

Needs the SDK's **`feature/dduet-channel`** branch (DDUET isn't on `main` yet):

```bash
cd ../wss-sdk-python && git checkout feature/dduet-channel
cd ../secretary-sample
uv venv && uv pip install -e ../wss-sdk-python python-dotenv google-genai aiohttp mcp

cp .env.example .env      # keys + the two model names
./start.sh                # prints the owner-site URL (with its token)
python test_isolation.py  # security invariants
```

The owner site is at `http://127.0.0.1:8899/?t=<token>` — `start.sh` prints it, and the
token is in `.run/web-token`. To add the MCP face:

```bash
claude mcp add secretary -- "$PWD/.venv/bin/python" "$PWD/secretary_mcp.py"
```

## Testing it now — the channel simulator

DDUET inbound isn't live yet, so `/sim` stands in for it. You play the **external
party**: pick a channel, claim an identity, send a message.

```bash
SECRETARY_SIM=1 ./start.sh      # then open the /sim link it prints
```

**It runs the real path.** `/sim` calls `brain.handle_query()` — the same function the
DDUET handler calls — so what you see is what a real inbound message would get. A
simulator with its own copy of the logic proves nothing once the two drift.

The point is to switch channel with the **same identity** and watch the difference:

| `+6591234567` asks "What is on the roadmap?" | outcome | why |
|---|---|---|
| on **DDUET** (anonymous) | escalated `not_grounded` | no profile, `knowledge/public` only |
| on **TELCO** (verified) | escalated `person_rule` | profile applied → its `Always escalate` fires |

Same person, same question, different answer — because verification is what the profile
and the folder grants hang off.

> **OFF by default.** The simulator can forge a *verified* identity, which bypasses the
> entire identity model. It needs `SECRETARY_SIM=1` **and** the owner token, binds
> localhost only, and logs a warning when enabled. `test_isolation.py` fails if it is
> on in your environment.

## Vocabulary — get these terms right

They were used interchangeably early on and it caused confusion. "Capability" is **not** the
agent, and **not** the slot machinery.

| term | what it is | layer | example |
|---|---|---|---|
| agent / persona | who it acts for, how it speaks | config | "the secretary", `owner.md` |
| action primitive | a thing the framework can physically do | **code** | `book_slot` |
| store | machinery + state behind a primitive | code + state | `schedule.py`, `.run/schedule.json` |
| **capability** | domain + primitive + bounds, declared by the owner | **config** | `pizza_delivery` |
| bounds | the limits; `CHECKED` in code vs advisory to the model | config | `hours`, `max_quantity` |
| commitment | what a capability produces | state | a booking |
| knowledge | what it may *say* | config | `knowledge/public/learned.md` |

The persona is config, so the same machine works as a business front desk. Verification is a
**per-capability bound** (`verified_only`) rather than a global rule precisely because a
restaurant serves external parties by default — a global verified-only would block the business.

## Next up — resume here (2026-07-28, later)

**The four capability mechanisms are built and verified working** (`capabilities.py`,
`schedule.py`, `_try_capability` in `brain.py`, six owner tools). Declared `pizza_delivery`
over the tool layer and confirmed: an order books, a clash offers the next slot, over-quantity
and out-of-hours are refused with the actual limit, a timeless order asks for the time, a
discount still escalates as `policy:negotiation`. New outcome `acted`, only for a real
booking. Bookings are visible via the `bookings` tool.

**Model: `qwen3.6-flash` on DashScope (attached 2026-07-29).** Key read from `~/.qwen`
(same bare-file convention as `~/.gemini`); `attach_model` verified it live and saved it to
`$DDUET_HOME/.env`. Booking and escalation both confirmed correct on it. Adding this third
provider touched `llm.py` only — the seam holds. Gates behave identically across providers;
reply *wording* differs (Qwen's refusals are more scattershot than Gemini's).

**Gemini spend cap — still exhausted**, waiting on techops to lift it. Not blocking: Every model call returns `429 RESOURCE_EXHAUSTED`
("project has exceeded its monthly spending cap"). It surfaces as **HTTP 500** through
`/api/sim`, and as `RemoteDisconnected` sometimes, so it does not look like a quota problem —
check the cap at ai.studio/spend first. `test_behaviour.py` cannot run until this clears
(raise the cap, or the month rolls over). Verified good news: with no model the system
**fails closed** — a pizza order escalated and booked nothing.

Open, in the order I would do them:

**DONE 2026-07-29 — owner-side drafting is grounded.** The owner chat used to draft with
`tools: []` and invent figures (a "10%" counter to a 20% request), never reading her messages
or the briefing that already held a draft written *with* her thread. Fixed by handing the
evidence over instead of hoping it asks: `tools.owner_context(asker)` assembles the person's
open threads, each briefing (wants / facts / decision / SUGGESTED DRAFT) and the recent
exchange, and `OwnerChat` injects it on the SYSTEM side per turn — not into history, which
would accumulate stale copies of a queue that changes.

It now returns the briefing's real draft verbatim ("I can't commit to a 20% discount right
now…"), quotes 20% rather than inventing a number, and answers "what exactly has she asked
me for?" from the actual threads. `tools: []` is now the CORRECT outcome — it no longer needs
a lookup, so this is both cheaper and grounded. Third time the deficit was evidence rather
than reasoning (after `_which_close` and the retrieval loop).

Reuses `open_escalations()` and `conversation_with()` rather than adding a second view of
"what is open" — the drift that separated the two owner faces before.

1. **A model-free test suite for bounds and conflicts.** `schedule` and
   `capabilities.check_bounds` are pure comparisons, but today they are only exercised through
   `test_behaviour.py`, which needs quota. This is the suite that should have caught the
   bounds bugs, and it would run during an outage like this one.
**DROPPED as YAGNI 2026-07-29 — a `knowledge` field on capabilities.** The idea was that a
capability would declare the knowledge it depends on, so `declare_capability` could warn
"declared, but I have nothing documented about the menu". Unnecessary: the sample's menu is
just a file in `knowledge/public/`, and once written every menu question answered. A capability
grants AUTHORITY; what the agent may SAY is already the folder grant's job, and coupling them
would blur the disclosure/action seam this design rests on.

What the pizza menu actually taught, worth more than the field: **retrieval matches on the
asker's vocabulary, and the search loop is not a reliable bridge.** "What time do you close?"
failed against a doc that said only "Hours: 11:00-21:00" — no word overlap — and "gluten free"
missed "Gluten-free". Write sample knowledge in the words people use, including their
hyphenation.

2. **Prove genericity: add a SECOND capability through MCP alone** (e.g. accepting a callback
   request). If it needs no code, this is a framework; if it needs code, we have only
   structured pizza nicely. This is the actual acceptance test and it is still unproven.
3. **Local (Ollama) adapter — low priority, deliberately deferred.** The adapter itself is
   ~40 lines now that `llm.py` exists, so building it early saves nothing. What it buys is
   not speed of development but two properties: an **unblockable path** (a spend cap cost
   us hours on 2026-07-29 — a local model has no quota to exhaust, which also makes it a
   free CI target), and the **privacy claim** for a desktop agent that reads the owner's
   folders — "your documents never leave this machine" is a sales answer no hosted provider
   can give. Do it when a customer asks about on-device, or when we want a zero-cost
   regression target. Not before the demo: on a CPU-only box it is too slow to use.
   Two traps already handled generically, so they are not repeat work: `<think>` tag
   stripping (in `_strip_thinking`, recurs on every open-weight model) and the naming
   collision — "qwen" maps to DashScope, so a local qwen3:8b needs `SECRETARY_PROVIDER=ollama`
   rather than a model-name prefix. The one genuinely new piece is `num_ctx`: Ollama
   defaults to 4096 and **truncates silently**, and our prompts carry retrieved documents,
   so grounding would degrade invisibly.

4. **The escalation list IS framework (decided 2026-07-29) — finish the split.** Not deferred
   and not YAGNI: "answer on behalf of the owner, escalate what you cannot answer" is the
   premise of the product, so every agent built on this needs the queue. Most of the mechanics
   already sit in framework files (`tools.open_escalations`, `policy`, `asker_actions`): items
   derived from an append-only log, threaded on a key, a lifecycle (open → resolved /
   withdrawn / aged-out), a TTL, weighted ordering, and two parties with different rights.

   What remains is to move the sample-specific parts out to config: the reason vocabulary
   (`policy:negotiation`, `missing_knowledge`), `REASON_WEIGHT`, and the briefing fields
   (wants / facts / decision / draft).

   **Invariant any refactor must keep:** the two parties see DIFFERENT projections of the same
   log. `open_asks` hides reasons, briefings and ids from the asker; `open_escalations` shows
   the owner everything. A single shared view would leak what we do and don't document.

5. **"Canvas" — an owner-authored HTML space for visualisation (Stanley, 2026-07-29).** Distinct
   from the escalation list; the two were conflated in an earlier note. The owner (or the LLM on
   their behalf) creates a rendered view — "show today's deliveries as a timeline", "a board of
   what needs me".

   **Scope decision needed, because it changes the size enormously: who sees it?**

   - **Owner-facing** — cheap and safe. The site already serves HTML locally and the LLM can
     write it. Recommended for v1.
   - **Asker-facing** (a menu + checkout UI for the pizza case) — collides with two existing
     decisions. The owner site binds **127.0.0.1 only** by design, so a customer cannot reach
     it; an asker-facing canvas needs a public host, which is DDuet-backend territory rather
     than the desktop. And a checkout handles **money**, far outside "may hold a 30-minute
     slot" — `capabilities` bounds would have to cover payment authority, which is exactly the
     kind of thing the disclosure/action seam says must be explicit and narrow.

   So: build owner-facing, and treat asker-facing UI as a hosted-surface feature for the
   backend roadmap, not a desktop one.

5. Per-message model cost is a real constraint — the cap was hit testing one person. Each
   inbound can spend several calls (retrieval up to 3, contradiction, briefing, capability
   extraction). Worth measuring before this runs on a desktop all day.

Done today, for context: model-decided closing reframed from "does this reply satisfy the
request" to "which requests is it about" (replying now clears the entry — the list is what
someone waits on the owner for, not the owner's to-do list); replies lead with context
derived from the asker's own wording; the one-sided `(owner replied)` turn fixed in the
readers (keyed on `reason`, so stored rows needed no migration); site-chat continuity.

## Status / blockers

- ✅ SDK side verified — the branch's DDUET tests pass (15), and the echo bot
  **connects to prod** and stays up.
- ✅ **Inbound DDUET works** (2026-07-28). Two things were needed: the **dev** endpoint
  (`AGENTDUET_BASE_URL=ws://wss-dev.internal.b3networks.com:8080`, not prod) and
  **`inbound_message=True` in the trigger conditions** — without the latter the socket
  connects and no message ever arrives. Round trip confirmed via `stg.dduet.com` chat.
- ⚠️ **DDUET is passive** — we can only write into a `dduet_session_uid` seen on an inbound
  message, so the agent cannot start a conversation. Owner replies to someone with no live
  session are therefore **held** (`asker_actions.queue_reply`) and flushed on their next
  inbound, or handed to an open page by `/api/deliver`.
- 🔎 Open question for the AgentDuet team: `stg.dduet.com/<slug>/chat` requires a login
  (Google SSO) but offers **no directory** — it goes straight to the owner's own secretary.
  Raised in `#AI-Product`; see `../my-agenda.md`.
