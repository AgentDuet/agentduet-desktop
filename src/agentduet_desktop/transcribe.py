"""Turn recorded call legs into text, on a queue, with or without a network.

TWO ENGINES, ONE INTERFACE.

  hosted  `qwen3-asr-flash` on DashScope. More accurate, needs the owner's model key.
  local   faster-whisper on the CPU. No key, no network, nothing leaves the machine.

Hosted is preferred when a credential exists, because it is measurably better on the same
audio — hosted returned "Sir, ma'am … Trusty's Security Department" where local `base` gave
"Sarah, ma'am … trustee security department". Local is the one that makes the feature work at
all for an owner who has not attached a model, and on the recording path that is the common
case: carrying a call needs no LLM, so requiring one just to read back what was said would be
an odd tax.

Neither is a chat model. `qwen3-asr-flash` is a dedicated ASR *task* — it refuses a text part
alongside the audio, which is how that was discovered — and Whisper takes no instruction at all.
On a path whose entire input is a stranger's voice, an engine that cannot be instructed is the
right shape rather than a limitation.

THE QUEUE IS THE FILESYSTEM. A `.wav` with no sibling `.txt` is work to do. There is no queue
file to corrupt, lose or get out of step with the recordings, and it is restart-safe by
construction: a daemon that dies mid-transcription finds the same job waiting when it comes
back. Re-running one means deleting its `.txt`. A permanent failure writes `.failed` beside the
audio so it stops being retried; deleting that re-queues it.

Slow is fine here. This runs after the call, off the event loop, and the audio — the part that
cannot be recreated — is already closed on disk before any of it starts.
"""

import asyncio
import base64
import io
import logging
import os
import pathlib
import wave

logger = logging.getLogger("secretary")

#: The hosted ASR task model.
MODEL = os.getenv("SECRETARY_ASR_MODEL", "qwen3-asr-flash")

#: How hard the local engine tries. Measured on a real 22s call, against the hosted engine's
#: "Hi! Hello, hello! What can you do? Okay. Err. Never mind. Bye. Bye.":
#:
#:   fast      base   14x realtime  ~145 MB   "…what you do? Okay, uh, never mind, I'm fine."
#:   balanced  small  5.8x          ~484 MB   "Hi, hello, hello, okay, do okay, nevermind, bye"
#:   accurate  medium 3.1x          ~1.5 GB   "…what can you do? Okay, never mind, bye-bye."
#:
#: `accurate` is the only one that recovered "what can you do", and at 3x realtime a five-minute
#: call still finishes in under two minutes — which is free, because this runs after the call on
#: a queue and nothing waits for it. BALANCED is the default only because the model downloads on
#: first use and 1.5 GB is a surprise to hand someone who never looks at a transcript.
#:
#: `max` is large-v3, ~3 GB, and it EARNS ITS PLACE on real call lengths — which a 22-second
#: clip did not show. On that clip it matched medium word for word, and the first version of
#: this comment concluded there was no point to it. On 57s and 88s recordings the two agree only
#: ~80% of the time, and the differences change meaning:
#:
#:   medium  "can you WAIT FOR my credit card bill?"     large  "can you WAVE my credit card bill?"
#:   medium  "my name is Spandy Leong"                   large  "my name is Standee Leong"
#:
#: Still 4.4x realtime on the 88s file, so cost is download size, not time. The lesson is about
#: the sample, not the model: a short clip starves both equally and hides the difference.
QUALITY = {"fast": "base", "balanced": "small", "accurate": "medium", "max": "large-v3"}
LOCAL_QUALITY = os.getenv("SECRETARY_STT_QUALITY", "")

#: BEAM 5 AND VAD ALWAYS, at every tier. Not a trade: beam=5 with VAD measured FASTER than the
#: beam=1 default it replaces (14x against 10.8x), because VAD strips silence so there is less
#: audio to decode. The old default was the worst of both — slower AND greedier.
BEAM_SIZE = 5
VAD = True


def local_model() -> str:
    """The faster-whisper model to load. An explicit model name still wins over the tier."""
    if name := os.getenv("SECRETARY_STT_MODEL"):
        return name
    from . import owner
    tier = (LOCAL_QUALITY or owner.transcription_quality() or "balanced").lower()
    return QUALITY.get(tier, QUALITY["balanced"])

#: Audio is sent inline as base64, so one hosted request carries about 1.4x the WAV's bytes. A
#: minute of 24 kHz mono 16-bit is ~2.8 MB; a ten-minute call in one request would be ~38 MB.
#: Chunking makes request size a function of this constant rather than call length. The local
#: engine has no such limit and reads the whole file.
CHUNK_SECONDS = 60

#: A WAV header with no frames. Written when a call produced no audio at all — which is what an
#: unbridged call looks like — and there is nothing to transcribe in one.
EMPTY_WAV_BYTES = 64

