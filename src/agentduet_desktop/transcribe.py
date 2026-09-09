"""Turn recorded call legs into text, on a queue, with or without a network.

ONE ENGINE: faster-whisper, on this machine. No key, no network, and the audio does not leave.

IT USED TO PREFER A HOSTED ONE — `qwen3-asr-flash` on DashScope — whenever a credential existed,
and it was measurably better on the same audio: it returned "Sir, ma'am … Trusty's Security
Department" where local `base` gave "Sarah, ma'am … trustee security department". That path was
removed on 2026-08-27, and the reason is worth keeping because the accuracy argument for putting
it back will come round again.

THE CREDENTIAL IT KEYED OFF WAS THE LLM's. `_hosted_key()` was literally
`llm._DashScope.credential()`, so attaching a Qwen key to summarise transcripts silently
started uploading the CALL AUDIO to Alibaba. Nobody would predict that from either setting, and
it happened on this machine: a local GLM running the assistant while every recording went to the
cloud, on a key left over from an unrelated test.

THE THREE JOBS ARE SEPARATE, and conflating the last two is what produced that. Two humans talk
and we record them; speech-to-text turns that into words; a language model may LATER read the
words. Only the third needs a provider. Speech-to-text was never an LLM job — `qwen3-asr-flash`
is a dedicated ASR task that refuses a text part alongside the audio, which is how that was
discovered.

So the strongest thing this product says — that a recording of two people talking stays on the
owner's machine — is now true without a clause. If a hosted engine returns it needs its own
explicit setting and its own credential, never one inferred from the model key.

THE QUEUE IS THE FILESYSTEM. A `.wav` with no sibling `.txt` is work to do. There is no queue
file to corrupt, lose or get out of step with the recordings, and it is restart-safe by
construction: a daemon that dies mid-transcription finds the same job waiting when it comes
back. Re-running one means deleting its `.txt`. A permanent failure writes `.failed` beside the
audio so it stops being retried; deleting that re-queues it.

Slow is fine here. This runs after the call, off the event loop, and the audio — the part that
cannot be recreated — is already closed on disk before any of it starts.
"""

import asyncio
import io
import logging
import functools
import os
import shutil
import subprocess
import sys
import pathlib
import wave

logger = logging.getLogger("secretary")


#: How hard the local engine tries. Measured on a real 22s call, against the (then) hosted
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
#: The models offered, smallest first. NO `tiny` OR `base`: they are fast and not accurate
#: enough for a phone call, which is the only audio this product transcribes. Offering a model
#: whose output would not be worth reading is not a choice, it is a trap with a small number
#: next to it. They remain resolvable by name for anyone who sets one deliberately.
#:
#: WHISPER'S OWN NAMES, not adjectives of ours: "balanced"
#: and "Whisper small" were the same thing under two names in one card, and an owner who reads
#: anything about Whisper elsewhere meets these names, not ours.
#:
#: `large-v3-turbo` shares large-v3's encoder with a decoder cut from 32 layers to 4. Measured
#: on a clean 88s call it was 20.7s -> 11.2s for the same audio, which is why it is here and why
#: it sits between medium and large-v3 rather than at the top.
#:
#: NO `distil-*`. They are faster again and ENGLISH ONLY, and this product's own language list
#: offers Vietnamese, Chinese, Malay and Thai. A model that silently cannot do most of the
#: languages on the next control is not a tier.
TIERS = ["small", "medium", "large-v3-turbo", "large-v3"]

#: What a fresh install gets, and what an unreadable value falls back to. ONE CONSTANT, because
#: it was three literals and they are the kind that drift apart.
#:
#: `large-v3-turbo` rather than `small` from 2026-08-27. It shares large-v3's encoder with a
#: decoder cut from 32 layers to 4, so it is close to the most accurate model at roughly half
#: its time — measured on a clean 88s call at 20.7s -> 11.2s. The cost is the download: 1.6 GB
#: against small's 464 MB, on a queue where nothing waits for the result.
#:
#: A TYPO FALLS BACK HERE TOO, which means an unreadable value can start a 1.6 GB fetch. That is
#: deliberate: the alternative is a fresh install and a mistyped one quietly running different
#: models, and the row marked "in use" says which is running either way.
DEFAULT_MODEL = "large-v3-turbo"

#: What the four adjectives used to mean. Kept so an instance configured before 2026-08-27 keeps
#: the model it chose instead of silently jumping tier on upgrade.
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
    chosen = (os.getenv("SECRETARY_STT_QUALITY") or owner.transcription_quality()
              or DEFAULT_MODEL).lower()
    # ANY MODEL FASTER-WHISPER KNOWS, not just the ones we offer. TIERS is a curated list, not
    # a whitelist: someone who deliberately sets `tiny`, `large-v2` or a `.en` variant should get
    # it. Narrowing this to TIERS silently moved such an instance to the default on upgrade,
    # which is the failure the legacy-name mapping below exists to prevent.
    #
    # A legacy adjective is translated; anything faster-whisper does not know falls back rather
    # than raising, because a settings typo must not stop a call being transcribed.
    if chosen in QUALITY:
        return QUALITY[chosen]
    return chosen if _repo(chosen) else DEFAULT_MODEL

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




