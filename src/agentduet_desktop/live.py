"""Live captions: each side of a call, transcribed piece by piece while the call is on.

A PREVIEW, NOT THE RECORD. The after-call pass in `transcribe` still runs on the finished legs and
its transcript is the one that is kept; this exists so the owner can read along. That split is
deliberate: the after-call pass sees the whole leg, so it learns the line's noise floor from all of
it, where this has only the audio so far.

HOW. `carry._record_leg` already drains each party's audio as it arrives, to write the WAV. It
hands each chunk to a `Leg` here as well. The leg cuts at pauses exactly as `transcribe._pieces`
does after the call — PIECE_GAP of quiet ends a piece, PIECE_MAX caps one — and puts every
finished piece on ONE queue. One worker drains it through `transcribe.qwen_piece`, which shares
the resident model with the after-call pass under a lock. A caption appears about PIECE_GAP after
the speaker stops, plus the fraction of a second the piece takes to transcribe.

The two parties are separate streams, so who said each caption is known without guessing: the
`caller` leg is the other party and the `callee` leg is the owner, in both directions — the same
convention the leg files use.

Qwen only. Whisper and Apple are not wired here: this is a preview, and the engine that detects
language per piece is the one worth previewing with.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import time

logger = logging.getLogger("secretary")

#: The recording format `carry` writes: 24 kHz mono 16-bit.
RATE = 24_000
#: Energy frame, in samples at RATE. 30 ms, the same frame `transcribe._pieces` uses.
FRAME = int(RATE * 0.03)
#: Frames of history the noise floor is learned from — about ten seconds.
FLOOR_FRAMES = 330

#: The calls on right now: call_id -> {"who", "started", "captions": [...]}. What a page that opens
#: mid-call needs in order to catch up.
_calls: dict[str, dict] = {}
_queue: "asyncio.Queue | None" = None
#: call_id -> languages a long piece of that call has shown. See `worker`.
_confirmed: dict[str, set] = {}


def enabled() -> bool:
    """Whether captions can run: the speech engine is Qwen and its model is on disk."""
    from . import transcribe
    return transcribe.local_model() == transcribe.QWEN and transcribe.is_cached(transcribe.QWEN)


def snapshot() -> dict:
    """Every call in progress, with its captions so far — sent to a page when it connects."""
    return {"type": "live_calls",
            "calls": [{"call": cid, "who": c["who"], "started": c["started"],
                       "captions": c["captions"]} for cid, c in _calls.items()]}


async def _push(obj: dict) -> None:
    from . import phone
    await phone._broadcast(obj)


async def start(call_id: str, who: str) -> None:
    """A call is on. Pages mark the person as live, and the speech model is loaded now.

    LOADED AT THE START OF THE CALL, not on the first piece. It is not kept resident between
    calls, and loading it on demand held the first caption of the first real call eight seconds.
    """
    _calls[call_id] = {"who": who, "started": time.time(), "captions": []}
    if enabled():
        from . import transcribe
        asyncio.get_running_loop().run_in_executor(None, _warm, transcribe)
    await _push({"type": "live_start", "call": call_id, "who": who,
                 "started": _calls[call_id]["started"]})


def _warm(transcribe) -> None:
    try:
        with transcribe._qwen_lock:
            transcribe._qwen_model()
    except Exception as exc:
        logger.warning("live captions: could not load the speech model (%s: %s)",
                       type(exc).__name__, exc)


async def end(call_id: str) -> None:
    """The call is over. Pages drop the live mark; the captions stay until the real transcript."""
    _confirmed.pop(call_id, None)
    if _calls.pop(call_id, None) is not None:
        await _push({"type": "live_end", "call": call_id})


class Leg:
    """One party's audio, cut into pieces at pauses as it arrives.

    `feed` is called from the recorder with every chunk, so it must never block: it only measures
    and slices, and hands finished pieces to the queue.
    """

    def __init__(self, call_id: str, leg: str) -> None:
        from . import transcribe
        self.call_id, self.leg = call_id, leg
        self.gap = int(transcribe.PIECE_GAP / 0.03)
        self.max_frames = int(transcribe.PIECE_MAX / 0.03)
        self.min_frames = int(transcribe.PIECE_MIN / 0.03)
        self.pad = int(transcribe.PIECE_PAD / 0.03)
        self.started = None                  # wall clock of this leg's first frame
        self.buf = bytearray()               # samples not yet measured
        self.frames: list[bytes] = []        # this piece's frames, including its lead-in pad
        self.history = collections.deque(maxlen=FLOOR_FRAMES)
        self.speaking = False
        self.quiet = 0
        self.piece_at = 0                    # frame index where the current piece starts
        self.seen = 0                        # frames measured so far
        self.lead = collections.deque(maxlen=self.pad)

    def feed(self, chunk: bytes) -> None:
        if self.started is None:
            self.started = time.time()
        self.buf += chunk
        step = FRAME * 2
        while len(self.buf) >= step:
            frame, self.buf = bytes(self.buf[:step]), self.buf[step:]
            self._frame(frame)

    def _frame(self, frame: bytes) -> None:
        import numpy as np
        a = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
        rms = float(np.sqrt((a * a).mean()))
        self.history.append(rms)
        floor = float(np.percentile(self.history, 10)) if self.history else 0.0
        loud = rms > max(floor * 3.0, 0.004)
        i = self.seen
        self.seen += 1
        if not self.speaking:
            if loud:
                self.speaking, self.quiet = True, 0
                self.frames = list(self.lead) + [frame]
                self.piece_at = i - len(self.lead)
            else:
                self.lead.append(frame)
            return
        self.frames.append(frame)
        self.quiet = 0 if loud else self.quiet + 1
        if self.quiet >= self.gap or len(self.frames) >= self.max_frames:
            self._close()

    def _close(self) -> None:
        # Trim the trailing silence back to the pad, so the piece ends where the speech did.
        keep = len(self.frames) - max(0, self.quiet - self.pad)
        frames = self.frames[:keep]
        speech = keep - len(self.lead) - self.pad
        self.speaking, self.quiet, self.frames = False, 0, []
        self.lead.clear()
        if speech < self.min_frames:
            return
        at = (self.started or time.time()) + self.piece_at * 0.03
        _enqueue(self.call_id, self.leg, at, len(frames) * 0.03, b"".join(frames))

    def flush(self) -> None:
        """The call ended mid-sentence: whatever was being said is still a piece."""
        if self.speaking and self.frames:
            self._close()


def _enqueue(call_id: str, leg: str, at: float, secs: float, pcm: bytes) -> None:
    if _queue is None or call_id not in _calls:
        return
    try:
        _queue.put_nowait((call_id, leg, at, secs, pcm))
    except asyncio.QueueFull:
        logger.info("live captions: queue full, a piece of call %s was skipped", call_id)


def _transcribe(pcm: bytes) -> tuple[str, str]:
    """One piece of 24 kHz int16 audio, resampled to the 16 kHz the model takes."""
    import numpy as np
    from . import transcribe
    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    want = int(len(a) * 16_000 / RATE)
    a16 = np.interp(np.linspace(0, len(a) - 1, want), np.arange(len(a)), a).astype(np.float32)
    return transcribe.qwen_piece(a16)


async def worker() -> None:
    """Transcribe live pieces in the order they finished, one at a time, off the event loop."""
    global _queue
    from . import transcribe
    _queue = asyncio.Queue(maxsize=64)
    while True:
        call_id, leg, at, secs, pcm = await _queue.get()
        if call_id not in _calls:
            continue                          # the call ended while this piece waited
        try:
            lang, said = await asyncio.to_thread(_transcribe, pcm)
        except Exception as exc:
            logger.warning("live captions: a piece failed (%s: %s)", type(exc).__name__, exc)
            continue
        if not said or call_id not in _calls:
            continue
        # THE SAME RULE AS AFTER THE CALL, applied to what has been heard SO FAR: a short piece may
        # not introduce a language no longer piece has confirmed. Live, that can drop a first
        # "Hi" before anything longer has been said — acceptable in a preview, and the after-call
        # transcript still has it if it was real.
        seen = _confirmed.setdefault(call_id, set())
        if secs >= transcribe.PIECE_SURE and lang:
            seen.add(lang)
        elif lang and lang not in seen:
            continue
        cap = {"leg": leg, "at": at, "text": said}
        _calls[call_id]["captions"].append(cap)
        await _push(dict(cap, type="caption", call=call_id))
