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
import time
import wave
from datetime import datetime

from agentduet import OutgoingCallNotification

from . import callmode, paths, phone

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


def legs() -> pathlib.Path:
    """Where the PER-LEG audio is written, which is not where the owner looks.

    The two legs are working files: they exist because keeping the parties apart is what lets a
    transcript say who spoke without diarisation, and because a re-transcription can still
    separate the speakers later. They are not what the owner asked to keep — one file per call
    is — so they live inside the instance and only the merged pair lands in the chosen folder.

    That also splits two questions the one folder was answering at once: what still needs work
    (here) and what the owner keeps (there). And it stays restart-safe, which
    `transcribe.pending` relies on: legs left behind by a crash are still on disk, so the merge
    finishes on the next start rather than being lost.
    """
    return paths.RUN / "legs"


def stem_of(name: str) -> str:
    """`20260909T103616-<uuid>-caller.wav` -> `20260909T103616-<uuid>`."""
    base = name[:-4] if name.endswith(".wav") or name.endswith(".txt") else name
    for suffix in ("-caller", "-callee", "-agent"):
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return base


def merged_wav(stem: str) -> pathlib.Path:
    """The one file per call the owner keeps: both parties, one channel each."""
    return recordings() / f"{stem}.wav"


def merged_txt(stem: str) -> pathlib.Path:
    return recordings() / f"{stem}.txt"


def transcript_of(names: list[str], folder: pathlib.Path) -> str:
    """The labelled transcript for one call, or "" while it is still being made.

    ONE COPY OF THIS, because there are now two readers: the hub renders it, and `suggest.py`
    reads it to decide whether the call mentioned an appointment. Two copies would drift, and
    the drift would be invisible — the page would show one text and the model would judge
    another, so a suggestion would cite words the owner cannot see.

    `-caller` is always the other party and `-callee` always this line, whichever way the call
    was set up, so the labels are read off the filename rather than guessed.

    THE LABEL TEST WAS ON THE WRONG EXTENSION AND HAD NEVER FIRED. `names` holds `.wav`
    filenames — `call_audio` globs for audio — and this asked whether one ended in
    `-caller.txt`, which no `.wav` ever does. So `side` was always empty and the two legs were
    concatenated with nothing to say whose words were whose, sorted, which puts the owner's own
    side first. Invisible in normal use because the MERGED case is one file whose `.txt` already
    carries the labels inside it (`transcribe._merge_text` writes them), and that is what the
    page shows once the merge lands. It only showed through in the window before the merge
    finishes, or if it fails — which is precisely when the owner is staring at the thread
    waiting. Found 2026-09-09 by extracting this function and testing it by running it; the
    grep-based assertion it replaced could not see it.
    """
    # THE OTHER PARTY FIRST. There is no timing to order legs by — that is what the merged
    # transcript is for — so this is sorted, and plain sorting puts `-callee` (the owner's own
    # side) above `-caller`. Their words are the ones carrying the information, and an inbound
    # call opens with them, so a leg-order transcript reads the way the call went.
    parts = []
    for n in sorted(names, key=lambda x: (not pathlib.Path(x).stem.endswith("-caller"), x)):
        t = (folder / n).with_suffix(".txt")
        if not t.is_file():
            continue
        try:
            body = t.read_text().strip()
        except OSError:
            continue
        if not body:
            continue
        stem = pathlib.Path(n).stem
        side = ("them" if stem.endswith("-caller")
                else "you" if stem.endswith("-callee") else "")
        parts.append(f"{side}: {body}" if side else body)
    return "\n".join(parts)


