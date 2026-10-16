"""The format of the recording the owner keeps: AAC in `.m4a` on a Mac, WAV elsewhere.

WHY (Stanley, 2026-10-16). The merge was 24 kHz stereo 16-bit WAV: 5.8 MB a minute, so ten
three-minute calls a day came to about 60 GB a year. AAC at `BITRATE` is about 0.36 MB a minute.
`.m4a` is what an iPhone's own call recording and Voice Memos save, plays in Quick Look,
QuickTime, iPhone and Windows, and Apple's encoder ships with macOS (`afconvert`), so nothing is
bundled and Apple carries the AAC licence. MP3 was the other candidate (the PBX convention, patents
expired) but macOS has no MP3 encoder, so it would mean bundling LAME.

WHAT IS STILL WAV: the legs in `carry.legs()`. They are working files that transcription reads,
so the full app's transcript never comes from compressed audio. Only the owner's copy is
compressed — and AgentDuet AI transcribes that copy, through `read` (the cost: see `BITRATE`).

ELSEWHERE, OR IF THE ENCODER FAILS, the recording stays WAV: a call is never lost to a format.
Older recordings stay WAV too; every reader takes both (`EXTENSIONS`).
"""
from __future__ import annotations

import logging
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import wave

logger = logging.getLogger("secretary")

#: Every extension a kept recording may have, preferred first.
EXTENSIONS = (".m4a", ".wav")

#: AAC bit rate for the stereo recording: 24 kbps a party, ~0.36 MB a minute (16x smaller).
#: MEASURED 2026-10-16 on three real calls (English, Mandarin, Malay), each side transcribed by
#: Qwen3-ASR from the WAV and from AAC decoded back, as word similarity to the WAV's transcript.
#: The WAV run twice gave 1.000, so every difference is the codec:
#:   32k 0.23-0.84 · 48k 0.74-0.84 · 64k 0.30-0.84 · 96k 0.79-0.93
#: No rate is transparent to the speech model, and 64k bought nothing over 48k. So nothing in
#: the full app transcribes this file: it transcribes the WAV legs. AgentDuet AI, which has only
#: this file, is the one reader that pays.
BITRATE = 48_000

#: Long enough for a four-hour call on a slow Mac; `afconvert` does a 4-minute call in ~0.1 s.
TIMEOUT_SECONDS = 300


def _afconvert() -> str | None:
    return shutil.which("afconvert")


def extension() -> str:
    """The extension a NEW recording gets on this machine."""
    return ".m4a" if _afconvert() else ".wav"


def find(folder: pathlib.Path, stem: str) -> pathlib.Path | None:
    """The kept recording for `stem` in `folder`, whichever format it was saved in."""
    for ext in EXTENSIONS:
        p = folder / f"{stem}{ext}"
        if p.is_file():
            return p
    return None


def is_recording(name: str) -> bool:
    return name.lower().endswith(EXTENSIONS)


def write(out: pathlib.Path, channels: int, rate: int, width: int, frames: bytes) -> pathlib.Path | None:
    """Save PCM as the call's recording at `out` (its suffix is replaced). The path written, or None.

    WRITTEN WHOLE, BY RENAME, like the call's `.txt`: AgentDuet AI may be watching the folder,
    and must never read half a file.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.suffix == ".m4a" and (tool := _afconvert()):
        final = out.with_suffix(".m4a")
        part = final.with_name(f".{final.name}.part")
        with tempfile.TemporaryDirectory() as tmp:
            src = pathlib.Path(tmp) / "call.wav"
            _write_wav(src, channels, rate, width, frames)
            try:
                subprocess.run([tool, "-f", "m4af", "-d", "aac", "-b", str(BITRATE), str(src), str(part)],
                               check=True, capture_output=True, timeout=TIMEOUT_SECONDS)
                os.replace(part, final)
                return final
            except (OSError, subprocess.SubprocessError) as exc:
                part.unlink(missing_ok=True)
                # KEEP THE CALL. A recording in the bigger format beats none.
                logger.warning("could not encode %s as AAC (%s) — keeping it as WAV", final.name, exc)
    final = out.with_suffix(".wav")
    part = final.with_name(f".{final.name}.part")
    try:
        _write_wav(part, channels, rate, width, frames)
        os.replace(part, final)
    except (OSError, wave.Error) as exc:
        part.unlink(missing_ok=True)
        logger.warning("could not write %s (%s)", final.name, exc)
        return None
    return final


def _write_wav(path: pathlib.Path, channels: int, rate: int, width: int, frames: bytes) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(frames)


def read(path: pathlib.Path) -> tuple[int, int, int, bytes]:
    """(channels, rate, sample width, PCM frames) of a kept recording. Raises OSError."""
    if path.suffix.lower() == ".m4a":
        tool = _afconvert()
        if not tool:
            raise OSError(f"{path.name}: no AAC decoder on this machine")
        with tempfile.TemporaryDirectory() as tmp:
            pcm = pathlib.Path(tmp) / "call.wav"
            try:
                subprocess.run([tool, "-f", "WAVE", "-d", "LEI16", str(path), str(pcm)],
                               check=True, capture_output=True, timeout=TIMEOUT_SECONDS)
            except subprocess.SubprocessError as exc:
                raise OSError(f"{path.name}: could not decode ({exc})") from exc
            return _read_wav(pcm)
    return _read_wav(path)


def _read_wav(path: pathlib.Path) -> tuple[int, int, int, bytes]:
    try:
        with wave.open(str(path), "rb") as r:
            return r.getnchannels(), r.getframerate(), r.getsampwidth(), r.readframes(r.getnframes())
    except (EOFError, wave.Error) as exc:
        raise OSError(f"{path.name}: {exc}") from exc


#: Lengths already read, by (path, size, mtime). The hub asks for every call of the open person on
#: each poll, and reading an `.m4a`'s length starts a process.
_LENGTHS: dict[tuple, float | None] = {}


def seconds(path: pathlib.Path) -> float | None:
    """How long a kept recording plays, or None when it cannot be read."""
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_size, st.st_mtime_ns)
    if key not in _LENGTHS:
        if len(_LENGTHS) > 4096:
            _LENGTHS.clear()
        _LENGTHS[key] = _seconds(path)
    return _LENGTHS[key]


def _seconds(path: pathlib.Path) -> float | None:
    if path.suffix.lower() == ".m4a":
        tool = shutil.which("afinfo")
        if not tool:
            return None
        try:
            out = subprocess.run([tool, str(path)], capture_output=True, text=True,
                                 timeout=30, check=True).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        m = re.search(r"estimated duration:\s*([\d.]+)", out)
        return float(m.group(1)) if m else None
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except (OSError, EOFError, wave.Error):
        return None
