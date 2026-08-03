# Two facings, two agents — a design proposal

Status: **proposed**, 2026-08-03. Not built. Decisions below are recorded so they are not
re-litigated; open questions are marked as open rather than guessed.

Companion document: a threat model, not yet written. This doc says what the system should be.
That one says who is attacking it and what they want. Keeping them apart matters — otherwise the
analysis gets quietly edited to match the design.

---

## The problem this solves

DDuet Desktop runs one process containing two very different jobs:

- **Serving the owner.** Attended. They ask it to grant a folder, correct a fact, write a reply.
  Broad authority is the point — 33 registry operations in `tools.OWNER_TOOLS`.
- **Serving external parties.** Unattended, 24 hours, strangers on the other end. Narrow
  authority is the point — five operations, and everything else refused.

Today the separation between them is **convention checked by a test**: invariant 9 says
`canvas.py` must not import `tools`. That is a real control and it has held. But it is a rule
about imports, enforced by review, in a codebase where both agents share a process, a prompt
style, and an author who is in a hurry.

Two defects this week came from prompt strings being ordinary code: a voice call answered as
`[Owner's Name]` because a value was missing, and before that a `SYSTEM_PROMPT` with no pronoun
slot at all. On the text channel a bad prompt is embarrassing. On voice the prompt IS the
disclosure control, so a bad prompt is a security failure — and it reached a real caller.

## The shape

```
   external party                    owner
        │                              │
   ┌────▼─────────────┐        ┌───────▼────────────────────┐
   │  ASKER AGENT     │        │  OWNER AGENT               │
   │  unattended      │        │  attended, general-purpose │
   │  5 operations    │        │  (Claude Code / Goose /    │
   │  no OS reach     │        │   the built-in site chat)  │
   └────┬─────────────┘        └───────┬────────────────────┘
        │                              │
        │                        ┌─────▼──────┐
        │                        │ dduet MCP  │   the narrow waist
        │                        └─────┬──────┘
        └──────────┬───────────────────┘
                   │
        ┌──────────▼───────────────────────────────┐
        │  the enforcing core                      │
        │  capabilities.check_bounds · policy      │
        │  permissions · folder_index · schedule   │
        └──────────────────────────────────────────┘
```

Both agents reach the same enforcing core. Neither agent *is* the enforcement — that principle
does not change, and it is the one the whole product rests on: **the model reads, code decides.**

## The two allow-lists

| | asker agent | owner agent |
|---|---|---|
| attended | no | yes |
| model | realtime (voice) / text | whatever the owner already uses |
| operations | 5, fixed | 33, and OS reach via their own agent |
| may read | granted folders only, via `search_knowledge` | the instance |
| may write | nothing outside a booking | `knowledge/`, settings, permissions |
| fenced by | an allow-list it cannot extend | the owner's own approval flow |

The asker's five: `search_knowledge`, `book`, `request_callback`, `transfer_to_owner`,
`escalate`. Adding a sixth should require editing the allow-list, being reviewed as a change to
the fence, and nothing else in the codebase should be able to widen it at runtime.

## Agents are declared, not assembled

Copied in shape from Claude Code subagents (`name` / `description` / `model` / `tools`) rather
than Goose recipes, for two reasons: it is four fields instead of a schema, and it matches what
already exists here — a registry with faces over it. A "recipe" is then just a **named filter
over `tools.OWNER_TOOLS`**, not a second configuration system that spawns processes.

```yaml
# agents/asker.yaml
name: asker
model: ${SECRETARY_VOICE_MODEL}
tools: [search_knowledge, book, request_callback, transfer_to_owner, escalate]
prompt: prompts/asker-voice.md
```

Rules the loader enforces:

- **Only what is listed is available.** Goose's own docs put it well: *"When a recipe specifies
  an explicit `extensions` block, only the listed extensions are available."* An allow-list, not
  a layer over defaults.
- **A prompt is a template with declared parameters**, and a missing parameter is a **load
  error**, not a live call. This is the specific discipline that would have caught both defects
  above: `[Owner's Name]` was an unfilled value reaching a stranger, and the absent pronoun slot
  was a parameter nobody declared.
- **Templates are versioned files**, diffable and reviewable, because on voice they are the
  control.

## Voice: what a prompt can and cannot fence

Speech-to-speech means nothing inspects a sentence before it is spoken. A template does not
change that, and it is important not to claim otherwise.

| what | mechanism | when |
|---|---|---|
| standing rules — who you are, never invent, use these tools | prompt template → the session's `instructions` | once, at session open |
| per-answer grounding — say *this* | the **return value of a tool** | every turn |

`instructions` is set once. So the fence is two pieces and only one of them is a prompt.

**The proposed change: `search_knowledge` returns the sentence to say, not documents to
paraphrase.** Today the model receives text and composes; that composition is the ungoverned
step. If the tool returns an utterance produced by the same code path the text side uses, the
model's remaining job is delivery and conversation flow. It can still deviate — this is
mitigation, not prevention — but the surface shrinks from "anything it knows" to "did it say
what it was handed", which is also trivial to check afterwards.

