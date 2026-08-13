"""Voice — the connector's phone number, answered by a realtime model.

WHAT IS DIFFERENT ABOUT VOICE

In text, `brain.handle_query` runs retrieval, the disclosure/action seam and `check_bounds`
BEFORE anything is said. A speech-to-speech model speaks on its own initiative, so that ordering
is not available. What survives, and what does not:

- **Action stays code-enforced.** Booking is a TOOL the model must call, and the tool runs the
  same `capabilities.check_bounds` + `schedule.book` path as the text side. It cannot book
  outside the owner's declared limits however it is asked.
- **Disclosure becomes prompt-enforced.** Nothing can intercept a sentence before it is spoken.
  The model is given `search_knowledge` and told to answer only from what it returns, and the
  transcript is recorded so an ungrounded claim is at least *visible* afterwards. That is
  detection, not prevention — do not describe voice as having the text guarantees.

ONE CLIENT PER CONNECTOR

`VoiceAgent.serve()` opens its own `SessionManager` and would be a second client on a connector
the message daemon already holds — the documented race, and trigger conditions are persisted
server-side so the two configs would overwrite each other. So this module does NOT call
`serve()`. The daemon owns the one client; we register a call handler on it and reuse
`VoiceAgent`'s per-call audio bridge, with `inbound=None` so it leaves the trigger config alone.

A VoiceAgent is built PER CALL, because its tool and transcript handlers take no call context —
binding them to the caller in a closure is how a tool knows whose booking it is making.
"""

import asyncio
import logging
import os
import pathlib
import uuid
import wave
from datetime import datetime

from . import (brain, capabilities, identity, memory, paths, people, permissions, policy,
               schedule, status)

logger = logging.getLogger("dduet.voice")

#: Said when the agent cannot answer AND the caller is not being put through — either they
#: declined, or nobody picked up. A promise we can keep: the escalation is recorded and the
#: owner is notified.
HOLDING_LINE = ("I'm not able to answer that one myself — I'll pass it to {owner} and "
                "they'll come back to you.")

#: `Call.connect()` starts a 3-way conference by ringing the destination configured on the
#: connector. It takes NO destination parameter — only ringTimeSeconds — so which number rings
#: is a platform setting, not ours. That is why there is no owner-number config here: storing
#: one would imply a control we do not have. If B3 makes the destination passable, this is the
#: place it would go.
TRANSFER_RING_SECONDS = 30

#: Realtime model for calls. Separate from SECRETARY_MODEL: the text model cannot do audio.
#: The adapter's own default is `qwen3.5-omni-flash-realtime` — 3.5 supports tool calling and
#: the older qwen3-omni-* does not, which matters because tool calling is what keeps a booking
#: inside its bounds.
VOICE_MODEL = os.getenv("SECRETARY_VOICE_MODEL", "")

#: Which synthesised voice answers the phone. The Qwen adapter's own default is "Jennifer"
#: (female); named here so it is visible and changeable rather than buried in the adapter.
VOICE = os.getenv("SECRETARY_VOICE", "Jennifer")

#: MUST match the voice model's output rate, and is negotiated for the whole call by
#: secretary_agent. Qwen omni realtime emits 24 kHz (output_audio_format="pcm24"); the SDK's
#: default is 16 kHz, which plays that audio 1.5x too slow and a fifth too low.
#: Overridable because the correct value depends on the voice model AND on what the call leg
#: actually delivers, and getting it wrong is not a subtle failure — it is slow-and-deep audio
#: in one direction and an ASR that transcribes nothing in the other. 8000 / 16000 / 24000 only.
CALL_SAMPLE_RATE = int(os.getenv("SECRETARY_CALL_SAMPLE_RATE", "24000"))

#: How long an answered call may stay silent before we give up on it. The realtime session can
#: die AFTER connecting — the observed case is DashScope refusing on an ACCOUNT-WIDE cap
#: ("connections too much max_connections 100"), which arrives as an error frame the adapter
#: logs but does not surface as an event. We therefore cannot know the model is dead; we can
#: only notice that it never spoke. Answering and then saying nothing is the worst outcome
#: available: the caller believes they got through. Hanging up at least prompts a redial.
#: The greeting normally lands in ~1.5s, so this is generous.
SILENCE_TIMEOUT = float(os.getenv("SECRETARY_CALL_SILENCE_TIMEOUT", "6"))

#: How long to ring the OWNER when passing on a callback request. Longer than a transfer ring:
#: nobody is waiting on the line, so a missed ring costs only a retry.
CALLBACK_RING_SECONDS = 45

