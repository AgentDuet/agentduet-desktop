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

#: The hosted ASR task model. A function for the same reason as `local_model()`: a value read
#: once at import cannot be changed without restarting the daemon.
def hosted_model() -> str:
    return os.getenv("SECRETARY_ASR_MODEL", "qwen3-asr-flash")

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

#: BEAM 5 AND VAD ALWAYS, at every tier. Not a trade: beam=5 with VAD measured FASTER than the
#: beam=1 default it replaces (14x against 10.8x), because VAD strips silence so there is less
#: audio to decode. The old default was the worst of both — slower AND greedier.
BEAM_SIZE = 5
VAD = True


def local_model() -> str:
    """The faster-whisper model to load. An explicit model name still wins over the tier."""
    # READ AT USE TIME, never captured at import. This was a module constant, so changing the
    # tier did nothing until a restart — and the settings page deliberately writes into the
    # RUNNING process's environment so that a restart is not needed. CLAUDE.md calls this out
    # by name; I reintroduced it anyway, which is what a documented gotcha is for.
    if name := os.getenv("SECRETARY_STT_MODEL"):
        return name
    from . import owner
    tier = (os.getenv("SECRETARY_STT_QUALITY") or owner.transcription_quality()
            or "balanced").lower()
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

#: How many times a recording is retried before it is written off. Exists because the first
#: transcription on a fresh install DOWNLOADS the speech model — hundreds of megabytes, or 2.9 GB
#: at `max` — and a network failure there says nothing about the recording.
MAX_ATTEMPTS = 3


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
        return f"hosted ({hosted_model()})"
    if which == "local":
        return f"local ({local_model()}, on this machine)"
    return "OFF — " + available()[1]


#: Roughly what each tier costs to fetch, for telling the owner BEFORE it happens rather than
#: after. Measured from the cache on disk, not from the docs.
MODEL_MB = {"tiny": 75, "base": 142, "small": 464, "medium": 1500, "large-v3": 2900}


def is_cached(model: str = "") -> bool:
    """Is the local model already on disk? Never downloads to find out."""
    try:
        from faster_whisper.utils import download_model
        download_model(model or local_model(), local_files_only=True)
        return True
    except Exception:
        return False


def fetch(model: str = "") -> str:
    """Download the local model now. Blocking, and the whole point of calling it early.

    Without this the model arrives on the FIRST TRANSCRIPTION — hundreds of megabytes, or 2.9 GB
    at `max`, fetched silently from a background worker at whatever moment a call happens to end.
    On a metered connection that is rude, and when it fails the recording it was working on is
    what pays. Better to say the number and let the owner choose the moment.
    """
    name = model or local_model()
    from faster_whisper.utils import download_model
    download_model(name)
    return name


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
    body = {"model": hosted_model(), "messages": [{"role": "user", "content": [
        {"type": "input_audio",
         "input_audio": {"data": f"data:audio/wav;base64,{b64}", "format": "wav"}}]}]}
    r = httpx.post(f"https://{host}/compatible-mode/v1/chat/completions", json=body,
                   headers={"Authorization": f"Bearer {key}"}, timeout=180.0)
    if r.status_code != 200:
        raise TranscriptionUnavailable(f"{r.status_code}: {r.text[:200]}")
    return (r.json()["choices"][0]["message"].get("content") or "").strip()


_local_model = None
_loaded_name = ""


#: The macOS release that first shipped SpeechAnalyzer/SpeechTranscriber, the long-form API.
#: The older SFSpeechRecognizer exists further back but was built for dictation and caps a
#: request at about a minute, which is useless for a call.
ANE_MIN_MACOS = 26