#: How often the worker looks for work. Long, because nothing is waiting on it: the call is over
#: and the audio is safe on disk.
POLL_SECONDS = 20


class TranscriptionUnavailable(RuntimeError):
    """No engine can run. Recording must survive this."""


# ---- engines ---------------------------------------------------------------------------

def _hosted_key() -> str | None:
    from . import llm
    return llm._DashScope.credential()


def _local_available() -> bool:
    # find_spec, not a try/import: importing faster_whisper pulls in a CPU inference runtime and
    # costs a second or more, and this is called from `status` and from every queue poll.
    import importlib.util
    return importlib.util.find_spec("faster_whisper") is not None


def engine() -> str:
    """`hosted`, `local`, or `` when neither can run."""
    if _hosted_key():
        return "hosted"
    return "local" if _local_available() else ""


def available() -> tuple[bool, str]:
    """(can transcribe, why not). Checked at start-up so the owner learns before a call."""
    which = engine()
    if which == "hosted":
        return True, ""
    if which == "local":
        return True, ""
    return False, ("no model key and faster-whisper is not installed — recordings will be kept, "
                   "but not transcribed. `pip install 'agentduet-desktop[stt]'` transcribes "
                   "on this machine with no key and no network.")


def describe() -> str:
    """One line for `status`, naming which engine would actually run."""
    which = engine()
    if which == "hosted":
        return f"hosted ({MODEL})"
    if which == "local":
        return f"local ({local_model()}, on this machine)"
    return "OFF — " + available()[1]


def _chunks(path: pathlib.Path, seconds: int = CHUNK_SECONDS):
    """The WAV as a series of self-contained smaller WAVs, each with its own header."""
    with wave.open(str(path), "rb") as src:
        params = src.getparams()
        per = params.framerate * seconds
        while True:
            frames = src.readframes(per)
            if not frames:
                return
            buf = io.BytesIO()
            with wave.open(buf, "wb") as out:
                out.setnchannels(params.nchannels)
                out.setsampwidth(params.sampwidth)
                out.setframerate(params.framerate)
                out.writeframes(frames)
            yield buf.getvalue()


def _hosted_one(audio: bytes, key: str) -> str:
    import httpx
    region = (os.getenv("DASHSCOPE_REGION") or "intl").strip().lower()
    host = "dashscope-intl.aliyuncs.com" if region == "intl" else "dashscope.aliyuncs.com"
    b64 = base64.b64encode(audio).decode()
    # AUDIO ONLY. A text part is rejected: this is a dedicated ASR task, not a chat model that
    # happens to hear. Adding "please transcribe" here would break every call.
    body = {"model": MODEL, "messages": [{"role": "user", "content": [
        {"type": "input_audio",
         "input_audio": {"data": f"data:audio/wav;base64,{b64}", "format": "wav"}}]}]}
    r = httpx.post(f"https://{host}/compatible-mode/v1/chat/completions", json=body,
                   headers={"Authorization": f"Bearer {key}"}, timeout=180.0)
    if r.status_code != 200:
        raise TranscriptionUnavailable(f"{r.status_code}: {r.text[:200]}")
    return (r.json()["choices"][0]["message"].get("content") or "").strip()


_local_model = None
_loaded_name = ""


def _local(path: pathlib.Path) -> str:
    """Whisper on the CPU. The model is loaded ONCE and reused for the life of the process.

    Loading is the expensive part — seconds, and hundreds of MB resident — so re-loading it per
    file would dominate the run and let two copies exist at once. That is also why the worker
    drains strictly one at a time.
    """
    global _local_model, _loaded_name
    from faster_whisper import WhisperModel
    want = local_model()
    # Reload when the tier CHANGES. Without this, raising the quality does nothing until the
    # daemon restarts, and the owner sees no difference from a setting they just changed.
    if _local_model is None or _loaded_name != want:
        logger.info("loading the local speech model (%s) — first use downloads it", want)
        _local_model = WhisperModel(want, device="cpu", compute_type="int8")
        _loaded_name = want
    from . import owner
    lang = os.getenv("SECRETARY_STT_LANGUAGE") or owner.language() or None
    # PRIMING WITH THE OWNER'S NAME beats a bigger model, and costs nothing. Measured on an 88s
    # call: medium heard "my name is Spandy Leong"; primed with "Stanley Leong" it heard it
    # correctly, which neither medium nor large-v3 managed unprimed. A caller saying the owner's
    # name is the commonest proper noun on this path and the one most worth getting right.
    #
    # A PUNCTUATED SENTENCE, not a bare name — the prompt sets STYLE as well as vocabulary, and
    # that is not obvious until it bites. Measured on the same 88s call with large-v3:
    #
    #   "Stanley Leong"             -> "hi hi uh can i waive my credit card bill ah okay my
    #                                   name is uh standee leong last four digit is 5678"
    #   "Stanley Leong."            -> "Hi, hi, can I waive my credit card bill? Okay, my name
    #                                   is Standy Leong. Last four digits is 5678."
    #   "A call for Stanley Leong." -> the same, with the name CORRECT.
    #
    # An unpunctuated prompt teaches it to write unpunctuated lowercase text, and the whole
    # transcript loses its sentence boundaries. Kept short and factual regardless: Whisper will
    # echo this prompt into the output when the audio is silent or unclear, so every word here
    # is a word that can appear in a transcript nobody said.
    name = owner.name()
    prompt = f"A call for {name}." if name and name != owner.DEFAULT_NAME else None
    segments, info = _local_model.transcribe(str(path), beam_size=BEAM_SIZE, language=lang,
                                             vad_filter=VAD, initial_prompt=prompt)
    text = "".join(s.text for s in segments).strip()
    if lang is None:
        # SAY WHAT IT GUESSED. A wrong guess produces a fluent transcript of the wrong language,
        # which reads as a broken recording rather than a misconfiguration — this is the only
        # place that difference is visible.
        logger.info("%s: language guessed as %s (p=%.2f) — set `## Language` in settings.md if "
                    "that is wrong", path.name, info.language, info.language_probability)
    return text


