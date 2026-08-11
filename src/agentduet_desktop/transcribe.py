"""Turn a recorded call leg into text.

POST-CALL, NOT LIVE. The call is already over when this runs, so nothing here is on a latency
path and a slow or failed transcription costs a text file, never a conversation. That is the
whole reason it is a separate module from `carry`: carrying must keep working when this does not.

WHY A DEDICATED ASR MODEL AND NOT THE CHAT MODEL. `qwen3-asr-flash` is a *task* model on
DashScope — it takes audio and returns what was said, and it REFUSES a text instruction sent
alongside (a prompt part earns a 400, which is how this was found). So there is no instruction to
get wrong, no way to ask it to summarise instead of transcribe, and nothing a caller can say that
becomes an instruction. On a path whose input is a stranger's voice, a model that cannot be
instructed is the right shape rather than a limitation.

IT REUSES THE OWNER'S EXISTING KEY. Same credential, host and endpoint as `llm._DashScope`, so
transcription needs no second signup — which matters because the model key is already one of the
two things onboarding cannot yet hand over automatically.
"""

import base64
import io
import logging
import os
import pathlib
import wave

logger = logging.getLogger("secretary")

#: The dedicated ASR task model. Not the chat model: see the header.
MODEL = os.getenv("SECRETARY_ASR_MODEL", "qwen3-asr-flash")

#: Audio is sent inline as base64, so one request carries about 1.4x the WAV's bytes. A minute of
#: 24 kHz mono 16-bit is ~2.8 MB, which is comfortable; a ten-minute call in one request would be
#: ~38 MB, which is not. Chunking makes the request size a function of this constant instead of
#: the call length, so a long call degrades into more requests rather than one that fails.
CHUNK_SECONDS = 60


class TranscriptionUnavailable(RuntimeError):
    """No credential, or the provider refused. Carrying a call must survive this."""


def available() -> tuple[bool, str]:
    """(can transcribe, why not). Checked before a call, so the owner learns at start-up."""
    from . import llm
    if not llm._DashScope.credential():
        return False, f"no {llm._DashScope.KEY} — recordings will be kept, but not transcribed"
    return True, ""


def _chunks(path: pathlib.Path, seconds: int = CHUNK_SECONDS):
    """Yield the WAV as a series of self-contained smaller WAVs.

    Each chunk carries its own header. Sending raw PCM slices would be smaller, but the provider
    is given a file and has to be told the rate and width somehow — and a header it can read is
    less brittle than parameters that must be kept in step with `carry`.
    """
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


def _one(audio: bytes, key: str) -> str:
    import httpx
    region = (os.getenv("DASHSCOPE_REGION") or "intl").strip().lower()
    host = "dashscope-intl.aliyuncs.com" if region == "intl" else "dashscope.aliyuncs.com"
    b64 = base64.b64encode(audio).decode()
    # AUDIO ONLY. A text part alongside it is rejected — the model is a dedicated ASR task, not a
    # chat model that happens to hear. Adding "please transcribe" here would break every call.
    body = {"model": MODEL, "messages": [{"role": "user", "content": [
        {"type": "input_audio",
         "input_audio": {"data": f"data:audio/wav;base64,{b64}", "format": "wav"}}]}]}
    r = httpx.post(f"https://{host}/compatible-mode/v1/chat/completions", json=body,
                   headers={"Authorization": f"Bearer {key}"}, timeout=180.0)
    if r.status_code != 200:
        raise TranscriptionUnavailable(f"{r.status_code}: {r.text[:200]}")
    return (r.json()["choices"][0]["message"].get("content") or "").strip()


def transcribe(path: pathlib.Path) -> str:
    """The words on one recorded leg. Raises TranscriptionUnavailable; never returns a guess.

    A chunk that fails is marked in place rather than dropped. A transcript silently missing its
    middle minute reads as a complete record of a shorter call, which is worse than one that
    says where the gap is.
    """
    from . import llm
    key = llm._DashScope.credential()
    if not key:
        raise TranscriptionUnavailable(available()[1])

    parts: list[str] = []
    for i, chunk in enumerate(_chunks(path)):
        try:
            if text := _one(chunk, key):
                parts.append(text)
        except TranscriptionUnavailable:
            raise
        except Exception as exc:
            logger.error("transcribing %s chunk %d failed (%s: %s)",
                         path.name, i, type(exc).__name__, exc)
            parts.append(f"[minute {i + 1}: not transcribed]")
    return " ".join(parts).strip()