def call_audio(names: list[str], call_id: str = "") -> tuple[pathlib.Path, list[str]]:
    """(folder, filenames) to show for one indexed call — the merge if it is done, else legs.

    The index is written when the call ENDS and the merge happens later, on the transcription
    queue, so for a few seconds a row legitimately has legs and no merge. Readers therefore
    cannot assume either one: asking only for the merged name would report a just-finished call
    as "No recording.", which is a false claim about audio that is sitting on disk.
    """
    # A ROW THAT NAMES NOTHING IS NOT PROOF THERE IS NOTHING. The index globbed the wrong
    # folder for a few minutes on 2026-09-09 and wrote `recordings: []` for a call whose audio,
    # transcripts and merge were all on disk — and an empty list reads as "No recording.", which
    # is the confident-wrong answer this file keeps hunting. The call id is in the row, so ask
    # the disk before believing the absence. Costs one glob on rows that have no files, which is
    # exactly the case where being right matters.
    if not names and call_id:
        names = sorted(p.name for p in legs().glob(f"*{call_id}*.wav"))
        if not names:
            names = sorted(p.name for p in recordings().glob(f"*{call_id}*.wav"))
    stems = {stem_of(n) for n in names}
    merged = [f"{st}.wav" for st in sorted(stems) if merged_wav(st).is_file()]
    if merged:
        return recordings(), merged
    here = [n for n in names if (legs() / n).is_file()]
    if here:
        return legs(), here
    # LEGS RECORDED BEFORE THEY MOVED. Every call carried until 2026-09-09 wrote both legs
    # straight into the owner's folder, so those rows name files that were never in `legs()`
    # and would otherwise read as "No recording." — a real transcript on disk reported as
    # absent. Nothing is migrated: they are already where the owner keeps things, and moving
    # someone's saved audio to tidy up our layout is not a fix.
    return recordings(), [n for n in names if (recordings() / n).is_file()]

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


def _wav_path(stamp: str, call_id: str, leg: str) -> "paths.pathlib.Path":
    """One leg's working file. THE STAMP IS PASSED IN, not taken here.

    It used to call `datetime.now()` itself, once per leg — so the two legs of a call got
    different stamps whenever they straddled a second boundary, and the pair no longer shared a
    stem. Nothing noticed while every reader globbed on the call id, and it breaks the moment
    the merge has to find one leg from the other. Today's recordings match by luck.
    """
    return legs() / f"{stamp}-{call_id}-{leg}.wav"


