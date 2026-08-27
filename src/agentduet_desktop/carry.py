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
import pathlib
import wave
from datetime import datetime

from . import callmode, paths

logger = logging.getLogger("secretary")

#: Where recordings land, inside the instance — never the install directory, which an upgrade
#: replaces wholesale.
def recordings() -> pathlib.Path:
    """Where recordings go, ASKED EACH TIME.

    This was `RECORDINGS = paths.RUN / "recordings"`, a module constant — so every importer
    froze it at import and the settings page could only ever display the folder, never change
    it. Same read-at-use-time rule the model key already had to learn.

    `carry` owns the question because it owns the files; `owner.recordings_dir()` holds the
    answer because it is a setting.
    """
    from . import owner
    return owner.recordings_dir()


#: Kept so the name still resolves for anything that reads it as a value. It is the DEFAULT,
#: not the setting — call `recordings()` for what is actually in use.
RECORDINGS = paths.RUN / "recordings"

#: Subdirectory for calls the AGENT answered. Defined here, beside the directory it sits in,
#: rather than in `voice.py` — the settings page and the hub both build this path, and reaching
#: into the answering agent for a five-letter string made two recorder endpoints import it.
ANSWERED = "answered"

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

#: The longest a carried call may hold its recorders open. A backstop, not a policy: the
#: hangup event normally ends a call long before this, and it only matters when that event
#: never arrives — which happens when the SDK thinks the call failed while it is in fact up.
MAX_CALL_SECONDS = 4 * 60 * 60


def _wav_path(call_id: str, leg: str) -> "paths.pathlib.Path":
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    return recordings() / f"{stamp}-{call_id}-{leg}.wav"


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
        recordings().mkdir(parents=True, exist_ok=True)
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
        # DO NOT ANSWER FIRST. Connect straight away.
        #
        # This was answer() -> connect() for a day, taken from the platform docs' call-monitoring
        # page, which shows answer -> hold message -> connect -> spy. the SDK author confirmed (2026-08-12) that
        # is scenario 2 and the comm side has never implemented it; scenario 1, connecting without
        # answering, is the one that works. The SDK's own connect_spy_isolated.py example does it
        # this way too, and I changed away from it on the strength of a doc page.
        #
        # The cost of answering first was not theoretical: every carry test after the SIP account
        # was configured used the unsupported order, so we spent a day reading the failure as a
        # SIP or NAT problem when it was the call flow.
        #
        # Revisit if scenario 2 lands — answering first is nicer for the caller, who currently
        # hears ringing rather than silence while the far end is rung.
        result = await call.connect(ring_time_seconds=RING_SECONDS)
        code = getattr(result, "error_code", "") if not result else ""
        if code == "CALL_UNANSWERED":
            # NOBODY PICKED UP. An ordinary outcome, not an error — logged at info so a quiet
            # office does not read as a broken install. Nothing is live, so stop here.
            logger.info("call %s from %s: the destination did not answer", call_id, caller)
            return
        if not result:
            # A FAILED COMMAND IS NOT PROOF THE CALL IS DEAD, and treating it that way threw
            # away a working recording. connect() waits `ring_time_seconds` for the SERVER'S
            # response; when that response is late the client reports TIMEOUT even though the
            # bridge is up and audio is flowing. Observed exactly that (2026-08-12): the
            # softphone rang, was answered, its level meter moved as the caller spoke — and we
            # logged TIMEOUT, cancelled both recorders, and wrote two empty files.
            #
            # So: say what happened and CARRY ON RECORDING. The hangup event below is what ends
            # the call, and it is the only thing that knows the call is really over. If the
            # bridge truly failed, the streams stay silent and the empty-file warning still
            # reports it — the cost of being wrong here is a 44-byte file, against losing a
            # recording of a real conversation.
            logger.warning("call %s from %s: connect() returned %s (%s) — the bridge may still "
                           "be up, so recording continues until hangup", call_id, caller,
                           getattr(result, "error_message", "?"), code or "?")
        else:
            logger.info("call %s from %s: carried through, recording both legs", call_id, caller)
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
        # BOUNDED. `done` is set by on_hangup, and that event does NOT arrive when the SDK
        # believes the call failed — so waiting on it alone hangs forever, holding two open
        # files and two tasks per call. Observed 2026-08-12, immediately after removing the
        # premature cancel that this replaced: one bug traded for its opposite.
        #
        # Two exits, both needed. Silence means no audio ever reached us, which is what a
        # bridge the SDK is not feeding us looks like — there is nothing to record and no
        # reason to hold the file open. MAX is the backstop for a genuine call whose hangup we
        # never hear about.
        try:
            await asyncio.wait_for(done.wait(), MAX_CALL_SECONDS)
        except asyncio.TimeoutError:
            logger.warning("call %s: no hangup after %ds — closing the recording", call_id,
                           MAX_CALL_SECONDS)
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
        # WRITE THE INDEX LAST, once the files are closed and their sizes are final. Recording
        # filenames carry a CALL ID, not a person, so without this row there is no way back from
        # a .wav to whoever was on it — which is the whole basis of a per-person view. The
        # caller is known here and was only being logged.
        from . import calls as _calls
        _calls.record(call_id, caller, "carried", recordings=sorted(
            str(p.name) for p in recordings().glob(f"*{call_id}*.wav")))
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
    # TAKE THE SLOT FIRST. One connector has one on_incoming_call, so a second
    # registration does not fail on its own — both attach and race for the call.
    callmode.claim("carry")
    @sm.on_incoming_call
    async def _handler(noti) -> None:
        # Its own task, for the same reason the voice path does it: blocking the SDK's event
        # bus for the length of a call stops any other call being set up.
        asyncio.create_task(handle(sm, noti))

    logger.info("calls are CARRIED to the configured destination, and BOTH LEGS ARE RECORDED "
                "to %s — the agent does not answer in this mode", RECORDINGS)
    return True