def transcribe(path: pathlib.Path) -> str:
    """The words on one recorded leg. Raises TranscriptionUnavailable; never returns a guess."""
    which = engine()
    if which == "local":
        return _local(path)
    key = _hosted_key()
    if not key:
        raise TranscriptionUnavailable(available()[1])

    parts: list[str] = []
    for i, chunk in enumerate(_chunks(path)):
        try:
            if text := _hosted_one(chunk, key):
                parts.append(text)
        except TranscriptionUnavailable:
            raise
        except Exception as exc:
            # Marked in place rather than dropped: a transcript silently missing its middle
            # minute reads as a complete record of a shorter call.
            logger.error("transcribing %s chunk %d failed (%s: %s)",
                         path.name, i, type(exc).__name__, exc)
            parts.append(f"[minute {i + 1}: not transcribed]")
    return " ".join(parts).strip()


# ---- the queue -------------------------------------------------------------------------

def pending() -> list[pathlib.Path]:
    """Recordings still needing a transcript, oldest first.

    Derived, not stored. Skips the two cases that are not work: a WAV that is only a header
    (an unbridged call produces exactly that), and one already marked `.failed`.
    """
    from . import carry
    if not carry.RECORDINGS.is_dir():
        return []
    out = []
    for wav in sorted(carry.RECORDINGS.glob("*.wav")):
        if wav.with_suffix(".txt").exists() or wav.with_suffix(".failed").exists():
            continue
        if wav.stat().st_size <= EMPTY_WAV_BYTES:
            continue
        out.append(wav)
    return out


def _record(wav: pathlib.Path, text: str) -> None:
    """File the transcript where the rest of the product reads it."""
    from . import brain
    # `<stamp>-<call id>-<leg>.wav`. The leg is the last field and the call id the rest, because
    # a call id contains hyphens and a stamp does not.
    stem = wav.stem.split("-")
    leg = stem[-1]
    call_id = "-".join(stem[1:-1])
    wav.with_suffix(".txt").write_text(text + "\n")
    # outcome="carried" marks a call nobody answered. It is not an exchange with the agent, and
    # filing it as one would misreport what happened.
    brain.record("", f"[carried call, {leg}]", "carried", "", text,
                 None, "TELCO", None, False, f"call-{call_id}")


def drain_once() -> int:
    """Transcribe every pending recording. Returns how many were written."""
    jobs = pending()
    if not jobs:
        return 0
    ok, why = available()
    if not ok:
        logger.warning("%d recording(s) waiting, but %s", len(jobs), why)
        return 0

    done = 0
    for wav in jobs:
        try:
            text = transcribe(wav)
        except Exception as exc:
            # A PERMANENT marker, so a corrupt or unreadable file is not retried every poll
            # forever. Deleting the `.failed` re-queues it.
            logger.error("could not transcribe %s (%s: %s)", wav.name, type(exc).__name__, exc)
            wav.with_suffix(".failed").write_text(f"{type(exc).__name__}: {exc}\n")
            continue
        if not text:
            logger.info("%s had no speech in it", wav.name)
            wav.with_suffix(".txt").write_text("")
            continue
        _record(wav, text)
        done += 1
        logger.info("transcribed %s (%d chars, %s)", wav.name, len(text), engine())
    return done


async def worker() -> None:
    """Drain the queue forever, one file at a time, off the event loop.

    STRICTLY SEQUENTIAL. The local engine holds a model resident, so two at once would double
    the memory for no gain on a CPU that is already the bottleneck — and nothing is waiting on
    the result anyway.
    """
    while True:
        await asyncio.sleep(POLL_SECONDS)
        try:
            await asyncio.to_thread(drain_once)
        except Exception as exc:            # a worker that dies takes the queue with it
            logger.error("the transcription worker hit %s: %s", type(exc).__name__, exc)
