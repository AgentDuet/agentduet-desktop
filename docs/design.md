# DDuet Desktop — the design

Single source of direction, 2026-08-03. Replaces `agents.md` and `service.md`, which were
written across a week of pivots and had begun to contradict each other and the code.

---

## The product is two parts

**The asker daemon.** Always on. Holds the DDUET connection, answers calls and messages from
external parties, decides nothing it has not been authorised to decide. This is the product.

**The owner mcp.** How the owner reads and drives it, from an AI assistant they already use —
Claude Code, Goose, anything speaking MCP.

**There is no owner interface**, and that is the decision that shapes everything else.

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

### The reversal, recorded honestly

This is the **second** reversal on this point.

- MCP was originally primary.
- **2026-07-30** it was reversed: the site became primary, because *"MCP needs the owner to
  already have an AI app and configure it; the site needs nothing."* A dead site was made fatal.
- **2026-08-03** it is reversed back, on a different premise: that the owner **is** someone with
  an assistant, and that maintaining three UI surfaces for one product is not affordable.

The 2026-07-30 reasoning was not wrong; the assumption about who the owner is changed. If that
assumption changes again, this decision is the one to revisit — not the plumbing under it.

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

## Configuration: a path, not an interface

**Secrets cannot go through the assistant.** `save_connector` is deliberately outside
`OWNER_TOOLS`: typing a credential into a chat box sends it to the model provider and writes it
to `run/owner_chat.json` in plaintext. That reasoning holds and is not overturned by removing
the UI.

So with no interface, no chat and (today) no CLI path, **the product cannot be set up.** The
answer is a configuration path at a terminal:

    dduet-desktop init        # model key, connector, name — secrets never touch the assistant

Everything after first run goes through the mcp.

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
| macOS Intel | the `macos-13` job has never started; that label is on GitHub's retirement track. Decide whether Intel is supported |
| Linux x86_64 | CI builds it |
| Windows | not started |

Notarization needs an Apple Developer ID. Acceptable for a colleague, not past that.

---

## Decisions

1. **Two parts: the asker daemon and the owner mcp. No owner interface.**
2. **The asker fence is a code-level allow-list in the daemon** — five operations. Not MCP;
   there is no external host on that side. A separate OS process is the documented upgrade path
   if the threat model ever includes local code execution, which today it does not.
3. **Prompts are versioned templates with declared parameters and value checks.**
4. **Voice grounding moves into the tool contract**; prompts carry standing rules only.
5. **The owner mcp is derived from `tools.OWNER_TOOLS`**, never enumerated.
6. **Service tools are separate from secretary tools**, because one must work when the daemon
   does not.
7. **The login-item tool takes no parameters.**
8. **Secrets are configured at a terminal, never through the assistant.**
9. **The site is transitional, not primary.** It stays until `init` covers first-run
   configuration and the mcp path is proven — Cen is testing with it now and has no assistant.
   It stops being load-bearing immediately: the daemon must no longer exit when it fails.

## Open

- Does the secretary tools face need HTTP, or is per-session stdio enough?
- Push or pull for escalations?
- Is Intel Mac supported?
- Is the hosted cascade actually too slow? Unmeasured.

## Next

1. **Stop the daemon dying with the site.** One line, and it is the only thing in this document
   that is currently harmful.
2. **Service tools** — `service_status` / `service_start` / `service_stop`. Includes fixing
   `signal.SIGKILL`, which does not exist on Windows.
3. **`init` takes the connector**, so the product can be set up without the site.
4. **Tag asker-authored content as untrusted** in whatever the mcp returns.
5. **Login-start units**, then the Windows installer.
