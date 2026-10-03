"""Merging a call's two legs into the one file the owner keeps — no speech involved.

Split out of `transcribe.py` on 2026-10-03 for the RECORDER edition (see `edition.py`), which
has no transcription but still keeps one stereo file per call: playback reads the merge, never a
single leg. In the full edition `transcribe` calls `once()` with its own readiness test (every
leg transcribed) and its transcript writer; the recorder calls it with neither, from `worker`.
"""
from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import time
import wave

from . import carry

logger = logging.getLogger("secretary")

# The legs exist because keeping the parties apart is what lets a transcript say who spoke
# without diarisation. What the owner asked for is ONE file per call, so the pair is merged
# after the fact — on a queue, where nothing is waiting. It is
# deliberately not done in `carry._record_leg`: that runs while two people are talking, and
# "a failure here must not kill the call".
#
# STEREO, ONE PARTY PER CHANNEL — not a sum. Summing is what the old comment in carry.py warns
# about: the legs are not sample-aligned, so adding them puts one voice ahead of the other and
# compresses both. Two channels keep every sample of each party exactly, stay separable for a
# future re-transcription, and open in any player as one recording.
MERGE_SUFFIX = ".merged"


def leg_start(wav: pathlib.Path) -> float | None:
    """When this leg's first frame arrived, from the sidecar `carry` wrote."""
    try:
        return float(wav.with_suffix(".start").read_text().strip())
    except (OSError, ValueError):
        return None


def ready(settled) -> list[str]:
    """Stems whose legs are all `settled(wav)` and which have not been merged yet."""
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
        if not all(settled(w) for w in wavs):
            continue
        out.append(stem)
    return out


def audio(stem: str, wavs: list[pathlib.Path]) -> bool:
    """Write one stereo WAV: caller left, callee right, aligned by their start sidecars."""
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
    starts = {leg: leg_start(w) for leg, w in sides.items()}
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


def recorded(wav: pathlib.Path) -> bool:
    """A leg is finished once nothing of its call is still being written.

    `carry` writes each leg as `.part` and renames it on close, so a `.part` for the stem means
    the call (or its other leg) is still going. Merging then would keep half a call.
    """
    stem = carry.stem_of(wav.name)
    return not any(carry.legs().glob(f"{stem}-*.wav.part"))


# ---- the call's .txt: its header is written here ------------------------------------------

#: How long a merged call waits for its row in calls.jsonl, which names who it was with. The row
#: is written as the call ends, so it is normally there; this only covers a merge that runs
#: first, and a call whose row never came (a crash), which then gets "unknown" rather than none.
ROW_WAIT_SECONDS = 600


def _row(stem: str) -> dict | None:
    from . import calls
    call_id = stem.split("-", 1)[1] if "-" in stem else stem
    return next((r for r in calls.recent(2000) if r.get("call_id") == call_id), None)


def _length(path: pathlib.Path) -> str:
    try:
        with wave.open(str(path), "rb") as w:
            s = round(w.getnframes() / float(w.getframerate() or 1))
    except (OSError, EOFError, wave.Error):
        return ""
    return f"{s // 60}:{s % 60:02d}"


def header(stem: str, row: dict | None) -> str:
    """The call's details, for the top of its `.txt` — see carry.HEADER_FIRST for the format.

    THE NUMBER IS THE KEY, the name only a label: a name changes in Contacts and this header
    does not follow it, so nothing may match a person by it.
    """
    from . import names
    row = row or {}
    who = (row.get("caller") or "").strip()
    if who in ("", "?"):
        call = "unknown"
    else:
        name = names.name_for(who)
        call = f"{name} ({who})" if name and name != who else who
    started = (row.get("started") or row.get("at") or "").replace("T", " ")
    lines = [f"{carry.HEADER_FIRST} {call}"]
    if row:
        lines.append(f"Direction: {'outgoing' if row.get('outgoing') else 'incoming'}")
    if started:
        lines.append(f"Started: {started}")
    wav = carry.merged_wav(stem)
    if length := _length(wav):
        lines.append(f"Length: {length}")
    lines.append(f"Recording: {wav.name}")
    return "\n".join(lines)


def write_txt(stem: str, body: str | None = None, row: dict | None = None) -> None:
    """Write a call's `.txt`: its header, then `body` — or the body already there when None.

    WRITTEN WHOLE, BY RENAME. Another app may be watching this folder and read the file the
    moment it appears; written in place it could be caught empty or cut short, and a reader that
    found no header would write its own without one. A rename is one step, so the file is either
    the old one or the new one. The header already on disk is kept when there is no row to build
    one from, so a later body write cannot erase details the recorder wrote.
    """
    path = carry.merged_txt(stem)
    old_head, old_body = ("", "")
    if path.is_file():
        try:
            old_head, old_body = carry.split_txt(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            pass
    head = header(stem, row) if row is not None or not old_head else old_head
    text = head + "\n\n" + ((old_body if body is None else body).strip())
    tmp = path.with_name(f".{path.name}.part")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text.rstrip() + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("merge %s: could not write %s (%s)", stem, path.name, exc)
        tmp.unlink(missing_ok=True)


def once(settled=None, text=None, audio_too: bool = True) -> int:
    """Merge every call whose legs are settled. Returns how many were written.

    `settled` says when a leg is ready (default: finished recording, see `recorded`); `text`,
    when given, writes the call's transcript after its audio — the full edition's speech pass.
    `audio_too=False` is AgentDuet AI's: the recording and its header are the recorder's, so
    only the transcript is written, below the header already there.
    """
    done = 0
    for stem in ready(settled or recorded):
        wavs = sorted(w for w in carry.legs().glob("*.wav")
                      if carry.stem_of(w.name) == stem)
        if not audio_too:
            if text is not None:
                text(stem, wavs)
                done += 1
            # THE SPLIT LEGS ARE OURS, copies of the recorder's file: kept only to transcribe.
            for w in wavs:
                w.unlink(missing_ok=True)
            try:
                (carry.legs() / f"{stem}{MERGE_SUFFIX}").write_text("")
            except OSError as exc:
                logger.warning("merge %s: could not mark it done (%s)", stem, exc)
            continue
        # WAIT FOR THE ROW that says who the call was with, briefly — see ROW_WAIT_SECONDS.
        row = _row(stem)
        if row is None and wavs and time.time() - max(w.stat().st_mtime for w in wavs) < ROW_WAIT_SECONDS:
            continue
        if audio(stem, wavs):
            write_txt(stem, row=row or {})
            if text is not None:
                text(stem, wavs)
            done += 1
        # MARKED EITHER WAY. A call whose legs are all empty — an unbridged call, which is every
        # call until the platform hands us audio — has nothing to merge and must not be
        # reconsidered on every poll for the life of the instance.
        try:
            (carry.legs() / f"{stem}{MERGE_SUFFIX}").write_text("")
        except OSError as exc:
            logger.warning("merge %s: could not mark it done (%s)", stem, exc)
    return done


#: How often the recorder's merge looks for work. A call is merged within this of hanging up.
POLL_SECONDS = 10


async def worker() -> None:
    """The RECORDER edition's queue: merge finished calls, forever, off the event loop.

    The full edition does not run this — its transcription worker merges, after the words.
    """
    while True:
        await asyncio.sleep(POLL_SECONDS)
        try:
            await asyncio.to_thread(once)
        except Exception as exc:            # a worker that dies takes the queue with it
            logger.error("the merge worker hit %s: %s", type(exc).__name__, exc)
