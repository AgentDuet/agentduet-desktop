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

#: The local model. `base` is the default trade: ~145 MB, about 7x realtime on a laptop CPU, and
#: noticeably better than `tiny` on names. Anything faster-whisper accepts works — `tiny` when
#: size matters, `small` when accuracy does.
LOCAL_MODEL = os.getenv("SECRETARY_STT_MODEL", "base")

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
        return f"local ({LOCAL_MODEL}, on this machine)"
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


def _local(path: pathlib.Path) -> str:
    """Whisper on the CPU. The model is loaded ONCE and reused for the life of the process.

    Loading is the expensive part — seconds, and hundreds of MB resident — so re-loading it per
    file would dominate the run and let two copies exist at once. That is also why the worker
    drains strictly one at a time.
    """
    global _local_model
    from faster_whisper import WhisperModel
    if _local_model is None:
        logger.info("loading the local speech model (%s) — first use may download it", LOCAL_MODEL)
        _local_model = WhisperModel(LOCAL_MODEL, device="cpu", compute_type="int8")
    segments, _info = _local_model.transcribe(str(path), beam_size=1)
    return "".join(s.text for s in segments).strip()


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
