"""Carry an inbound call onward, and record both legs.

THE TOPOLOGY, because it is not what the name "call forwarding" suggests:

    Telco ──▶ CPaaS Leg 1 ──▶ AgentDuet WSS ◀──▶ this process
                                    │
                                    ▼
                              CPaaS Leg 2 ──▶ PBX

Leg 1 TERMINATES here. Leg 2 is ORIGINATED from here, by `call.connect()`, toward a destination
configured on the connector. Two legs stitched together — a back-to-back user agent. We are not
attached to somebody else's call and we are not eavesdropping on one; we are the junction, which
is why the audio is ours by construction and why `caller` and `callee` are simply the two legs.

WHAT THIS IS NOT. There is no agent here. Nobody is answered, no knowledge is read, nothing is
decided, and no tool can be called — so none of the invariants in CLAUDE.md are in play on this
path. That is the whole reason it is cheap.

WHAT IT COSTS INSTEAD. The secretary only ever holds what the owner told it to say. This holds
everything both parties say, in a conversation neither of them had with us. The mitigating fact
is where it lands: this process runs on the owner's own machine, so recordings are STORED only
there. Be exact about that — the media transits the platform to reach us, so "it never leaves
your machine" is false, while "stored only on your machine" is true and is the claim a regulated
buyer is actually asking about.

CONSENT IS NOT HANDLED HERE, and cannot be. Whether the parties must be told, and by whom,
depends on where each of them is. This module records what it is told to record; the setting
that switches it on says so in the file the owner reads.
"""

import asyncio
import logging
import wave
from datetime import datetime

from . import paths

logger = logging.getLogger("secretary")

#: Where recordings land, inside the instance — never the install directory, which an upgrade
#: replaces wholesale.
RECORDINGS = paths.RUN / "recordings"

#: WAV parameters. These describe what the SDK hands us, so they are not free choices: the
#: audio arrives as 24 kHz mono 16-bit PCM (`CallAudioConfig(sample_rate=...)` in the daemon).
#: Writing a different header does not convert anything — it mislabels the bytes, and the file
#: plays at the wrong speed and pitch. That exact mistake cost hours on the voice path when
#: 16 kHz was negotiated against a 24 kHz adapter.
SAMPLE_RATE = 24_000
CHANNELS = 1
SAMPLE_WIDTH = 2

#: How long to ring the destination before giving up. The SDK rejects anything outside 1–120.
RING_SECONDS = 30


def _wav_path(call_id: str, leg: str) -> "paths.pathlib.Path":
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    return RECORDINGS / f"{stamp}-{call_id}-{leg}.wav"


async def _record_leg(party, call_id: str, leg: str) -> None:
    """Drain one leg's audio into its own WAV file.

    ONE FILE PER LEG, not a mix. They arrive as separate streams because they ARE separate
    legs, and keeping them apart means a transcript can say who spoke without diarisation —
    which is the hard part of transcribing a two-party call.

    A failure here must not kill the call. The people talking do not know we exist, and losing
    a recording is a smaller harm than dropping their conversation, so this logs and returns.
    """
    path = _wav_path(call_id, leg)
    writer = None
    frames = 0
    try:
        RECORDINGS.mkdir(parents=True, exist_ok=True)
        writer = wave.open(str(path), "wb")
        writer.setnchannels(CHANNELS)
        writer.setsampwidth(SAMPLE_WIDTH)
        writer.setframerate(SAMPLE_RATE)
        async for chunk in party.audio_stream():
            writer.writeframes(chunk)
            frames += len(chunk)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("call %s: recording the %s leg failed (%s: %s)",
                     call_id, leg, type(exc).__name__, exc)
    finally:
        if writer is not None:
            try:
                writer.close()
            except OSError as exc:
                logger.warning("call %s: could not close %s (%s)", call_id, path.name, exc)
        # Report the EMPTY case distinctly. A 44-byte header with no frames is a file that
        # exists and contains nothing, which reads as "recording worked" in a directory listing
        # and is the failure most likely to go unnoticed.
        if frames:
            logger.info("call %s: wrote %s (%.1f s of the %s leg)",
                        call_id, path.name, frames / (SAMPLE_RATE * SAMPLE_WIDTH), leg)
        else:
            logger.warning("call %s: the %s leg produced NO audio — %s is empty",
                           call_id, leg, path.name)