#: HOW OFTEN A CALLER MAY MAKE THE OWNER'S PHONE RING.
#:
#: `request_callback` and `transfer_to_owner` are the cheapest real abuse of the asker's five
#: tools, and the only one needing no injection at all: a caller simply asks to be put through,
#: repeatedly. Nothing is stolen. The owner's phone becomes unusable, which for a product whose
#: promise is "it answers so you do not have to" is the whole product failing.
#:
#: TWO limits, and the second is the one that matters. The caller identity is whatever the channel
#: reports, so anyone willing to vary it defeats a per-caller cap on its own. The total is the
#: real ceiling; the per-caller limit only stops one persistent person being the whole budget.
#:
#: In memory, deliberately. A restart resets it, which is a real weakness and the right trade: a
#: store would have to be written on the call path, and the alternative to an imperfect limit here
#: was no limit at all.
RING_WINDOW_SECONDS = 3600
RING_PER_CALLER = int(os.getenv("SECRETARY_RING_PER_CALLER", "2"))
RING_TOTAL = int(os.getenv("SECRETARY_RING_TOTAL", "6"))

_rings: list[tuple[float, str]] = []


def _may_ring(caller: str) -> bool:
    """Record and allow, or refuse. Refusing does NOT drop the caller — see the handlers."""
    import time
    now = time.time()
    _rings[:] = [(t, c) for t, c in _rings if now - t < RING_WINDOW_SECONDS]
    if sum(1 for _, c in _rings if c == caller) >= RING_PER_CALLER:
        logger.warning("caller %s has already made the owner's phone ring %d times this hour",
                       caller, RING_PER_CALLER)
        return False
    if len(_rings) >= RING_TOTAL:
        logger.warning("the owner's phone has rung %d times this hour across all callers — "
                       "refusing more", RING_TOTAL)
        return False
    _rings.append((now, caller))
    return True


def available() -> tuple[bool, str]:
    """(usable, why not) — checked before registering a call handler."""
    try:
        from agentduet import VoiceAgent  # noqa: F401
    except ImportError:
        return False, "this build of the SDK has no VoiceAgent"
    try:
        from agentduet_adapters.qwen import QwenVoice  # noqa: F401
    except ImportError as exc:
        return False, f"the Qwen voice adapter is not installed ({exc})"
    # The instance's key, and only that. The daemon loads $AGENTDUET_HOME/.env into the
    # environment at startup, so a key attached at setup is visible here.
    #
    # We used to accept ~/.qwen as well. That is the USER's home, not the instance, so a
    # throwaway instance holding no credentials at all still reported "voice: available" —
    # which made a first-run test on a developer machine prove nothing about anyone else's.
    if not os.getenv("DASHSCOPE_API_KEY"):
        return False, "no DashScope key for this instance — run `agentduet-desktop init`"
    return True, ""


# ---- the three tools the CALLER's agent gets --------------------------------------------
# NOT the owner registry: those grant folders and reply as the owner. A caller-facing model gets
# exactly enough to answer, book within bounds, and hand over.

#: What a tool says when it cannot say more.
#:
#: A TOOL'S RETURN VALUE IS CALLER-VISIBLE OUTPUT. It enters the context of a model that is
#: speaking to a stranger, and the model narrates freely — `say` is a convention the prompt asks it
#: to respect, and prompts are not a boundary (docs/tool-surface-risk.md). So a return may contain
#: ONLY strings we wrote for a caller to hear. Never an exception, an error code from another
#: system, a path, or the caller's own input reflected back.
UNAVAILABLE = "unavailable"

#: STATUS → THE SENTENCE. A handler picks a status; the framework writes the words.
#:
#: WHY THE HANDLER MAY NOT WRITE PROSE
#:
#: A tool's return enters the context of a model that is speaking to a stranger, and the model
#: narrates freely — `say` is a convention the prompt asks it to respect, and prompts are not a
#: boundary. Removing internals from returns (2026-08-04) fixed the leaks we had; it did not stop
#: the next one, because any field a handler can fill with a string is a field it can fill with the
#: wrong string.
#:
#: So there is no field to leak into. The handler chooses from this table, and the only free text
#: it can influence is data WE produced — a time from our own scheduler, the owner's own name.
#:
#: This is also the contract a customer-authored tool will meet: it returns a status, and never a
#: sentence. Deciding it now, with five tools and one action, is far cheaper than retrofitting it
#: onto a sandbox boundary later (docs/design.md).
#: Two sentinels, because `None` was doing both jobs and got one of them wrong: search_knowledge
#: found content and was still handed the holding line, which would have made the agent say "I
#: cannot answer that" on top of the answer.
COMPOSE = object()   #: no sentence — the model writes one from the data it was given
HOLDING = object()   #: the holding line: cannot answer, and it is being passed to the owner