async def _record_leg(party, stamp: str, call_id: str, leg: str, live_leg=None) -> None:
    """Drain one leg's audio into its own WAV file.

    ONE FILE PER LEG, not a mix. They arrive as separate streams because they ARE separate
    legs, and keeping them apart means a transcript can say who spoke without diarisation —
    which is the hard part of transcribing a two-party call.

    A failure here must not kill the call. The people talking do not know we exist, and losing
    a recording is a smaller harm than dropping their conversation, so this logs and returns.
    """
    final = _wav_path(stamp, call_id, leg)
    # WRITTEN AS `.part` AND RENAMED ON CLOSE, so nothing downstream can ever see a leg that is
    # still being recorded. `transcribe.pending()` treats any non-empty `*.wav` as work, so
    # while the file was created under its final name the transcriber would pick it up MID-CALL,
    # write a `.txt` for the few seconds captured so far, and the merge — which waits only for
    # every leg to have a transcript — would then produce the finished recording from partial
    # audio and mark it done. Stanley's 15:22 call came out as 10.5 seconds of a 24-second
    # conversation, with both legs intact on disk beside it.
    #
    # The race was always there and today made it near-certain: whisper.cpp is twelve times
    # faster than the engine it replaced, and the worker now starts before the connector rather
    # than after, so the queue is drained while the call is still going.
    #
    # A rename on the same directory is atomic, so a reader sees the file either not at all or
    # complete. `.part` is also the convention `models.py` already uses for a download in
    # flight, including its stale-part handling.
    path = final.with_name(final.name + ".part")
    writer = None
    frames = 0
    # THIS leg's own clock. It was the process start time for one draft, which would have
    # printed the daemon's uptime and read as a call duration — a wrong number in a diagnostic
    # is worse than no number, because it is the one the next person reasons from.
    started = time.time()
    try:
        legs().mkdir(parents=True, exist_ok=True)
        writer = wave.open(str(path), "wb")
        writer.setnchannels(CHANNELS)
        writer.setsampwidth(SAMPLE_WIDTH)
        writer.setframerate(SAMPLE_RATE)
        async for chunk in party.audio_stream():
            # WHEN THIS LEG ACTUALLY STARTED TALKING, written once, beside the audio.
            #
            # The merge needs it and cannot recover it afterwards. Both recorders are created
            # in the same breath, but the streams do not begin yielding together — the far leg
            # is originated toward the PBX and may ring for seconds before it carries anything.
            # Treating sample zero of each file as the same instant would put one side of the
            # conversation ahead of the other by exactly that gap.
            #
            # A sidecar per leg rather than one file for the call, because two tasks writing
            # one JSON is a race for no benefit.
            if not frames:
                try:
                    final.with_suffix(".start").write_text(f"{time.time():.3f}\n")
                except OSError as exc:
                    logger.warning("call %s: could not note the %s leg's start (%s)",
                                   call_id, leg, exc)
            writer.writeframes(chunk)
            frames += len(chunk)
            # THE LIVE PREVIEW sees every chunk the file does. Never allowed to cost the
            # recording: a fault here is logged once and the tap is dropped for this leg.
            if live_leg is not None:
                try:
                    live_leg.feed(chunk)
                except Exception as exc:
                    logger.warning("call %s: live captions stopped for the %s leg (%s: %s)",
                                   call_id, leg, type(exc).__name__, exc)
                    live_leg = None
        # THE STREAM ENDED BY ITSELF, which is the case worth separating. Reaching here means
        # `audio_stream` raised StopAsyncIteration — the SDK puts a sentinel on the queue when
        # its voice session closes — rather than this task being cancelled at the end of the
        # call. If it happens while the call is still up, the audio stops and nothing says so:
        # exactly a recording that ends mid-sentence. Logged loudly because the fix depends on
        # knowing which of the two happened, and a normal end is indistinguishable in the file.
        logger.warning("call %s: the %s leg's audio stream ENDED ON ITS OWN after %.1fs of "
                       "audio (%.1fs of wall clock) — the SDK closed it rather than us; if the "
                       "call was still up, this is where the recording stops",
                       call_id, leg, frames / (SAMPLE_RATE * SAMPLE_WIDTH),
                       time.time() - started)
    except asyncio.CancelledError:
        # THE NORMAL END: the call finished and `handle` cancelled us.
        logger.info("call %s: the %s leg was closed with the call (%.1fs of audio)",
                    call_id, leg, frames / (SAMPLE_RATE * SAMPLE_WIDTH))
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
            # PUBLISH IT: until this rename the file is invisible to the queue, and after it the
            # file is complete, because the writer above is already closed.
            #
            # NOT VIA `return` IN A `finally`, which is how I first wrote it — a return there
            # swallows the exception in flight, and the exception in flight here is the
            # CancelledError this task is stopped with at the end of every call. The
            # cancellation has to keep propagating.
            try:
                path.replace(final)
            except OSError as exc:
                logger.error("call %s: recorded the %s leg but could not publish it (%s) — the "
                             "audio is at %s", call_id, leg, exc, path)
            logger.info("call %s: wrote %s (%.1f s of the %s leg)",
                        call_id, final.name, frames / (SAMPLE_RATE * SAMPLE_WIDTH), leg)
        else:
            # AND THEN REMOVE IT. Logging that the file is empty was the whole answer for a
            # month, and a warning in yesterday's log does not help someone opening the folder
            # today. The LOG is the record that a leg produced nothing, `calls.jsonl` is the
            # record that the call happened, and the hub already says "No recording." from an
            # empty file list — so the header on disk is the one copy of this fact that can
            # mislead, and it is the copy nobody asked for.
            #
            # The `.start` sidecar goes with it. It only exists to align this leg against the
            # other, and there is nothing left to align.
            logger.warning("call %s: the %s leg produced NO audio — discarding %s",
                           call_id, leg, path.name)
            for junk in (path, final.with_suffix(".start")):
                try:
                    junk.unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning("call %s: could not remove %s (%s)", call_id, junk.name, exc)