async def handle(sm, noti) -> None:
    """Bridge one inbound call onward and record it. Never raises into the SDK's event bus."""
    call_id = getattr(noti, "call_id", "?")
    caller = getattr(getattr(noti, "participant", None), "value", "?")
    try:
        import uuid
        session = await sm.open_session(uuid.uuid4().hex, noti.subscriber)
        call = await session.process_call(noti)
    except Exception as exc:
        logger.error("call %s from %s: could not attach (%s: %s)",
                     call_id, caller, type(exc).__name__, exc)
        return

    done = asyncio.Event()

    @call.on_hangup
    def _(_evt) -> None:
        done.set()

    # RECORDERS FIRST, THEN CONNECT. `connect()` rings the destination and returns once it is
    # bridged, so a recorder started afterwards misses everything said before the far end picks
    # up — including the caller's opening words, which on an inbound call is often the whole
    # reason they rang.
    legs = [asyncio.create_task(_record_leg(call.caller, str(call.id), "caller")),
            asyncio.create_task(_record_leg(call.callee, str(call.id), "callee"))]
    try:
        # ANSWER FIRST, THEN BRIDGE. This is the documented order — the platform docs'
        # call-monitoring flow is answer -> (brief hold message) -> connect -> spy — and the SDK's
        # `connect_spy_isolated.py` example omitting `answer()` is what led this the other way
        # first. Two things change by answering:
        #
        #   The caller hears silence rather than ringing while the far end is rung, which is
        #   what any PBX does; and their audio flows from this moment, so a bridge that fails
        #   still leaves a recording of their side. Without it, a failed bridge records nothing
        #   at all — not one leg, not a second of it.
        #
        # Worth being clear-eyed about the second: on a failed bridge we are recording someone
        # waiting to be connected to a person who never arrives. That is not a new consent
        # question — this mode already records both parties — but it is the least expected
        # moment for it, so it is written down rather than left as a surprise in the logs.
        answered = await call.answer()
        if not answered:
            logger.error("call %s from %s: could not answer (%s)", call_id, caller,
                         getattr(answered, "error_code", "?"))
            return
        result = await call.connect(ring_time_seconds=RING_SECONDS)
        if not result:
            # An unanswered destination is an ORDINARY outcome, not an error: nobody picked up.
            # It is logged at info for that reason, and separately from a real failure, so a
            # quiet office does not look like a broken install.
            if getattr(result, "error_code", "") == "CALL_UNANSWERED":
                logger.info("call %s from %s: the destination did not answer", call_id, caller)
            else:
                logger.error("call %s from %s: could not bridge — %s (%s)", call_id, caller,
                             getattr(result, "error_message", "?"),
                             getattr(result, "error_code", "?"))
            return
        # SILENT, EXPLICITLY. `connect()` documents spy as its default, and the platform's own
        # call-monitoring example still calls this — so it is asked for rather than assumed. A
        # failure is logged and ignored: if the default already holds we are silent anyway, and
        # if it does not, hanging up a working call over an audio-mode command would be worse
        # than the risk it guards. Nothing here ever sends audio, so there is nothing to leak
        # into the conversation either way.
        try:
            await call.spy()
        except Exception as exc:
            logger.warning("call %s: could not confirm spy mode (%s: %s) — connect() documents "
                           "it as the default, so carrying on", call_id, type(exc).__name__, exc)
        logger.info("call %s from %s: carried through, recording both legs", call_id, caller)
        await done.wait()
    except Exception as exc:
        logger.error("call %s from %s: carrying it failed (%s: %s)",
                     call_id, caller, type(exc).__name__, exc)
    finally:
        # The streams end when the call does, but a bridge that never connected leaves them
        # open with nothing coming — so cancel rather than await, and let each recorder close
        # its own file in its finally block.
        for t in legs:
            t.cancel()
        await asyncio.gather(*legs, return_exceptions=True)
        # THE TRANSCRIPT IS NOT THIS FUNCTION'S JOB. Carrying a call ends when the audio is
        # closed on disk; a `.wav` with no sibling `.txt` is the queue, and the worker in
        # `transcribe` picks it up within a poll. That keeps the call path free of a network
        # round trip it must not depend on, survives a restart mid-transcription, and means a
        # provider being down costs a text file rather than anything on the call.


def register(sm) -> bool:
    """Claim the connector's inbound-call handler for carrying. True when registered.

    MUTUALLY EXCLUSIVE WITH `voice.register`. One connector has one `on_incoming_call`
    handler, so the daemon calls one of these and never both — the choice is the owner's
    `## Calls` setting, and the daemon logs which one it took.
    """
    @sm.on_incoming_call
    async def _handler(noti) -> None:
        # Its own task, for the same reason the voice path does it: blocking the SDK's event
        # bus for the length of a call stops any other call being set up.
        asyncio.create_task(handle(sm, noti))

    logger.info("calls are CARRIED to the configured destination, and BOTH LEGS ARE RECORDED "
                "to %s — the agent does not answer in this mode", RECORDINGS)
    return True