SAY = {
    "answered":             COMPOSE,
    "booked":               "Booked for {at}.",
    "slot_taken":           "That time is taken.",
    "slot_taken_alt":       "That time is taken. {next_free} is free.",
    "outside_bounds":       "I can't arrange that one — I'll pass it to {owner}.",
    "no_capability":        "I'm not able to arrange that myself — I'll pass it to {owner}.",
    "escalated":            HOLDING,
    "callback_promised":    "I'll have {owner} call you back on this number shortly.",
    "callback_unavailable": HOLDING,
    "transferring":         "Putting you through now.",
    "transfer_failed":      HOLDING,
    "unavailable":          HOLDING,
    # A CUSTOMER TOOL BROKE. Told plainly rather than hidden behind the holding line, because a
    # caller who is told "I'll pass it on" and then hears nothing more assumes they were fobbed
    # off — and the owner's tools WILL break, since the owner wrote them. The reason stays in the
    # log; the caller hears only that it did not work and that the message still travels.
    "tool_failed":          "I'm having trouble with that just now, but I'll pass your message "
                            "to {owner}.",
}


def _render(result: dict, owner_name: str) -> dict:
    """Turn a handler's status into what the model may say.

    The handler's own keys are dropped unless they are named here — so a field added carelessly,
    or filled with an exception, never reaches the model at all.
    """
    status = result.get("status", "unavailable")
    if status not in SAY:
        logger.error("voice tool returned an undeclared status %r", status)
        status = "unavailable"
    template = SAY[status]
    out = {"status": status}
    if template is not COMPOSE:
        text = HOLDING_LINE if template is HOLDING else template
        out["say"] = text.format(owner=owner_name, at=result.get("at", ""),
                                 next_free=result.get("next_free", ""))
    # DATA the model may reason with — ours, never the handler's prose. `content` is the known
    # exception: on a knowledge question the documents ARE the answer, and narrowing that to a
    # sentence is the open tool-contract work.
    for key in ("found", "matches", "content", "at", "then"):
        if key in result:
            out[key] = result[key]
    return out

#: THE ASKER AGENT'S ENTIRE AUTHORITY, and the single place it is written down.
#:
#: WHY A REGISTRY, AND WHY IT IS NOT MCP
#:
#: The declarations and the dispatch used to be two hand-maintained lists — a schema literal and a
#: five-branch `if name == ...`. This codebase has already paid for that shape once: the owner's
#: MCP face listed its tools by hand and drifted to 16 of 33, so an owner could ask for something
#: the secretary plainly did and be told it could not. Here the same drift is worse, because a
#: drifted asker tool is one a CALLER can reach.
#:
#: MCP would also solve the drift, and is the wrong instrument. Its value is letting a host you do
#: not control attach tools across a process boundary, discovered at runtime. The asker harness and
#: these tools are the same program, so there is no boundary and nothing to discover — and the
#: realtime model takes a plain schema list anyway, so an MCP server would be translated straight
#: back into this. The cost is the part that matters: MCP turns "which tools exist" from a
#: compile-time fact into a runtime lookup, and this list being slow and visible to change is the
#: asker side's main protection. See docs/tool-surface-risk.md.
#:
#: So: one definition, compiled in, with both the schema and the dispatch derived from it.
ASKER_TOOLS: list[dict] = [
        {"name": "search_knowledge",
         "description": "Look up what the owner has made available. Answer ONLY from what this "
                        "returns. If it returns nothing relevant, do not guess — escalate.",
         "input_schema": {"type": "object", "required": ["query"], "properties": {
             "query": {"type": "string", "description": "what the caller asked, in their words"}}}},
        {"name": "book",
         "description": "Arrange a time for something the owner has authorised. Refused "
                        "automatically if it falls outside their declared limits.",
         "input_schema": {"type": "object", "required": ["what", "at"], "properties": {
             "what": {"type": "string", "description": "what is being arranged"},
             "at": {"type": "string", "description": "ISO 8601 start time, e.g. 2026-08-01T19:00"},
             "quantity": {"type": "integer", "description": "how many, if it is countable"}}}},
        {"name": "request_callback",
         "description": "Arrange for the owner to call this caller back. Use when you cannot "
                        "answer and they want a person. Only offer this if it is available — "
                        "if it returns unavailable, take a message instead.",
         "input_schema": {"type": "object", "required": ["about"], "properties": {
             "about": {"type": "string",
                       "description": "what they want, in one line, for the owner to read"}}}},
        {"name": "transfer_to_owner",
         "description": "Put the caller through to the owner when you cannot answer and they "
                        "want to speak to a person. Ask the caller first. If nobody picks up "
                        "this returns unanswered and you should fall back to taking a message.",
         "input_schema": {"type": "object", "properties": {}}},
        {"name": "escalate",
         "description": "Use when you cannot answer from the owner's documents, or when the "
                        "caller asks for something you may not decide — a price, a discount, a "
                        "commitment. Records it for the owner and tells you what to say.",
         "input_schema": {"type": "object", "required": ["question"], "properties": {
             "question": {"type": "string", "description": "what the caller wants, in one line"}}}},
]