async def handle(sm, noti) -> None:
    """Bridge one call onward and record it. Never raises into the SDK's event bus.

    Takes either direction. An INBOUND call is someone ringing the connector's number; an
    OUTGOING one is the owner's own line placing a call outside the SDK — a desk phone, or the
    SIM in their hand. `session.process_call` accepts both and the call operations are identical.
    """
    call_id = getattr(noti, "call_id", "?")
    # `participant` is the OTHER party in both directions: whoever rang in, or whoever was rung.
    other = getattr(getattr(noti, "participant", None), "value", "?")
    outgoing = isinstance(noti, OutgoingCallNotification)
    # "from +65…" or "to +65…", so every line below reads correctly in either
    # direction. The format strings must NOT also say "from" — that produced
    # "call c1 from from +6591234567".
    who = f"{'to' if outgoing else 'from'} {other}"
    try:
        import uuid
        session = await sm.open_session(uuid.uuid4().hex, noti.subscriber)
        call = await session.process_call(noti)
    except Exception as exc:
        logger.error("call %s %s: could not attach (%s: %s)",
                     call_id, who, type(exc).__name__, exc)
        return

    done = asyncio.Event()
    taken = time.time()

    @call.on_hangup
    def _(_evt) -> None:
        # SAY WHY, because the reasons are not equivalent and the event was discarded. The SDK
        # SYNTHESISES a terminated event when its own transport dies — `reason:
        # "transport_closed"` — so a network blip on our side is delivered here exactly like the
        # far end hanging up, and we stop recording a call that is still in progress. Stanley's
        # calls stop at ~22s with both legs ending mid-speech at full volume, which is what that
        # looks like. Until this line the log could not tell the two apart.
        why = ""
        for attr in ("reason", "code", "type"):
            got = getattr(_evt, attr, None) or (
                _evt.get(attr) if isinstance(_evt, dict) else None)
            if got:
                why = f"{attr}={got}"
                break
        logger.info("call %s: hangup received (%s) after %.1fs — closing the recording",
                    call_id, why or f"no reason on {type(_evt).__name__}",
                    time.time() - taken)
        done.set()

    # RING HERE FIRST, when the owner asked for it and a page is open to ring. Inbound only: an
    # outgoing call is the owner's own line, already in their hand. Anything but an answer falls
    # through to the ordinary pass-through below, so turning this on can never cost a call that
    # would otherwise have reached the destination.
    from . import owner as _owner          # at use time, like every setting read here
    if not outgoing and _owner.answer_here() and phone.present():
        decision = await phone.ring(str(call_id), other, done)
        if decision == "answer":
            await _answer_here(call, call_id, other, done)
            return
        if decision == "gone":
            logger.info("call %s %s: the caller hung up while it rang in the app", call_id, who)
            from . import calls as _calls
            _calls.record(call_id, other, "carried", note="missed in the app")
            return
        logger.info("call %s %s: %s in the app — passing it through", call_id, who, decision)

    # RECORDERS FIRST, THEN CONNECT. `connect()` rings the destination and returns once it is
    # bridged, so a recorder started afterwards misses everything said before the far end picks
    # up — including the caller's opening words, which on an inbound call is often the whole
    # reason they rang.
    # BOUND BY ROLE, NOT BY FIELD — the SDK's `process_call` says so outright: "the frame and
    # the call operations are identical — only `caller`/`callee` swap" between the directions.
    # So on a call the owner PLACED, `call.caller` is the owner's own line and `call.callee` is
    # the far party, the exact reverse of an inbound call. Recording the fields straight through
    # would put the far party's audio in the file named for the near one, and nothing downstream
    # could tell: the transcript would simply attribute every sentence to the wrong person.
    #
    # `<id>-caller.wav` therefore always holds the OTHER party and `<id>-callee.wav` always
    # holds this line, whichever way the call was set up.
    far, near = (call.callee, call.caller) if outgoing else (call.caller, call.callee)
    # ONE STAMP FOR THE CALL, so both legs share a stem and the merge can find the pair.
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    # THE INDEX'S call id, not `call.id`: the hub matches a finished call's live captions to its
    # history row by this, and the row is written with the notification's id.
    captions = await _live_start(str(call_id), other)
    # `recorders`, not `legs` — that name is now the folder they are written to, and a local
    # shadowing it here is a trap for whoever next needs the folder in this function.
    recorders = [asyncio.create_task(_record_leg(far, stamp, str(call.id), "caller",
                                                 captions and captions[0])),
                 asyncio.create_task(_record_leg(near, stamp, str(call.id), "callee",
                                                 captions and captions[1]))]
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
            logger.info("call %s %s: the destination did not answer", call_id, who)
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
            logger.warning("call %s %s: connect() returned %s (%s) — the bridge may still "
                           "be up, so recording continues until hangup", call_id, who,
                           getattr(result, "error_message", "?"), code or "?")
        else:
            logger.info("call %s %s: carried through, recording both legs", call_id, who)
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
        logger.error("call %s %s: carrying it failed (%s: %s)",
                     call_id, who, type(exc).__name__, exc)
    finally:
        # The streams end when the call does, but a bridge that never connected leaves them
        # open with nothing coming — so cancel rather than await, and let each recorder close
        # its own file in its finally block.
        for t in recorders:
            t.cancel()
        await asyncio.gather(*recorders, return_exceptions=True)
        await _live_end(str(call_id))
        # WRITE THE INDEX LAST, once the files are closed and their sizes are final. Recording
        # filenames carry a CALL ID, not a person, so without this row there is no way back from
        # a .wav to whoever was on it — which is the whole basis of a per-person view. The
        # caller is known here and was only being logged.
        from . import calls as _calls
        # THE NUMBER IS THE PERSON. `who` carries a direction word for the log ("from +65…",
        # "to +65…") and putting that in the index FRAGMENTED the people list: the same number
        # appeared as two or three entries — bare, "to", and "from" — each with its own
        # conversation history, so a person you rang and who rang you back were strangers to
        # each other. Direction is a property of the CALL and belongs in its own field.
        # GLOB THE LEGS, which is where the audio now IS. This still asked the owner's folder
        # after the legs moved out of it this morning, so every row written since would have
        # named no files at all — and the hub reads that as "No recording." on a call whose
        # audio is sitting on disk. Caught before the first live call, by adding the
        # empty-leg cleanup and asking what the index would then have to work with.
        _calls.record(call_id, other, "carried", outgoing=outgoing, recordings=sorted(
            str(p.name) for p in legs().glob(f"*{call_id}*.wav")))
        # TRANSCRIBE IT NOW, not at the next poll: the legs are closed and on disk.
        from . import transcribe
        transcribe.wake()
        # THE TRANSCRIPT IS NOT THIS FUNCTION'S JOB. Carrying a call ends when the audio is
        # closed on disk; a `.wav` with no sibling `.txt` is the queue, and the worker in
        # `transcribe` picks it up within a poll. That keeps the call path free of a network
        # round trip it must not depend on, survives a restart mid-transcription, and means a
        # provider being down costs a text file rather than anything on the call.


