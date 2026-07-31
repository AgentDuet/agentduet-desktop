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
    # The adapter reads DASHSCOPE_API_KEY, falling back to the raw key in ~/.qwen. The daemon
    # loads $DDUET_HOME/.env into the environment at startup, so a key attached at setup is
    # already visible here. ~/.qwen is the USER's home, not the instance directory.
    if not (os.getenv("DASHSCOPE_API_KEY") or (pathlib.Path.home() / ".qwen").is_file()):
        return False, "no DashScope key (DASHSCOPE_API_KEY, or the raw key in ~/.qwen)"
    return True, ""


# ---- the three tools the CALLER's agent gets --------------------------------------------
# NOT the owner registry: those grant folders and reply as the owner. A caller-facing model gets
# exactly enough to answer, book within bounds, and hand over.

def _tool_declarations() -> list[dict]:
    """Neutral declarations; the adapter maps them to the provider's function-tool shape."""
    return [
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


def _make_tools(caller: str, verified: bool, convo: str, owner_name: str, live: dict):
    """Bind the tools to THIS caller.

    `live` is a one-key holder for the Call, filled in once the call is answered. The handler
    signature is (name, args) with no call context, so transferring — which acts on the live
    call — needs the object reaching it some other way.
    """

    async def _dispatch(name: str, args: dict) -> dict:
        try:
            if name == "search_knowledge":
                q = str(args.get("query") or "")
                text, sources = await asyncio.to_thread(
                    permissions.context_for, caller, verified, q)
                # Same grant that governs text. A caller cannot reach a folder the owner did
                # not share, whatever they ask for.
                return {"found": bool(text.strip()), "sources": sources, "content": text[:4000]}

            if name == "book":
                cap = _only_capability()
                if cap is None:
                    return {"ok": False, "reason": "the owner has not authorised any bookings"}
                at = str(args.get("at") or "")
                minutes = capabilities.block_minutes(cap)
                ok, why = await asyncio.to_thread(
                    capabilities.check_bounds, cap, verified,
                    args.get("quantity"), at, minutes)
                if not ok:
                    return {"ok": False, "reason": why}
                what = str(args.get("what") or "a booking")[:120]
                try:
                    row = await asyncio.to_thread(schedule.book, at, minutes, what, caller)
                except schedule.Conflict:
                    nxt = await asyncio.to_thread(
                        schedule.next_free, at, minutes,
                        str((capabilities.get(cap) or {}).get("bounds", {}).get("hours", "")))
                    return {"ok": False, "reason": "that time is taken",
                            "next_free": nxt or "nothing nearby"}
                await asyncio.to_thread(
                    brain.record, caller, f"[call] {what}", "acted",
                    f"capability:{cap}:voice", f"Booked for {row['at']}",
                    None, "TELCO", None, verified, convo)
                return {"ok": True, "at": row["at"], "what": what}

            if name == "transfer_to_owner":
                call = live.get("call")
                if call is None:
                    return {"ok": False, "say": HOLDING_LINE.format(owner=owner_name)}
                result = await call.connect(ring_time_seconds=TRANSFER_RING_SECONDS)
                if result:
                    await asyncio.to_thread(
                        brain.record, caller, "[call] asked to speak to a person", "acted",
                        "voice:transferred", f"Put through to {owner_name}",
                        None, "TELCO", None, verified, convo)
                    # A 3-way conference: the agent is still on the line. It is told to stop
                    # talking rather than dropped, because closing here would cut off whatever
                    # it is mid-way through saying. Leaving cleanly after the handoff needs an
                    # event to hang the close on — not built.
                    return {"ok": True, "say": "Putting you through now.",
                            "then": "stop talking; the call is now between them"}
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
                return {"ok": False, "reason": code,
                        "say": HOLDING_LINE.format(owner=owner_name)}

            if name == "escalate":
                q = str(args.get("question") or "")[:400]
                await asyncio.to_thread(
                    brain.record, caller, f"[call] {q}", "escalated",
                    "voice:cannot_answer", policy.ABSTAIN, None, "TELCO", None, verified, convo)
                from .notify import escalate_to_owner
                await asyncio.to_thread(escalate_to_owner, caller, q, "on a call")
                return {"ok": True, "say": HOLDING_LINE.format(owner=owner_name)}

            return {"error": f"no such tool: {name}"}
        except Exception as exc:                       # never let a tool kill the call
            logger.exception("voice tool %s failed", name)
            return {"error": str(exc)}

    return _dispatch


def _only_capability() -> str | None:
    """The single declared capability, if there is exactly one.

    Voice has no capability EXTRACTION step — the model asks for a booking directly. With more
    than one declared there is no way to tell which is meant without adding that step, so this
    refuses rather than guessing at authority.
    """
    caps = list(capabilities.all_capabilities() or {})
    return caps[0] if len(caps) == 1 else None


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

        instruction = (
            f"You are the personal assistant for {owner_name}, answering their phone.\n"
            f"Speak briefly — one or two sentences, as a person would on a call.\n"
            f"Answer ONLY from search_knowledge. If it finds nothing relevant, or the caller "
            f"asks you to agree a price, a discount, a meeting or anything binding, call "
            f"escalate and say what it gives you. Never invent a fact about {owner_name}, "
            f"and never agree to anything on their behalf.\n"
            f"If the caller wants to speak to a person, offer to put them through and use "
            f"transfer_to_owner. If it comes back unanswered, take a message instead."
        )
        # sample_rate is the rate of the CALL, in both directions — the adapter uses it to
        # decide whether to resample the caller's audio down to Qwen's 16 kHz input. It must
        # equal what secretary_agent negotiated in CallAudioConfig, or the mismatch breaks
        # BOTH directions at once: the agent's replies play slow and low, and the caller's
        # speech is resampled to nonsense so the ASR emits no transcript. The visible effect
        # of the second one is an agent that says its greeting and then never responds, and a
        # call that records nothing because a turn needs both sides.
        model = QwenVoice(instruction=instruction, tools=_tool_declarations(),
                          voice=VOICE, sample_rate=CALL_SAMPLE_RATE,
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

        dog = asyncio.create_task(_watchdog())
        try:
            await va._bridge(call, ms)
        finally:
            dog.cancel()
            live.pop("call", None)
            await recorder.flush()          # the last question, if it was never answered
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