The pause while the tool runs is what a person does anyway: *"let me check that for you."*

Options considered and not chosen, kept here so they are not rediscovered:

- **Hosted cascade** (STT → `brain.handle_query` → TTS) is the only option that restores every
  invariant, because the brain runs before anything is said. Rejected 2026-07-31 — but the
  recorded reason was that a **local** cascade is too slow (CPU) or needs the T4 box. A hosted
  cascade was never measured. **It should be given a number before it stays rejected.**
- **Interrupting on a policy hit** using caller transcripts is racy, and cutting the agent off
  mid-sentence sounds broken.
- **Post-hoc grounding check** on the transcript is detection, not prevention. It is
  complementary and stays on the roadmap; under the tool-returns-the-sentence design it becomes
  nearly free.

## The crossing point — the risk this design must not hide

The two-zone picture invites a comfortable assumption: untrusted input reaches only the fenced
agent. It does not.

**Asker-authored text flows into the owner agent's context.** Escalation questions, briefings,
people notes, call transcripts — all written by strangers, all rendered into the prompt of the
agent with the broad authority. So the attack is not "convince the fenced agent"; it is:

> a caller says something shaped like an instruction → it is recorded verbatim → the owner later
> asks "what's waiting?" → the privileged agent reads it.

This gets **worse**, not better, if the owner agent becomes a general-purpose assistant with
shell access. "A human is present" is thinner than it sounds, because the human is reading a
summary produced by the model that just read the injection.

Therefore, a property of the MCP server and not of either agent:

- **Everything asker-authored is returned tagged as untrusted content**, structurally delimited,
  never interpolated as prose the host agent could mistake for its own instruction.
- **The owner-facing prompt states the rule**: an instruction appearing inside quoted asker
  content is content to report, never to follow.

This is the strongest argument for the narrow waist. Today both agents share a Python process and
the boundary is an import rule. With an MCP server in between, the boundary is a place where
sanitising can actually be enforced.

## Decisions

1. **Two agents, each with an explicit allow-list declared as data.** The asker list is five
   operations and may only be widened by editing it.
2. **Agent definitions follow the Claude Code subagent shape**, implemented in this codebase as a
   filter over `tools.OWNER_TOOLS`. No second runtime, no separate process lifecycle, so it works
   inside the shipped binary.
3. **Prompts become versioned templates with declared parameters**, and a missing parameter fails
   at load.
4. **Voice grounding moves into the tool contract**: `search_knowledge` returns an utterance.
   Prompts carry standing rules only.
5. **One asker MCP server, not two.** Splitting prompts from tools is conceptually tidier and
   buys no security, since both are equally in-process.
6. **Goose is not adopted.** Its recipe model is a good reference, but it cannot sit in a
   speech-to-speech loop — so it would fence the text path, which `brain.handle_query` already
   fences in code, and leave the voice path, which is the unfenced one. It is also a second
   runtime beside a binary whose whole pitch is double-click-and-go.
7. **The asker agent is not moved to its own OS process yet.** The attacker in scope influences
   model output; they cannot execute code. Against that attacker a correctly-enforced allow-list
   is a real fence, and process isolation defends against a threat that is not in the model.
   Revisit if that changes.

## Open — decide before building

- **Who is the owner?** If it is a developer, "bring your own agent + our MCP" is the right
  primary surface and much of the desktop packaging serves the wrong person. If it is a lawyer or
  a shop, the site stays primary and MCP is the power-user path. The 2026-07-30 decision assumed
  the second. **This is a product decision and it changes what gets built.**
- **Does the site chat survive?** If the owner agent is external, the built-in owner chat becomes
  a third face over the same registry. Keeping it is defensible for the non-technical owner;
  it is not free.
- **Is the hosted cascade actually too slow?** Unmeasured. Decision 4 is a mitigation; the
  cascade is a fix.
- **Where does the MCP server run** — in the daemon process, or beside it? In-process is
  simplest and matches decision 7. Beside it is the upgrade path if the threat model changes.

## What changes in code

Roughly in order, each independently useful:

1. `prompts/` — voice and text templates extracted from f-strings, parameters declared. Smallest
   change, directly prevents the two defects already shipped.
2. Tag asker-authored content as untrusted wherever it enters an owner-facing prompt. Cheap, and
   it addresses the crossing point.
3. `agents/*.yaml` + a loader that filters `tools.OWNER_TOOLS` and refuses anything unlisted.
   Invariant 9 becomes data instead of an import rule.
4. `search_knowledge` returns an utterance for the voice agent.
5. Re-register the MCP face from the registry rather than by hand — it has drifted to 16 of 33
   operations, which is the same enumeration bug this whole design is meant to stop repeating.