def _local_available() -> bool:
    # find_spec, not a try/import: importing faster_whisper pulls in a CPU inference runtime and
    # costs a second or more, and this is called from `status` and from every queue poll.
    import importlib.util
    return importlib.util.find_spec("faster_whisper") is not None


# ---- Apple's on-device engine (macOS 26+, Apple Silicon) -----------------------------------
#
# MEASURED BEFORE IT WAS ADOPTED, on an M5 against faster-whisper large-v3-turbo, on a real
# 222-second call from the bank sample:
#
#     Whisper   21.5s wall   88.5s CPU   729 chars
#     Apple      1.1s wall    0.06s CPU   617 chars
#
# Nineteen times faster and roughly fifteen hundred times less CPU for comparable output, and it
# formats what a transcript needs — spoken digits become 91234567, a spoken domain becomes
# b3networks.com. On a laptop transcribing all day the CPU figure is the one that matters.
#
# IT IS NOT A REPLACEMENT, and the reason is language, not the OS floor. Thirty locales, none of
# them Malay, Vietnamese, Tamil, Thai, Indonesian or Hindi — while the Language setting promises
# exactly those. Worse, it has no language detection: told the wrong language it returns fluent
# nonsense rather than an error. Verified on a Vietnamese call in the same sample, where Whisper
# produced a coherent transcript including the caller's name and this produced "wife guy, 18
# charge book". So the language decides the engine, and Whisper keeps everything Apple cannot
# serve.

#: The bundled Swift helper. `docs/experiments` has the throwaway it grew from.
APPLE_HELPER = "agentduet-stt"

#: What `## Transcription` holds to mean "use Apple's engine". `_apple_choice` accepts
#: several spellings; this is the one the UI writes, so the card and the setting agree.
APPLE = "apple"


def _apple_bin() -> pathlib.Path | None:
    """The helper, or None. Beside the daemon in Contents/MacOS, or on PATH in a dev checkout."""
    if sys.platform != "darwin":
        return None
    override = os.getenv("SECRETARY_STT_APPLE_BIN")
    if override:
        p = pathlib.Path(override)
        return p if p.is_file() and os.access(p, os.X_OK) else None
    # BOTH the resolved and unresolved interpreter directory. In a frozen bundle sys.executable
    # IS the binary, so resolving is right; in a venv it is a SYMLINK to the real interpreter,
    # so resolving walks out of the venv entirely and the helper sitting in its bin/ is missed.
    exe = pathlib.Path(sys.executable)
    roots = {exe.parent, exe.resolve().parent}
    cands = [r / APPLE_HELPER for r in roots] + [r.parent / "MacOS" / APPLE_HELPER for r in roots]
    for cand in cands:
        if cand.is_file() and os.access(cand, os.X_OK):
            return cand
    found = shutil.which(APPLE_HELPER)
    return pathlib.Path(found) if found else None