async def _live_start(call_id: str, other: str):
    """Mark the call live for the hub and return its two caption cutters, or None.

    None when captions cannot run (the speech engine is not Qwen, or its model is not here), and
    never raises: a preview must not be able to cost the call or its recording.
    """
    from . import calls as _calls, live
    try:
        await live.start(call_id, _calls.person_of({"caller": other}))
        if not live.enabled():
            return None
        return live.Leg(call_id, "caller"), live.Leg(call_id, "callee")
    except Exception as exc:
        logger.warning("call %s: live captions unavailable (%s: %s)", call_id,
                       type(exc).__name__, exc)
        return None


async def _live_end(call_id: str) -> None:
    from . import live
    try:
        await live.end(call_id)
    except Exception as exc:
        logger.warning("call %s: could not close live captions (%s)", call_id, exc)


async def _answer_here(call, call_id: str, other: str, done: asyncio.Event) -> None:
    """The owner picked up in the app: answer, bridge to the page, record both sides.

    The SDK's own "Answer a call" shape — `answer()`, the caller's stream in, `send_audio()` out
    — with the owner's microphone where its echo loop is. Recorded as the same caller/callee
    legs a carried call writes, so the merge and transcription are unchanged: the caller from
    their stream, the owner from the microphone as it is sent.
    """
    from . import calls as _calls
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    far_rec, near_rec = phone._QueueParty(), phone._QueueParty()
    captions = await _live_start(str(call_id), other)
    recorders = [asyncio.create_task(_record_leg(far_rec, stamp, str(call.id), "caller",
                                                 captions and captions[0])),
                 asyncio.create_task(_record_leg(near_rec, stamp, str(call.id), "callee",
                                                 captions and captions[1]))]
    try:
        if not await call.answer():
            logger.error("call %s from %s: answer() failed after it was picked up in the app",
                         call_id, other)
            return
        logger.info("call %s from %s: answered in the app, recording both sides", call_id, other)
        await phone.bridge(call, done, far_rec, near_rec)
    except Exception as exc:
        logger.error("call %s from %s: the in-app call failed (%s: %s)",
                     call_id, other, type(exc).__name__, exc)
    finally:
        await phone.finish()
        for t in recorders:
            t.cancel()
        await asyncio.gather(*recorders, return_exceptions=True)
        await _live_end(str(call_id))
        _calls.record(call_id, other, "carried", note="answered in the app", recordings=sorted(
            str(p.name) for p in legs().glob(f"*{call_id}*.wav")))
        from . import transcribe
        transcribe.wake()


