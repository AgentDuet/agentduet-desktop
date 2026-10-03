"""AgentDuet AI's way in: the recordings AgentDuet Recorder leaves in the AgentDuet folder.

AgentDuet AI has no phone line (see `edition.py`). It finds a call by its files: the stereo
recording the recorder merged, and the `.txt` beside it whose header the recorder wrote
(`carry.HEADER_FIRST`, `merge.write_txt`). Nothing else is accepted — not a file the owner drops
in, not one without that header — because the header is what says who the call was with, and a
recording that cannot say so is not one this app can file.

FOR EACH NEW CALL:
  1. its header is read — the number, the direction, when it started and how long it was;
  2. the call is FILED in this app's own `calls.jsonl`, which is what the hub, the summaries and
     the assistant read, exactly as for a call the full app carried itself;
  3. if its `.txt` has no transcript yet, the recording is SPLIT into its two sides — caller left,
     owner right, already aligned by the recorder's merge — and handed to the ordinary
     transcription queue as two legs. The transcript is then written below the header
     (`merge.once(audio_too=False)`); the recording itself is never written.

THE FOLDER IS POLLED. A glob of one folder every few seconds costs nothing, needs no native
watcher, and catches up after the app was closed — the queue is the folder, as it is everywhere
else here. What has been taken is kept in this app's own instance (`run/ingested.json`), never in
the recorder's folder.
"""
from __future__ import annotations

import array
import asyncio
import json
import logging
import re
import wave
from datetime import datetime

from . import carry, paths

logger = logging.getLogger("secretary.ingest")

#: How often the folder is looked at. A call is picked up within this of the recorder merging it.
POLL_SECONDS = 5

SEEN = paths.RUN / "ingested.json"


def _seen() -> set[str]:
    try:
        return set(json.loads(SEEN.read_text()))
    except (OSError, ValueError):
        return set()


def _remember(seen: set[str]) -> None:
    SEEN.parent.mkdir(parents=True, exist_ok=True)
    tmp = SEEN.with_name(SEEN.name + ".part")
    tmp.write_text(json.dumps(sorted(seen)))
    tmp.replace(SEEN)


def parse_header(head: str) -> dict:
    """The recorder's header as fields: number, name, outgoing, started (epoch), seconds, recording."""
    fields = {}
    for line in head.splitlines():
        key, _, value = line.partition(":")
        fields[key.strip().lower()] = value.strip()
    call = fields.get("call", "")
    m = re.fullmatch(r"(.*?)\s*\(([^()]+)\)", call)
    number = (m.group(2) if m else call).strip()
    started = None
    try:
        started = datetime.fromisoformat(fields.get("started", "")).timestamp()
    except ValueError:
        pass
    seconds = 0
    if mm := re.fullmatch(r"(\d+):(\d{2})", fields.get("length", "")):
        seconds = int(mm.group(1)) * 60 + int(mm.group(2))
    return {"number": "" if number == "unknown" else number,
            "name": m.group(1).strip() if m else "",
            "outgoing": fields.get("direction", "") == "outgoing",
            "started": started, "seconds": seconds,
            "recording": fields.get("recording", "")}


def found() -> list[tuple[str, dict, bool]]:
    """(stem, header fields, already transcribed) for every recorder call in the folder."""
    out = []
    folder = carry.recordings()
    if not folder.is_dir():
        return out
    for wav in sorted(folder.glob("*.wav")):
        txt = wav.with_suffix(".txt")
        if not txt.is_file():
            continue                        # not merged yet, or not the recorder's
        try:
            head, body = carry.split_txt(txt.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        if not head:
            continue                        # NOT A RECORDER CALL: nothing says who it was with
        fields = parse_header(head)
        if fields["recording"] != wav.name:
            continue
        out.append((wav.stem, fields, bool(body)))
    return out


def split(stem: str) -> bool:
    """Write the recording's two channels as legs for the transcription queue. True when done.

    Left is the caller and right the owner — the recorder's merge put them there, already
    aligned, so both legs start at zero.
    """
    src = carry.recordings() / f"{stem}.wav"
    try:
        with wave.open(str(src), "rb") as r:
            if r.getnchannels() != 2 or r.getsampwidth() != 2:
                logger.warning("ingest %s: not a two-channel 16-bit recording", stem)
                return False
            rate = r.getframerate()
            frames = r.readframes(r.getnframes())
    except (OSError, EOFError, wave.Error) as exc:
        logger.warning("ingest %s: cannot read the recording (%s)", stem, exc)
        return False
    both = array.array("h")
    both.frombytes(frames)
    carry.legs().mkdir(parents=True, exist_ok=True)
    for leg, samples in (("caller", both[0::2]), ("callee", both[1::2])):
        final = carry.legs() / f"{stem}-{leg}.wav"
        # AS `.part` AND RENAMED, as `carry` writes its own legs: the transcription queue takes
        # any `*.wav` it sees, and must never see half of one.
        part = final.with_name(final.name + ".part")
        with wave.open(str(part), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(samples.tobytes())
        final.with_suffix(".start").write_text("0.000\n")
        part.replace(final)
    return True


def once() -> int:
    """Take every recorder call not taken yet. Returns how many."""
    from . import calls
    seen = _seen()
    taken = 0
    for stem, f, transcribed in found():
        if stem in seen:
            continue
        if not transcribed and not split(stem):
            continue                        # tried again next pass
        call_id = stem.split("-", 1)[1] if "-" in stem else stem
        ended = (f["started"] + f["seconds"]) if f["started"] else None
        calls.record(call_id, f["number"], "carried", recordings=[f"{stem}.wav"],
                     outgoing=f["outgoing"], started=f["started"], at=ended)
        seen.add(stem)
        _remember(seen)
        taken += 1
        logger.info("ingest %s: filed (%s, %s)%s", stem, f["number"] or "unknown",
                    "outgoing" if f["outgoing"] else "incoming",
                    "" if transcribed else "; transcribing")
    if taken:
        from . import transcribe
        transcribe.wake()
    return taken


async def worker() -> None:
    """Look at the folder forever, off the event loop."""
    while True:
        try:
            await asyncio.to_thread(once)
        except Exception as exc:            # a worker that dies takes the folder with it
            logger.error("the ingest worker hit %s: %s", type(exc).__name__, exc)
        await asyncio.sleep(POLL_SECONDS)