@functools.lru_cache(maxsize=1)
def apple_locales() -> tuple[str, ...]:
    """The locales Apple has INSTALLED, cached for the process.

    Installed rather than supported: thirty are supported but only the ones already on the
    machine work without an asset download, and a transcription job is the wrong moment to
    start fetching a language model behind the owner's back.
    """
    b = _apple_bin()
    if b is None:
        return ()
    try:
        out = subprocess.run([str(b), "--locales"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ()
    if out.returncode != 0:
        return ()
    return tuple(line.strip() for line in out.stdout.splitlines() if line.strip())


def apple_supports(lang: str | None) -> bool:
    """Whether Apple has an installed locale for this language. Empty means English."""
    want = (lang or "en").split("-")[0].strip().lower()
    if not want:
        return False
    return any(loc.lower() == want or loc.lower().startswith(want + "-")
               for loc in apple_locales())


def _configured_language() -> str | None:
    from . import owner
    return os.getenv("SECRETARY_STT_LANGUAGE") or owner.language() or None


def apple_ready() -> tuple[bool, str]:
    """(usable now, why not) — for `status` and the settings page, not for control flow."""
    if sys.platform != "darwin":
        return False, "macOS only"
    ok, why = ane_support()
    if not ok:
        return False, why
    if _apple_bin() is None:
        return False, "the agentduet-stt helper is not in this build"
    if not apple_locales():
        return False, "no speech locales are installed"
    lang = _configured_language()
    if not apple_supports(lang):
        return False, (f"no installed locale for '{lang or 'en'}' — "
                       f"has {', '.join(apple_locales()) or 'none'}")
    return True, ""


def _apple_choice() -> str:
    """What the `## Transcription` setting says about the engine: apple, whisper, or "".

    A Whisper model name means Whisper, because that is what choosing one MEANS — an owner who
    typed large-v3 did not ask for a different engine. Empty means "best available here", which
    is how macOS gets Apple by default without the template naming a platform.
    """
    from . import owner
    value = (os.getenv("SECRETARY_STT_MODEL") or os.getenv("SECRETARY_STT_QUALITY")
             or owner.transcription_quality() or "").strip().lower()
    if value in ("apple", "ane", "on-device", "system"):
        return "apple"
    if value:
        return "whisper"
    return ""


#: APPLE IS QUARANTINED, 2026-09-09 — Stanley's call, to keep this stage simple.
#:
#: Nothing is deleted: `_apple`, `apple_ready`, the bundled `agentduet-stt` helper and the
#: measurements that justify it all stand, and clearing this flag brings it back. What it buys
#: is ONE engine while the recording pipeline settles, and specifically EXACT turn order —
#: faster-whisper returns `.start`, `.end` and word-level `words` per segment, so interleaving
#: two legs is arithmetic. Apple's helper prints bare text, and everything approximate in this
#: file (the mono downmix, the difflib alignment, the confidence floor, the sentence snapping)
#: exists only to work around that.
#:
#: KNOW WHAT IT COSTS, because it is not small and there is no GPU to soften it. Measured on a
#: 222-second call: Whisper large-v3-turbo 21.5s wall and 88.5s CPU, against Apple's 1.1s and
#: 0.06s. CTranslate2 — the runtime under faster-whisper — is compiled with CPU and CUDA
#: backends only and has no Metal path, so on Apple Silicon this is CPU-only whatever the
#: settings say: `get_cuda_device_count()` is 0 here and asking it for CUDA compute types
#: raises "not compiled with CUDA support". The LLM does use the GPU on a Mac (llama.cpp, via
#: `models._gpu_layers`), which is why the two look like they should behave the same and do not.
#: It also costs a 1.6 GB+ model download on a fresh install, and en-SG accuracy: the 29-call
#: sweep found one outright language misdetection and about eight more scoring under 0.6.
#:
#: So this is a SIMPLIFICATION WITH A PRICE, taken deliberately and reversible in one line.
APPLE_QUARANTINED = True


def engine() -> str:
    """`apple`, `local`, or `` when nothing here can transcribe.

    A second one DOES exist now, which is why this was never collapsed to a boolean. The order
    is deliberate: Apple's engine wins when it is usable AND has the configured language, since
    it is nineteen times faster for a fifteen-hundredth of the CPU. Whisper takes everything
    else — every language Apple lacks, every older Mac, Linux and Windows.

    An explicit Whisper model in `## Transcription` is respected: choosing large-v3 is choosing
    Whisper, not asking for a faster engine that ignores the choice.
    """
    choice = _apple_choice()
    if APPLE_QUARANTINED:
        # EVEN IF THE SETTING ASKS FOR IT. A quarantine that an owner can step around by
        # typing "apple" is not one, and the point of this stage is that exactly one engine
        # runs. `describe()` says so on screen rather than silently ignoring the setting.
        return "local" if _local_available() else ""
    if choice != "whisper" and apple_ready()[0]:
        return "apple"
    if choice == "apple" and not _local_available():
        return ""            # asked for Apple, cannot have it, and no fallback exists
    return "local" if _local_available() else ""


def available() -> tuple[bool, str]:
    """(can transcribe, why not). Checked at start-up so the owner learns before a call."""
    if engine() in ("local", "apple"):
        return True, ""
    return False, ("the speech engine is not in this build — recordings are kept, but not "
                   "transcribed. `pip install 'agentduet-desktop[stt]'` adds it; it needs no "
                   "key and no network.")


def describe() -> str:
    """One line for `status`, naming which engine would ACTUALLY run.

    It names the engine and not the setting, because those diverge on purpose: an owner whose
    language Apple cannot serve has left the setting empty and is nonetheless getting Whisper.
    Reporting the setting would tell them what they asked for; this tells them what happens.
    """
    which = engine()
    if which == "apple":
        locales = ", ".join(apple_locales()[:3])
        lang = _configured_language() or "en"
        return f"Apple on-device ({lang}, on this machine)" + (f" — installed: {locales}…" if locales else "")
    if which == "local":
        why = ""
        # SAY THE QUARANTINE OUT LOUD. On a Mac that can run Apple's engine, Whisper appearing
        # here with no reason reads as a broken setting — and the owner would go looking in
        # settings.md, where the answer is not.
        if APPLE_QUARANTINED and sys.platform == "darwin" and apple_ready()[0]:
            return (f"Whisper {local_model()} on this machine — Apple's on-device engine is "
                    f"held back for now, so every language uses Whisper")
        # SAY WHY WHISPER. Two different reasons, and both leave a Mac owner staring at Whisper
        # with nowhere to look: either Apple's engine cannot run here, or it can and their
        # settings.md still holds the model name seeded before Apple existed — which every
        # instance created before 2026-09-03 does, because templates seed once.
        if sys.platform == "darwin":
            ok, reason = apple_ready()
            if _apple_choice() == "whisper" and ok:
                why = " — Apple's on-device engine is available here; clear `## Transcription` in settings.md to use it"
            elif _apple_choice() != "whisper" and not ok and reason:
                why = f" — Apple's engine unavailable: {reason}"
        return f"local ({local_model()}, on this machine){why}"
    return "OFF — " + available()[1]


#: Roughly what each tier costs to fetch, for telling the owner BEFORE it happens rather than
#: after. Measured from the cache on disk, not from the docs.
MODEL_MB = {"tiny": 75, "base": 142, "small": 464, "medium": 1500,
            "large-v3-turbo": 1600, "large-v3": 2900}


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






_local_model = None
_loaded_name = ""


#: The macOS release that first shipped SpeechAnalyzer/SpeechTranscriber, the long-form API.
#: The older SFSpeechRecognizer exists further back but was built for dictation and caps a
#: request at about a minute, which is useless for a call.
ANE_MIN_MACOS = 26

#: Generous: a long call on a busy machine, and the helper is 19x faster than Whisper anyway.
APPLE_TIMEOUT = 900


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


def _apple(path: pathlib.Path) -> str:
    """Apple's engine, via the bundled helper. Raises on any failure so the caller can fall back."""
    b = _apple_bin()
    if b is None:
        raise TranscriptionUnavailable("the agentduet-stt helper is not in this build")
    lang = _configured_language() or "en"
    try:
        out = subprocess.run([str(b), str(path), lang],
                             capture_output=True, text=True, timeout=APPLE_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        raise TranscriptionUnavailable(f"agentduet-stt did not run: {exc}") from exc
    if out.returncode != 0:
        raise TranscriptionUnavailable(
            f"agentduet-stt failed ({out.returncode}): {(out.stderr or '').strip()[:200]}")
    if (out.stderr or "").strip():
        logger.info("%s: %s", path.name, out.stderr.strip().splitlines()[-1])
    return out.stdout.strip()


def transcribe(path: pathlib.Path) -> str:
    """The words on one recorded leg. Raises TranscriptionUnavailable; never returns a guess."""
    which = engine()
    if which == "apple":
        try:
            return _apple(path)
        except TranscriptionUnavailable as exc:
            # FALL BACK RATHER THAN LOSE THE RECORDING. The helper can fail for reasons that
            # have nothing to do with this file — a locale uninstalled since start-up, a helper
            # missing from a hand-assembled bundle — and Whisper is right here.
            if _local_available():
                logger.warning("%s: Apple's engine failed (%s) — using Whisper", path.name, exc)
                return _local(path)
            raise
    if which != "local":
        raise TranscriptionUnavailable(available()[1])
    return _local(path)


# ---- the queue -------------------------------------------------------------------------

def pending() -> list[pathlib.Path]:
    """Recordings still needing a transcript, oldest first.

    Derived, not stored. Skips the two cases that are not work: a WAV that is only a header
    (an unbridged call produces exactly that), and one already marked `.failed`.
    """
    from . import carry
    # THE LEGS, not the owner's folder. The per-leg audio is the work; the merged file in the
    # owner's folder is the product, and globbing the product would re-transcribe it forever.
    if not carry.legs().is_dir():
        return []
    out = []
    for wav in sorted(carry.legs().glob("*.wav")):
        if wav.with_suffix(".txt").exists() or wav.with_suffix(".failed").exists():
            continue
        if wav.stat().st_size <= EMPTY_WAV_BYTES:
            continue
        out.append(wav)
    return out


def _record(wav: pathlib.Path, text: str) -> None:
    """File the transcript beside its audio, which is where the recorder reads it.

    IT ALSO WROTE TO `brain.record`, and that was wrong twice over. `calls.py` opens by arguing
    the case — a query log wants asker, question, outcome, answer, and a carried call has no
    question, so filing one there "would mean inventing a question to satisfy a schema". The row
    written was `[carried call, caller]`: exactly that invented question, on every call. Nothing
    read those rows; the hub, the assistant's tools and the settings page all read `calls.jsonl`
    and the `.txt` beside the audio. Removed 2026-08-27, with tests/test_boundary.py to keep it
    removed.
    """
    wav.with_suffix(".txt").write_text(text + "\n")


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
        # MERGE AFTER TRANSCRIBING, in its own try: a merge that raises must not stop the next
        # poll from transcribing, and a transcription failure must not stop a call whose legs
        # are already settled from being merged. They are separate jobs on one queue.
        try:
            await asyncio.to_thread(merge_once)
        except Exception as exc:
            logger.error("the merge step hit %s: %s", type(exc).__name__, exc)


# ---- what is on disk -------------------------------------------------------------------
#
# THE MODELS WERE INVISIBLE. The page offered four tiers by adjective and said "ready" when the
# chosen one happened to be present — so a machine could be holding every tier at once (6.7 GB
# was found on the developer's own, from an evaluation weeks earlier) with nothing in the UI
# saying so and no way to remove any of it. A model that downloads itself silently must be
# removable in the same place.

def _repo(model: str) -> str:
    """The Hugging Face repo behind a model name, asked of FASTER-WHISPER rather than kept here.

    A hand-written copy of this map drifts the moment the library adds a model — and it already
    had: `large-v3-turbo` was sitting in the cache on this machine under a repo the local map
    did not know.
    """
    try:
        from faster_whisper.utils import _MODELS
        return _MODELS.get(model, "")
    except Exception:
        return ""


def model_dir(model: str) -> pathlib.Path | None:
    """The cache directory holding this model, or None when it is not downloaded."""
    repo = _repo(model)
    if not repo:
        return None
    root = pathlib.Path(os.getenv("HF_HOME") or (pathlib.Path.home() / ".cache/huggingface"))
    d = root / "hub" / ("models--" + repo.replace("/", "--"))
    return d if d.is_dir() else None


def size_on_disk(model: str) -> int:
    """Megabytes this model actually occupies, or 0 when absent. Measured, not from the table."""
    d = model_dir(model)
    if not d:
        return 0
    # NOT SYMLINKS. The hub cache keeps one copy under blobs/ and links to it from
    # snapshots/, so following both counts every byte twice — it reported 927 MB for a model
    # `du` puts at 464.
    return int(sum(f.stat().st_size for f in d.rglob("*")
                   if f.is_file() and not f.is_symlink()) / 1024 / 1024)


def delete_model(model: str) -> str:
    """Remove a downloaded model. Refuses the one in use."""
    if model == local_model():
        return f"{model} is the model in use. Choose another quality first."
    d = model_dir(model)
    if not d:
        return f"{model} is not downloaded."
    freed = size_on_disk(model)
    import shutil
    shutil.rmtree(d, ignore_errors=True)
    return f"Deleted Whisper {model}, freeing {freed} MB."


def catalogue() -> list[dict]:
    """The four tiers, in order, with what each costs and whether it is here.

    Ordered by size rather than by the tier names, because the ONLY thing an owner is trading
    between them is accuracy against disk and time — and an ordered list shows that where four
    adjectives do not.
    """
    current = local_model()
    running = engine()
    out = []

    # APPLE IS AN ENGINE THE OWNER CAN CHOOSE, so it belongs in the list they choose from. It was
    # absent, and the consequence was worse than a missing option: the Whisper tier named by
    # `local_model()` was marked "in use" whenever it was the fallback, so this card told the
    # owner large-v3-turbo was transcribing their calls while `status` said "Apple on-device" and
    # every log line said `apple`. Two surfaces disagreeing about the same fact.
    #
    # Nothing to download and nothing to delete — it is part of macOS — so the row is `downloaded`
    # and the page suppresses Delete for it. Choosing it clears `## Transcription` to `apple`;
    # choosing a Whisper tier names that model, which IS choosing Whisper.
    ready, why = apple_ready()
    # WHILE QUARANTINED IT IS NOT OFFERED. A row that can be chosen and then does nothing is
    # the failure this file keeps finding — the click lands, the setting is written, and the
    # engine ignores it. Withheld from the list rather than shown as disabled, because the
    # dropdown is a choice and this is not currently one.
    if APPLE_QUARANTINED:
        ready = False
    if ready or running == "apple":
        out.append({"model": APPLE, "name": "Apple on-device",
                    "mb": 0, "got_mb": 0, "downloaded": True,
                    "in_use": running == "apple", "builtin": True, "why": why})

    for model in (TIERS if current in TIERS else [current] + TIERS):
        # `is_cached`, NOT "a directory exists". The directory appears the instant a download
        # STARTS, so the row claimed a 1.5 GB model was downloaded when 66 MB of it had
        # arrived — offering Use this and Delete for weights that were still coming down, and
        # making a fetch that had barely begun look instantaneous.
        done = is_cached(model)
        on_disk = size_on_disk(model)
        out.append({"model": model, "name": model,
                    "mb": on_disk if done else MODEL_MB.get(model, 0),
                    # What has landed so far, so a partial fetch can show how far along it is
                    # instead of looking like nothing or like everything.
                    "got_mb": on_disk,
                    # IN USE MEANS RUNNING. `model == current` alone marked the Whisper
                    # fallback as in use while Apple was the engine — see the note above.
                    "downloaded": done,
                    "in_use": running == "local" and model == current})
    return out


# ---- merging a call into one file the owner keeps -----------------------------------------
#
# The legs exist because keeping the parties apart is what lets a transcript say who spoke
# without diarisation. What the owner asked for is ONE file per call, so the pair is merged
# after the fact — here, on the same queue as transcription, where nothing is waiting. It is
# deliberately not done in `carry._record_leg`: that runs while two people are talking, and
# "a failure here must not kill the call".
#
# STEREO, ONE PARTY PER CHANNEL — not a sum. Summing is what the old comment in carry.py warns
# about: the legs are not sample-aligned, so adding them puts one voice ahead of the other and
# compresses both. Two channels keep every sample of each party exactly, stay separable for a
# future re-transcription, and open in any player as one recording.
MERGE_SUFFIX = ".merged"


def _leg_start(wav: pathlib.Path) -> float | None:
    """When this leg's first frame arrived, from the sidecar `carry` wrote."""
    try:
        return float(wav.with_suffix(".start").read_text().strip())
    except (OSError, ValueError):
        return None


def merge_ready() -> list[str]:
    """Stems whose legs are all transcribed and which have not been merged yet."""
    from . import carry
    if not carry.legs().is_dir():
        return []
    by_stem: dict[str, list[pathlib.Path]] = {}
    for wav in sorted(carry.legs().glob("*.wav")):
        by_stem.setdefault(carry.stem_of(wav.name), []).append(wav)
    out = []
    for stem, wavs in by_stem.items():
        if (carry.legs() / f"{stem}{MERGE_SUFFIX}").exists():
            continue
        # EVERY leg settled, one way or the other. A leg still queued for transcription would
        # otherwise be merged without its words and never revisited, because the merge marker
        # is what stops this looking again.
        if not all(w.with_suffix(".txt").exists() or w.with_suffix(".failed").exists()
                   or w.stat().st_size <= EMPTY_WAV_BYTES for w in wavs):
            continue
        out.append(stem)
    return out


def _merge_audio(stem: str, wavs: list[pathlib.Path]) -> bool:
    """Write one stereo WAV: caller left, callee right, aligned by their start sidecars."""
    from . import carry
    sides: dict[str, pathlib.Path] = {}
    for w in wavs:
        for leg in ("caller", "callee"):
            if w.stem.endswith("-" + leg):
                sides[leg] = w
    if not sides:
        return False
    frames: dict[str, bytes] = {}
    rate = carry.SAMPLE_RATE
    for leg, w in sides.items():
        try:
            with wave.open(str(w), "rb") as r:
                rate = r.getframerate() or rate
                frames[leg] = r.readframes(r.getnframes())
        except (OSError, wave.Error) as exc:
            logger.warning("merge %s: cannot read the %s leg (%s)", stem, leg, exc)
            return False
    # PAD THE LATE ONE WITH SILENCE, by the gap between the two first frames. Without this,
    # sample zero of each file is treated as the same instant, and the far leg — originated
    # toward the PBX, which may ring for seconds — arrives shifted by however long that took.
    starts = {leg: _leg_start(w) for leg, w in sides.items()}
    if len(starts) == 2 and all(v is not None for v in starts.values()):
        late = max(starts, key=lambda k: starts[k])
        gap = starts[late] - min(starts.values())
        pad = int(gap * rate) * carry.SAMPLE_WIDTH
        if pad:
            frames[late] = b"\x00" * pad + frames[late]
            logger.info("merge %s: padded the %s leg by %.2fs", stem, late, gap)
    width = carry.SAMPLE_WIDTH
    n = max((len(b) // width for b in frames.values()), default=0)
    if not n:
        return False
    left = frames.get("caller", b"").ljust(n * width, b"\x00")
    right = frames.get("callee", b"").ljust(n * width, b"\x00")
    out = carry.merged_wav(stem)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(out), "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(width)
            w.setframerate(rate)
            # Interleave the two channels: L,R,L,R… CALLER IS LEFT and callee right, always,
            # so a listener can tell the parties apart by ear and a splitter by index.
            w.writeframes(b"".join(left[i:i + width] + right[i:i + width]
                                   for i in range(0, n * width, width)))
    except (OSError, wave.Error) as exc:
        logger.warning("merge %s: could not write %s (%s)", stem, out.name, exc)
        return False
    return True


#: Shortest run of words that counts as attribution. A single word matches by coincidence —
#: "yes", "the", a name — and one wrong word starts a turn in the wrong mouth.
MIN_RUN = 2
#: How much of the mixed transcript must be attributed before the order is worth claiming.
#: Below this the reconstruction is mostly holes, and a mostly-holed order is a guess.
MIN_ATTRIBUTED = 0.6


def _runs(mixed: list[str],
          legs_words: dict[str, list[str]]) -> list[tuple[int, int, int, str]]:
    """Which stretches of the MIXED transcript came from which leg.

    `difflib` between the mixed word list and one leg's own word list: that leg's words appear
    in the mixed audio in the same order, so its matching blocks are where it was speaking.
    Runs shorter than MIN_RUN are dropped as coincidence, and where two legs claim the same
    stretch the longer match wins — the shorter one is the other party's words leaking into a
    channel, which is exactly what mixing does.
    """
    import difflib

    blocks: list[tuple[int, int, int, str]] = []
    for leg, words in legs_words.items():
        if not words:
            continue
        sm = difflib.SequenceMatcher(a=mixed, b=words, autojunk=False)
        for m in sm.get_matching_blocks():
            if m.size >= MIN_RUN:
                # The leg offset is carried too, because the WORDS come from the leg and only
                # the ORDER comes from the mix — see `_ordered`.
                blocks.append((m.a, m.a + m.size, m.b + m.size, leg))
    blocks.sort(key=lambda b: (b[0], -(b[1] - b[0])))
    kept: list[tuple[int, int, int, str]] = []
    for start, end, upto, leg in blocks:
        if kept and start < kept[-1][1]:
            # TRIM THE OVERLAP, DO NOT DISCARD THE RUN. Discarding cost a whole turn on the
            # first real call: the caller's block absorbed one shared word ("The", which both
            # parties said) at the exact index where the callee's four-word run began, so a
            # ONE-WORD overlap deleted the callee from the transcript entirely and the ordering
            # then reported that party as absent from the mix. Common words are shared by
            # definition in a conversation, so an overlap is the normal case and not a conflict.
            start = kept[-1][1]
            if end - start < MIN_RUN:
                continue
        kept.append((start, end, upto, leg))
    return kept


#: How far past a match a boundary may be pushed to finish the sentence. Unbounded, a single
#: match could swallow a party's entire remaining transcript and destroy the order it was
#: meant to establish.
SNAP_WORDS = 12


def _sentence_end(words: list[str], upto: int) -> int:
    """`upto`, extended to the end of the sentence it falls inside."""
    if upto <= 0 or upto >= len(words):
        return upto
    if words[upto - 1].endswith((".", "?", "!")):
        return upto
    for i in range(upto, min(len(words), upto + SNAP_WORDS)):
        if words[i].endswith((".", "?", "!")):
            return i + 1
    return upto


def _ordered(mixed_text: str, leg_texts: dict[str, str]) -> tuple[list[tuple[str, str]], float]:
    """Turns in speaking order, and the fraction of the mixed transcript that was attributed.

    APPROXIMATE BY CONSTRUCTION, and the caller must say so. Speech recognition on mixed audio
    does not produce the same words as on an isolated leg — overlapping talk degrades it and
    can fuse two speakers into one utterance — so this is text alignment, not timing. What it
    buys is the one thing the isolated legs cannot give: who spoke first.
    """
    mixed = mixed_text.split()
    if not mixed:
        return [], 0.0
    words = {leg: t.split() for leg, t in leg_texts.items()}
    kept = _runs(mixed, words)
    if not kept:
        return [], 0.0
    # EVERY PARTY MUST APPEAR IN THE MIX, or "100% attributed" is a lie. On the first real
    # call this returned 1.0 with one speaker missing entirely: the mix held only the caller,
    # so every word of it was placed — and the callee's turn appeared solely because unmatched
    # leg words are appended at the end. The share measures how much of the MIX was placed, so
    # a one-sided mix scores perfectly while ordering nothing. Both must be seen.
    spoke = {leg for _, _, _, leg in kept}
    if any(leg not in spoke for leg in words):
        logger.info("ordering: %s did not appear in the mix at all — no order to infer",
                    ", ".join(sorted(set(words) - spoke)))
        return [], 0.0
    turns: list[tuple[str, str]] = []
    covered = 0
    # THE WORDS COME FROM THE LEG, NOT FROM THE MIX. Emitting the matched span of the mixed
    # transcript would publish the WORSE transcription of every turn — mixed audio is exactly
    # where recognition degrades, and the isolated legs are the accurate copy. So each run
    # consumes that leg's own words up to the end of the match, which also keeps the words the
    # mix dropped: they were said, they are in the leg, and losing them to gain an order would
    # be a bad trade.
    taken = {leg: 0 for leg in words}
    for start, end, upto, leg in kept:
        covered += end - start
        # SNAP THE SEAM TO A SENTENCE. Word-level alignment cuts wherever the match happened to
        # end, and on the first real call that orphaned a lone "The" at the close of one turn
        # and started the next with "cat jump over the fox." The order was right and the
        # sentences were wrecked, which reads worse than not ordering at all. A speech turn is
        # sentence-shaped, so the boundary is extended to the end of the sentence it lands in.
        upto = _sentence_end(words[leg], upto)
        if upto <= taken[leg]:
            continue                          # already emitted by an earlier run's snap
        said = " ".join(words[leg][taken[leg]:upto]).strip()
        taken[leg] = upto
        if not said:
            continue
        # MERGE A CONSECUTIVE RUN BY THE SAME PARTY. Alignment breaks one turn into several
        # blocks wherever the mixed transcript dropped a word, and three bubbles from the same
        # speaker in a row is a worse reading of the call than one.
        if turns and turns[-1][0] == leg:
            turns[-1] = (leg, turns[-1][1] + " " + said)
        else:
            turns.append((leg, said))
    # ANYTHING LEFT AT THE END still belongs to its speaker. A leg whose last sentence never
    # made it into the mix would otherwise vanish from the call record entirely.
    for leg, n in taken.items():
        tail = " ".join(words[leg][n:]).strip()
        if not tail:
            continue
        if turns and turns[-1][0] == leg:
            turns[-1] = (leg, turns[-1][1] + " " + tail)
        else:
            turns.append((leg, tail))
    return turns, covered / len(mixed)


def _mono_for_ordering(stem: str) -> pathlib.Path | None:
    """A throwaway MONO downmix of the merged call, for reconstructing turn order only.

    THE STEREO FILE CANNOT BE USED FOR THIS, and the first real call proved it: Apple's engine
    reads only channel one, so the mix it transcribed contained the caller and not one word of
    the callee. Checked directly — the right channel transcribes perfectly on its own, and a
    mono sum of the two returns both parties interleaved, which is the ordering signal.
    outside both the legs folder and the owner's, because `pending()` globs the legs for work
    and this is not work, and because nobody asked to keep a downmix.
    Averaged rather than summed so two loud parties cannot clip. Accuracy barely matters here:
    the words that reach the transcript come from the legs, and this file is only ever asked
    what order they came in.
    """
    import struct
    import tempfile

    from . import carry

    src = carry.merged_wav(stem)
    try:
        with wave.open(str(src), "rb") as f:
            if f.getnchannels() != 2:
                return None
            rate, n, width = f.getframerate(), f.getnframes(), f.getsampwidth()
            raw = f.readframes(n)
        got = struct.unpack("<%dh" % (len(raw) // 2), raw)
        out = pathlib.Path(tempfile.mkdtemp(prefix="ad-mix-")) / f"{stem}.wav"
        with wave.open(str(out), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(width)
            w.setframerate(rate)
            w.writeframes(b"".join(struct.pack("<h", (a + b) // 2)
                                   for a, b in zip(got[0::2], got[1::2])))
        return out
    except (OSError, wave.Error, struct.error) as exc:
        logger.info("merge %s: could not build a mono mix for ordering (%s)", stem, exc)
        return None


def _merge_text(stem: str, wavs: list[pathlib.Path]) -> None:
    """One transcript per call, each turn labelled with who said it.

    IN SPEAKING ORDER WHEN THAT CAN BE RECONSTRUCTED, grouped by party when it cannot. The
    order comes from transcribing the MIXED audio as well: the legs know who spoke, the mix
    knows in what order, and aligning the two joins them.

    NOTHING IS WRITTEN INTO THE FILE ABOUT HOW IT WAS MADE. Two drafts carried a `#` header
    explaining that the order was reconstructed, or that it could not be — and that is a note
    about our machinery in the middle of the owner's transcript. Stanley's rule, restated
    2026-09-09: no hints, no debug lines, unless asked for. The distinction still exists and is
    still recorded — `logger.info` says what share was placed and when it fell back to
    grouping, which is where a note to ourselves belongs.

    The exact route stays open and is not this: per-utterance timings, which faster-whisper
    already returns and this package discards, and which Apple's helper does not print yet.
    """
    from . import carry
    labels = {"caller": "them", "callee": "you"}
    leg_texts: dict[str, str] = {}
    for leg in labels:
        hit = next((w for w in wavs if w.stem.endswith("-" + leg)), None)
        if hit is None:
            continue
        txt = hit.with_suffix(".txt")
        if txt.exists():
            try:
                leg_texts[leg] = txt.read_text().strip()
            except OSError:
                pass
    leg_texts = {k: v for k, v in leg_texts.items() if v}
    if not leg_texts:
        return

    parts: list[str] = []
    # ONLY WORTH TRYING WITH TWO PARTIES. One leg's words are already in order, so mixing adds
    # a transcription and can only lose accuracy.
    if len(leg_texts) > 1:
        mixed = ""
        mono = _mono_for_ordering(stem)
        if mono is not None:
            try:
                mixed = transcribe(mono)
            except Exception as exc:
                logger.info("merge %s: could not transcribe the mix for ordering (%s)",
                            stem, exc)
            finally:
                try:
                    mono.unlink()
                    mono.parent.rmdir()
                except OSError:
                    pass
        if mixed:
            turns, share = _ordered(mixed, leg_texts)
            if turns and share >= MIN_ATTRIBUTED:
                parts = [f"{labels[leg]}: {said}" for leg, said in turns]
            else:
                logger.info("merge %s: ordering placed only %.0f%% — grouping instead",
                            stem, share * 100)
    if not parts:
        parts = [f"{labels[leg]}: {leg_texts[leg]}" for leg in labels if leg in leg_texts]
    try:
        carry.merged_txt(stem).write_text("\n".join(parts) + "\n")
    except OSError as exc:
        logger.warning("merge %s: could not write the transcript (%s)", stem, exc)


def merge_once() -> int:
    """Merge every call whose legs are settled. Returns how many were written."""
    from . import carry
    done = 0
    for stem in merge_ready():
        wavs = sorted(w for w in carry.legs().glob("*.wav")
                      if carry.stem_of(w.name) == stem)
        if _merge_audio(stem, wavs):
            _merge_text(stem, wavs)
            done += 1
        # MARKED EITHER WAY. A call whose legs are all empty — an unbridged call, which is every
        # call until the platform hands us audio — has nothing to merge and must not be
        # reconsidered on every poll for the life of the instance.
        try:
            (carry.legs() / f"{stem}{MERGE_SUFFIX}").write_text("")
        except OSError as exc:
            logger.warning("merge %s: could not mark it done (%s)", stem, exc)
    return done