def ane_support() -> tuple[bool, str]:
    """Can this machine use the Apple Neural Engine for speech, and if not, why not.

    NOT IMPLEMENTED YET — this only answers whether it COULD be. The UI offers the option and
    disables it where the answer is no, so the reason has to be a sentence a person can act on
    ("your Mac is too old") rather than a boolean.

    The ANE is a separate accelerator on Apple Silicon, and nothing addresses it directly: you
    hand a model to Core ML and Core ML decides where the ops run. So the real test is not "is
    there an ANE" but "is the API that uses it present", which is a macOS version question.
    """
    import platform
    if platform.system() != "Darwin":
        return False, "only on a Mac"
    if platform.machine() not in ("arm64", "aarch64"):
        return False, "needs Apple Silicon"
    ver = platform.mac_ver()[0] or "0"
    try:
        major = int(ver.split(".")[0])
    except ValueError:
        return False, "could not read the macOS version"
    if major < ANE_MIN_MACOS:
        return False, f"needs macOS {ANE_MIN_MACOS} or newer, this is {ver}"
    return True, ""


def _device() -> tuple[str, str]:
    """(device, compute type). Uses a GPU that is ALREADY here; never asks for one.

    WE DO NOT BUNDLE CUDA, and that is deliberate rather than lazy. cuDNN and cuBLAS are 2-3 GB
    of wheels against a 58 MB binary; PyInstaller and ctypes-loaded native libraries are already
    a scar in this repo; and macOS — the primary target — has no CUDA at all, so it would help
    neither the build we ship nor the person we ship it to. Transcription is queued and
    post-call besides: `medium` does a five-minute call in about 100 seconds on a CPU while
    nothing waits for it.
    
    But refusing a GPU someone already has is just as wrong, and detecting one costs a single
    call. On a machine with CUDA properly installed this is roughly an order of magnitude
    faster; everywhere else it is exactly what it was.
    """
    if forced := os.getenv("SECRETARY_STT_DEVICE"):
        return forced, os.getenv("SECRETARY_STT_COMPUTE", "default")
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda", os.getenv("SECRETARY_STT_COMPUTE", "float16")
    except Exception:
        pass                        # no ctranslate2, no driver, no GPU — all mean CPU
    return "cpu", os.getenv("SECRETARY_STT_COMPUTE", "int8")


def _load(name: str):
    """Load the model, falling back to CPU if the GPU path will not start.

    A detected GPU is not a working one: the driver can be too old, the CUDA libraries absent,
    the card busy. That failure arrives at model load, and without this it would surface as a
    transcription failure on a real recording — three retries later the file is written off for
    a reason that has nothing to do with it.
    """
    from faster_whisper import WhisperModel
    device, compute = _device()
    try:
        m = WhisperModel(name, device=device, compute_type=compute)
        logger.info("speech model %s loaded on %s (%s)", name, device, compute)
        return m
    except Exception as exc:
        if device == "cpu":
            raise
        logger.warning("%s would not load on %s (%s: %s) — falling back to the CPU",
                       name, device, type(exc).__name__, exc)
        return WhisperModel(name, device="cpu", compute_type="int8")




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
        _local_model = _load(want)
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
            # RETRY BEFORE GIVING UP. This marked `.failed` on the first exception, which is
            # right for a corrupt file and wrong for everything else — and the commonest failure
            # here is not the file at all, it is the local model DOWNLOADING on first use. That
            # is up to 2.9 GB fetched inside this call, so a dropped connection, a closed laptop
            # or a metered link would permanently lose a recording's transcript to a blip.
            #
            # Counting attempts on disk keeps the queue derived from the filesystem — no state
            # to get out of step with the recordings — and a genuinely unreadable file still
            # stops after MAX_ATTEMPTS instead of being retried every poll forever.
            tries = wav.with_suffix(".try")
            n = int(tries.read_text().strip() or 0) + 1 if tries.exists() else 1
            if n >= MAX_ATTEMPTS:
                logger.error("could not transcribe %s after %d attempts (%s: %s)",
                             wav.name, n, type(exc).__name__, exc)
                wav.with_suffix(".failed").write_text(f"{type(exc).__name__}: {exc}\n")
                tries.unlink(missing_ok=True)
            else:
                logger.warning("could not transcribe %s (%s: %s) — attempt %d of %d, will retry",
                               wav.name, type(exc).__name__, exc, n, MAX_ATTEMPTS)
                tries.write_text(str(n))
            continue
        if not text:
            logger.info("%s had no speech in it", wav.name)
            wav.with_suffix(".txt").write_text("")
            continue
        wav.with_suffix(".try").unlink(missing_ok=True)
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