#: The authority itself, as a set. `_dispatch` checks against THIS, so a handler that exists but
#: was never declared can never be called — drift can only ever remove capability, not add it.
ASKER_TOOL_NAMES = frozenset(t["name"] for t in ASKER_TOOLS)


def _tool_declarations() -> list[dict]:
    """Neutral declarations; the adapter maps them to the provider's function-tool shape.

    Copies, so a provider adapter that mutates what it is handed cannot edit the registry.
    """
    return [dict(t) for t in ASKER_TOOLS]


def _make_tools(caller: str, verified: bool, convo: str, owner_name: str, live: dict):
    """Bind the tools to THIS caller.

    `live` is a one-key holder for the Call, filled in once the call is answered. The handler
    signature is (name, args) with no call context, so transferring — which acts on the live
    call — needs the object reaching it some other way.
    """

    # Handlers register under their OWN function name, so the name in the registry and the name
    # that dispatches cannot drift apart by editing one and forgetting the other.
    handlers: dict = {}

    def tool(fn):
        handlers[fn.__name__] = fn
        return fn

    @tool
    async def search_knowledge(args: dict) -> dict:
        q = str(args.get("query") or "")
        text, sources = await asyncio.to_thread(
            permissions.context_for, caller, verified, q)
        # Same grant that governs text. A caller cannot reach a folder the owner did
        # not share, whatever they ask for.
        #
        # The FILENAMES are logged, not returned. They are the owner's private layout, and a model
        # handed them will cite them — "according to owner.md" — to a stranger. A count is all the
        # model needs to know whether it has anything to work from.
        logger.info("search_knowledge %r → %s", q[:80], sources)
        return {"status": "answered", "found": bool(text.strip()),
                "matches": len(sources), "content": text[:4000]}

    @tool
    @tool
    async def book(args: dict) -> dict:
        cap = _only_capability()
        if cap is None:
            return {"status": "no_capability"}
        at = str(args.get("at") or "")
        minutes = capabilities.block_minutes(cap)
        ok, why = await asyncio.to_thread(
            capabilities.check_bounds, cap, verified,
            args.get("quantity"), at, minutes)
        if not ok:
            logger.info("book refused by bounds: %s", why)
            return {"status": "outside_bounds"}
        what = str(args.get("what") or "a booking")[:120]
        try:
            row = await asyncio.to_thread(schedule.book, at, minutes, what, caller)
        except schedule.Conflict:
            nxt = await asyncio.to_thread(
                schedule.next_free, at, minutes,
                str((capabilities.get(cap) or {}).get("bounds", {}).get("hours", "")))
            return ({"status": "slot_taken_alt", "next_free": nxt} if nxt
                    else {"status": "slot_taken"})
        await asyncio.to_thread(
            brain.record, caller, f"[call] {what}", "acted",
            f"capability:{cap}:voice", f"Booked for {row['at']}",
            None, "TELCO", None, verified, convo)
        return {"status": "booked", "at": row["at"]}

    # A promise the code can keep. `transfer_to_owner` bridges the live caller into a conference
    # with a destination we cannot see or set (SDK #36), and a real attempt timed out with the
    # owner's phone never ringing. `session.make_call(dest)` DOES take a destination — proven — so
    # ringing the owner ourselves is buildable, and it works whether or not they are at their desk.
    @tool
    async def request_callback(args: dict) -> dict:
        from . import owner as owner_settings
        number = owner_settings.phone()
        if not number:
            # No number: do NOT offer a callback. Saying it and not doing it is worse than
            # taking a message. The reason is neutral rather than "no owner number configured" —
            # that described the owner's setup to whoever happened to ring.
            logger.info("callback requested but no owner number is configured")
            return {"status": "callback_unavailable"}
        about = str(args.get("about") or "wants to speak to you")
        # REFUSING TO RING IS NOT REFUSING THE CALLER. The escalation below is recorded either
        # way, so a rate-limited caller still reaches the owner — just not by making their phone
        # ring again. Dropping them would turn an abuse control into a way to silence people.
        if not _may_ring(caller):
            ringing = False
        else:
            ringing = True
            live["callback"] = about
        await asyncio.to_thread(
            brain.record, caller, f"[call] callback requested: {about}", "escalated",
            "voice:callback_requested", policy.ABSTAIN,
            None, "TELCO", None, verified, convo)
        from .notify import escalate_to_owner
        await asyncio.to_thread(escalate_to_owner, caller, about, "callback requested")
        # Placed AFTER this call ends — see the callback runner. Two live calls at once would
        # need the model to speak on both legs.
        return {"status": "callback_promised" if ringing else "escalated"}

    @tool
    async def transfer_to_owner(args: dict) -> dict:
        call = live.get("call")
        if call is None:
            return {"status": "transfer_failed"}
        # Same budget as a callback: both end in the owner's phone ringing.
        if not _may_ring(caller):
            return {"status": "transfer_failed"}
        result = await call.connect(ring_time_seconds=TRANSFER_RING_SECONDS)
        if result:
            await asyncio.to_thread(
                brain.record, caller, "[call] asked to speak to a person", "acted",
                "voice:transferred", f"Put through to {owner_name}",
                None, "TELCO", None, verified, convo)
            # A 3-way conference: the agent is still on the line. It is told to stop talking
            # rather than dropped, because closing here would cut off whatever it is mid-way
            # through saying. Leaving cleanly after the handoff needs an event to hang the
            # close on — not built.
            return {"status": "transferring",
                    "then": "stop talking; the call is now between them"}
        # The code is logged, not returned: it is another system's diagnostic string and the model
        # would be free to read it out to the caller.
        code = getattr(result, "error_code", "") or "unknown"
        logger.info("transfer failed (%s) — falling back to a message", code)
        await asyncio.to_thread(
            brain.record, caller, "[call] wanted to speak to a person", "escalated",
            f"voice:transfer_failed:{code}", policy.ABSTAIN,
            None, "TELCO", None, verified, convo)
        from .notify import escalate_to_owner
        await asyncio.to_thread(
            escalate_to_owner, caller, "wanted to speak to you — transfer unanswered",
            "on a call")
        return {"status": "transfer_failed"}

    @tool
    async def escalate(args: dict) -> dict:
        q = str(args.get("question") or "")[:400]
        await asyncio.to_thread(
            brain.record, caller, f"[call] {q}", "escalated",
            "voice:cannot_answer", policy.ABSTAIN, None, "TELCO", None, verified, convo)
        from .notify import escalate_to_owner
        await asyncio.to_thread(escalate_to_owner, caller, q, "on a call")
        return {"status": "escalated"}

    # DRIFT IS A LOGGED ERROR, not a silent gap. A declared tool with no handler means the model
    # was offered something that cannot run; a handler with no declaration is dead code that the
    # authority check below will refuse anyway.
    undeclared = set(handlers) - ASKER_TOOL_NAMES
    unimplemented = ASKER_TOOL_NAMES - set(handlers)
    if undeclared or unimplemented:
        logger.error("asker tool registry drift — undeclared=%s unimplemented=%s",
                     sorted(undeclared), sorted(unimplemented))

    async def _dispatch(name: str, args: dict) -> dict:
        # AGAINST THE REGISTRY, not against `handlers`. The declared list is the authority, so a
        # handler that exists but was never declared can never be reached — drift can only remove
        # capability, never grant it.
        if name not in ASKER_TOOL_NAMES:
            # NOT the name they sent. Echoing an unknown tool name puts the caller's own string
            # into the context of a model that is speaking to them.
            return _render({"status": "unavailable"}, owner_name)
        # THE SECOND CHECK, and a different question. The registry above is the fence — what the
        # product offers at all, fixed at build time. This is the owner's grant for THIS caller.
        # A stranger gets search and escalate; booking and ringing the owner are granted, not
        # assumed. Checked here rather than by omitting the declaration, so every caller sees the
        # same tool list and the refusal is a decision rather than a hidden capability.
        if name not in permissions.tools_for(caller, verified):
            logger.info("caller %s is not granted %s", caller, name)
            return _render({"status": "unavailable"}, owner_name)
        fn = handlers.get(name)
        if fn is None:
            return _render({"status": "unavailable"}, owner_name)
        try:
            # EVERY return goes through _render. A handler cannot reach the model directly, so
            # "the handler must remember not to leak" stops being a rule anyone has to remember.
            return _render(await fn(args), owner_name)
        except Exception as exc:                       # never let a tool kill the call
            # THE DETAIL GOES TO THE LOG, NOT THE MODEL. `str(exc)` was returned here, and a
            # tool return is read by a model that then talks to a stranger — so an exception
            # carrying a filesystem path, a key fragment or an internal hostname was one
            # paraphrase away from being spoken aloud. The owner needs the detail; the caller
            # needs to know only that it did not work.
            logger.exception("voice tool %s failed", name)
            return _render({"status": "unavailable"}, owner_name)

    return _dispatch