def register(sm) -> bool:
    """Claim the connector's inbound-call handler for carrying. True when registered.

    MUTUALLY EXCLUSIVE WITH `voice.register`. One connector has one `on_incoming_call`
    handler, so the daemon calls one of these and never both — the choice is the owner's
    `## Calls` setting, and the daemon logs which one it took.
    """
    # TAKE THE SLOT FIRST. One connector has one on_incoming_call, so a second
    # registration does not fail on its own — both attach and race for the call.
    callmode.claim("carry")

    async def _handler(noti) -> None:
        # Its own task, for the same reason the voice path does it: blocking the SDK's event
        # bus for the length of a call stops any other call being set up.
        asyncio.create_task(handle(sm, noti))

    # BOTH DIRECTIONS, and the second one is not a nicety. A call the owner's own line PLACES
    # — a desk phone, or the SIM in their hand — arrives as an outgoing announcement, not an
    # incoming call, so subscribing to inbound alone means the app sees nothing at all and the
    # recordings directory stays empty with no error anywhere. Found on 2026-09-08 testing a
    # real Singtel SIM: the platform logged `callBegin` with `type2: outgoing` and
    # `P-B3-CALL-DIRECTION: outgoing`, and the daemon logged not one line.
    #
    # ONE HANDLER SERVES BOTH because the notifications are the same shape —
    # `IncomingCallNotification` and `OutgoingCallNotification` both carry call_id, subscriber,
    # participant, created_at — and the SDK's own docstring says to "handle it exactly like an
    # incoming call": open a session, attach, then connect.
    sm.on_incoming_call(_handler)
    sm.on_outgoing_call(_handler)

    # SAY WHERE THE OWNER'S FILE LANDS, not where the working files go. This said "BOTH LEGS
    # ARE RECORDED to <recordings>" and both halves stopped being true on 2026-09-09: the legs
    # are written to run/legs, and what arrives in the owner's folder is one merged recording
    # per call. A start-up line naming the wrong directory sends someone to an empty folder.
    logger.info("calls are CARRIED to the configured destination and RECORDED — both legs, "
                "merged into one stereo file per call in %s (the legs themselves are working "
                "files in %s) — inbound AND calls this line places; the agent does not answer "
                "in this mode", recordings(), legs())
    return True