def _only_capability() -> str | None:
    """The single declared capability, if there is exactly one.

    Voice has no capability EXTRACTION step — the model asks for a booking directly. With more
    than one declared there is no way to tell which is meant without adding that step, so this
    refuses rather than guessing at authority.
    """
    caps = list(capabilities.all_capabilities() or {})
    return caps[0] if len(caps) == 1 else None


# ---- recording an answered call ------------------------------------------------------------

#: Answered calls land in their OWN folder, and that is what keeps them out of the
#: transcription queue: `transcribe.pending()` globs `*.wav` non-recursively in `recordings/`,
#: so a subdirectory is invisible to it. Deliberate — an answered call ALREADY has a transcript,
#: written turn by turn from the model's own events, and re-transcribing the audio would file a
#: second, differently-worded copy of the same conversation against the same caller.
ANSWERED = "answered"


class _Recorder:
    """Wraps a ModelSession to write both sides of an answered call to disk.

    THE TAP POINTS ARE THE SDK'S OWN BRIDGE, not the call. `_bridge` pumps
    `call.caller.audio_stream()` into `ms.push_audio()` and `ms.events()`'s AudioOut back into
    `call.send_audio()` — so decorating the session sees every frame in both directions without
    opening a second consumer on the call's audio stream. Same technique as the bank demo's
    RecordingSession, and it is why this needs no SDK change.

    The two files are NOT aligned. The caller's side is continuous for the whole call; the
    agent's side only exists while it is speaking. Mixing them would need the caller-timeline
    offset of each agent chunk (the bank demo keeps exactly that), and a naive sum compresses
    the agent's speech toward the start. Two files, honestly unmixed, is the right MVP — the
    transcript already carries who said what in order.

    NEVER let recording break a call. Every write is guarded: a full disk, a read-only home or a
    closed file costs the recording, and the person on the phone must not notice.
    """

    def __init__(self, inner, call_id: str):
        from . import carry
        self._inner = inner
        self._dir = carry.RECORDINGS / ANSWERED
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        self._caller = self._open(f"{stamp}-{call_id}-caller.wav")
        self._agent = self._open(f"{stamp}-{call_id}-agent.wav")
        self._frames = {"caller": 0, "agent": 0}
        self._call_id = call_id
        self._closed = False

    def _open(self, name: str):
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            w = wave.open(str(self._dir / name), "wb")
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(CALL_SAMPLE_RATE)
            return w
        except Exception as exc:
            logger.error("could not open %s for recording (%s: %s)", name, type(exc).__name__, exc)
            return None

    def _write(self, which: str, writer, pcm: bytes) -> None:
        if writer is None or not pcm:
            return
        try:
            writer.writeframes(pcm)
            self._frames[which] += len(pcm)
        except Exception:
            pass          # a failed write must never reach the caller

    async def push_audio(self, pcm):
        self._write("caller", self._caller, pcm)
        await self._inner.push_audio(pcm)

    async def events(self):
        from agentduet import AudioOut
        async for ev in self._inner.events():
            if isinstance(ev, AudioOut):
                self._write("agent", self._agent, ev.pcm)
            yield ev

    async def send_tool_result(self, call_id, result):
        await self._inner.send_tool_result(call_id, result)

    async def close(self):
        # IDEMPOTENT. close() arrives TWICE on a normal call: the SDK closes the session from
        # its own on_hangup handler, and the call path closes it again in a finally. The writes
        # are guarded so nothing was corrupted, but every recording was reported to the log
        # twice — which reads as two recordings of one call, and would have had someone hunting
        # for a duplicate that does not exist.
        if self._closed:
            return
        self._closed = True
        for which, w in (("caller", self._caller), ("agent", self._agent)):
            if w is None:
                continue
            try:
                w.close()
            except Exception:
                pass
            secs = self._frames[which] / (CALL_SAMPLE_RATE * 2)
            # Report the EMPTY case distinctly, same as the carry path: a header with no frames
            # looks like a recording in a directory listing and is the failure most likely to go
            # unnoticed.
            if secs:
                logger.info("call %s: recorded %.1fs of the %s side", self._call_id, secs, which)
            else:
                logger.warning("call %s: the %s side recorded NO audio", self._call_id, which)
        await self._inner.close()

    def __getattr__(self, name):
        # Anything the SDK asks of a session that is not intercepted above. Without this a new
        # ModelSession method would break every answered call the day the SDK adds one.
        return getattr(self._inner, name)


# ---- transcripts into the same record the text side uses ---------------------------------

def _make_recorder(caller: str, verified: bool, convo: str):
    """Write turns into memory and the log, so a call reads like any conversation.

    ORDER MATTERS, AND IT USED TO BE WRONG. The first version held one slot per role and wrote
    a row as soon as both were filled. But the agent GREETS FIRST (greet_on_connect), so the
    greeting was sitting in the agent slot when the caller's first words arrived, and the two
    were paired. Every row after that was shifted by one: each answer was filed under the
    NEXT question, which reads as the agent replying before it was asked.

    So an answer is only ever paired with a question that came BEFORE it. Agent speech with no
    question pending is what it looks like — something said unprompted — and is recorded on its
    own rather than borrowed by the next question.
    """
    pending: list[str] = []          # caller utterances not yet answered
    spoke = asyncio.Event()          # set on the first transcript of either side

    async def _write(question: str, answer: str) -> None:
        await asyncio.to_thread(
            memory.append, memory.key(caller, verified, convo), question, answer, "voice")
        await asyncio.to_thread(
            brain.record, caller, question, "answered", "voice", answer, None, "TELCO", None,
            verified, convo)

    async def _on_transcript(ev) -> None:
        text = (getattr(ev, "text", "") or "").strip()
        if not text:
            return
        spoke.set()          # proof the model is alive; see SILENCE_TIMEOUT
        if getattr(ev, "role", "") == "user":
            # Several utterances before a reply (a pause mid-sentence, or an interruption)
            # belong to one question — joined, so none is dropped.
            pending.append(text)
            return
        question = " ".join(pending).strip()
        pending.clear()
        await _write(question, text)

    async def _flush() -> None:
        """A question the caller asked with the call ending before any answer. Without this it
        is silently lost — and it is the most interesting line in the transcript, because it is
        the one the agent never got to."""
        if pending:
            question = " ".join(pending).strip()
            pending.clear()
            await _write(question, "")

    _on_transcript.flush = _flush
    _on_transcript.spoke = spoke
    return _on_transcript


# ---- wiring it to the daemon's ONE client ------------------------------------------------

def register(sm, owner_name: str) -> bool:
    """Register a call handler on the daemon's existing SessionManager. True if voice is on."""
    ok, why = available()
    if not ok:
        logger.info("voice not enabled: %s", why)
        return False

    from agentduet import VoiceAgent
    from agentduet_adapters.qwen import QwenVoice

    async def _on_call(noti) -> None:
        caller = getattr(noti.participant, "value", str(noti.participant))
        network = getattr(noti, "network", None)
        verified = people.default_verified(str(getattr(network, "name", network) or "TELCO"))
        convo = f"call-{noti.call_id}"
        who = identity.resolve(caller, verified)[0]
        logger.info("incoming call from %s (verified=%s)", who, verified)

        # The instruction lives in prompts/asker-voice.md, not here. On voice it IS the
        # disclosure control — nothing inspects a sentence before it is spoken — so it is a
        # reviewable file, and render() refuses blanks and placeholder-shaped values. That is
        # the class of bug that answered a real call as "[Owner's Name]'s assistant".
        from . import owner as owner_settings, prompts
        instruction = prompts.render("asker-voice", owner_name=owner_name,
                                     pronoun=owner_settings.pronoun_raw())

        # api_key passed EXPLICITLY: the adapter falls back to ~/.qwen on its own, so without
        # it our availability check could report "no key" while a call still connected using a
        # file in the user's home. One source of truth, or the check is theatre.
        model = QwenVoice(instruction=instruction, tools=_tool_declarations(),
                          voice=VOICE, sample_rate=CALL_SAMPLE_RATE,
                          api_key=os.environ["DASHSCOPE_API_KEY"],
                          **({"model": VOICE_MODEL} if VOICE_MODEL else {}))

        # Per call, so the handlers can close over who is calling.
        live: dict = {}
        va_recorder = _make_recorder(who, verified, convo)
        va = VoiceAgent(
            _config_from_env(),
            tools=_make_tools(who, verified, convo, owner_name, live),
            on_transcript=va_recorder,
            inbound=None,          # leave the connector's trigger config to the daemon
        )
        # The SDK's own _handle_call would do the next four lines, but it keeps the Call to
        # itself — and transferring acts ON the live call. So open it here and hand the same
        # object to both the bridge and the tools. `_bridge` is private for the same reason
        # `_handle_call` is: the SDK only exposes this path through serve(), which would open
        # a second client on the connector. Worth asking for a public entry point.
        status.set_number(noti.subscriber)
        session = await sm.open_session(uuid.uuid4().hex, noti.subscriber)
        call = await session.process_call(noti)
        live["call"] = call
        ms = await model.open()
        # WRAP THE SESSION, not the call. The SDK's bridge already pumps the caller's audio into
        # ms.push_audio() and the model's AudioOut back to the call, so decorating ms sees both
        # directions without opening a second consumer on call.caller.audio_stream(). Wrapped
        # BEFORE answer(): the greeting is the agent's first words and belongs in the recording.
        from . import owner as _owner
        if _owner.record_calls():
            ms = _Recorder(ms, str(call.id))
        if not await call.answer():
            logger.error("answer failed for call %s", call.id)
            await ms.close()
            return
        recorder = va_recorder

        async def _watchdog() -> None:
            """Hang up a call the model never speaks on. See SILENCE_TIMEOUT."""
            try:
                await asyncio.wait_for(recorder.spoke.wait(), SILENCE_TIMEOUT)
                return                     # it spoke; nothing to do
            except asyncio.TimeoutError:
                pass
            logger.error(
                "call %s: the voice model never spoke within %.0fs — hanging up rather than "
                "leaving the caller in silence. Check the log for a provider error frame; the "
                "known cause is the DashScope ACCOUNT-WIDE concurrent-session cap.",
                call.id, SILENCE_TIMEOUT)
            try:
                from .notify import escalate_to_owner
                escalate_to_owner(
                    who, "(a call was answered but the agent could not speak)",
                    "voice model silent — call dropped")
            except Exception:
                pass                       # notifying must never mask the hangup
            try:
                await call.disconnect()
            except Exception as exc:
                logger.error("could not hang up the silent call %s: %s", call.id, exc)

        async def _ring_owner(number: str, about: str) -> None:
            """Ring the owner and read out who called and why.

            Placed AFTER the caller's call ends, not during: the model can only speak on one
            leg. This is what makes the callback promise true — an escalation in the queue and a
            desktop notification both reach an owner who is AT the machine, which is exactly the
            owner who did not need a phone call.
            """
            from agentduet import Address
            brief = (
                f"You are {owner_name}'s assistant, phoning {owner_name} with one message.\n"
                f"Say this and nothing else: {caller} called and asked for a callback about "
                f"{about}.\n"
                f"Then stop talking. Do not ask questions. Do not offer anything.")
            try:
                session2 = await sm.open_session(uuid.uuid4().hex, noti.subscriber)
                out = await session2.make_call(Address.telco(number))
                if not await out.dial(ring_time_seconds=CALLBACK_RING_SECONDS):
                    logger.warning("callback to the owner was not answered — it stays in the "
                                   "escalation queue")
                    return
                teller = QwenVoice(instruction=brief, tools=[], voice=VOICE,
                                   sample_rate=CALL_SAMPLE_RATE,
                                   api_key=os.environ["DASHSCOPE_API_KEY"],
                                   **({"model": VOICE_MODEL} if VOICE_MODEL else {}))
                ms2 = await teller.open()
                try:
                    await va._bridge(out, ms2)
                finally:
                    try:
                        await ms2.close()
                    except Exception:
                        pass
            except Exception as exc:
                # Never let the callback take anything else down: the escalation is already
                # recorded, so the worst case is the owner reads it on the dashboard instead.
                logger.error("callback to the owner failed: %s: %s", type(exc).__name__, exc)

        dog = asyncio.create_task(_watchdog())
        try:
            await va._bridge(call, ms)
        finally:
            dog.cancel()
            live.pop("call", None)
            await recorder.flush()          # the last question, if it was never answered
            about = live.pop("callback", "")
            if about:
                from . import owner as owner_settings
                number = owner_settings.phone()
                if number:
                    logger.info("caller %s asked for a callback — ringing %s", caller, number)
                    await _ring_owner(number, about)
            # The SDK closes ms from its on_hangup handler, which covers the normal ending.
            # This covers the ones it does not: a bridge that raises, or a call that ends
            # without a HANGUP. A realtime session left open counts against a DASHSCOPE
            # ACCOUNT-WIDE cap — we hit "connections too much max_connections 100" during
            # testing, and the symptom is not an error the caller hears, it is silence.
            try:
                await ms.close()
            except Exception:
                pass

    @sm.on_incoming_call
    async def _handler(noti) -> None:
        # Its own task: blocking the SDK event bus for the length of a call stops other
        # sessions being set up.
        asyncio.create_task(_on_call(noti))

    logger.info("voice enabled — calls answered by %s", VOICE_MODEL or "the adapter default")
    return True


def _config_from_env():
    from agentduet import SessionManagerConfig
    return SessionManagerConfig(
        api_key=os.getenv("AGENTDUET_API_KEY"),
        connector_uuid=os.getenv("AGENTDUET_CONNECTOR_UUID"),
    )
