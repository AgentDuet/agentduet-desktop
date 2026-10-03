"""One memory slot for the occasional models: speech (Qwen3-ASR), the decision model, or the
search model (EmbeddingGemma) — one at a time.

WHY (Stanley, 2026-10-03). Three models want memory on a Mac that may have 16 GB: Gemma (~5.6 GB,
the assistant and summaries), Qwen3-ASR (~2.5 GB) and the decision model (~0.5 GB working, 1.7 GB
mapped). Gemma STAYS LOADED: its warm prompt is what makes a follow-up question 0.7 s instead of
12.5 s cold (measured). The other two are needed now and then, and never at the same moment
for any reason that matters — so they share one slot, and the peak is Gemma plus the larger of
them (~8 GB) instead of all three (~10 GB).

LAZY TEARDOWN. Whatever is in the slot stays after it is used, because loading costs seconds and
the next use is often soon (the next piece of a call, the next summary). It leaves only when:
  - the OTHER model claims the slot — it is unloaded first, then the other loads; or
  - it has sat unused for `IDLE[kind]` seconds — the reaper unloads it.

DURING A CALL, SPEECH WINS. Live captions use the speech model piece by piece, so the decision
model may not push it out while a call is on: its claim is refused, and the summary that asked
falls back to its word rule (decider.ask answers None). Summaries do not run during calls anyway
(gate.py); this makes it a rule rather than a coincidence.

Each model registers how it is unloaded (`register`) and claims the slot just before use
(`claim`). An unloader must be safe to call at any time and must wait for a use in progress —
the speech model's takes `transcribe._qwen_lock`, the decision model's ends its worker process.
"""
from __future__ import annotations

import atexit
import logging
import threading
import time
from typing import Callable

logger = logging.getLogger("secretary.slot")

ASR, DECIDER, EMBED = "asr", "decider", "embed"

#: Seconds unused before the reaper unloads. Speech is kept longer: a call's pieces, the after-call
#: pass and the next call tend to come together, and reloading it costs more.
IDLE = {ASR: 600, DECIDER: 120, EMBED: 300}

#: How often the reaper looks.
REAP_EVERY = 30

_lock = threading.Lock()
_occupant = ""
_last = 0.0
_unloaders: dict[str, Callable[[], None]] = {}
_reaper: threading.Thread | None = None
_stop = threading.Event()


def register(kind: str, unload: Callable[[], None]) -> None:
    """How `kind` is unloaded. Called once by the module that loads it."""
    _unloaders[kind] = unload


def occupant() -> str:
    return _occupant


def _call_on() -> bool:
    try:
        from . import gate
        return gate._call_on()
    except Exception:
        return False


def claim(kind: str) -> bool:
    """Make the slot `kind`'s, unloading the other model first. False when it may not have it now.

    Call just before using the model. The model itself loads on use, as before; this only makes
    room and remembers who is there.
    """
    global _occupant, _last
    _start_reaper()
    with _lock:
        other = _occupant if _occupant and _occupant != kind else ""
        # DURING A CALL NOTHING PUSHES SPEECH OUT: not the decision model, not search.
        if other == ASR and kind != ASR and _call_on():
            logger.info("slot: %s waits — a call is using speech", kind)
            return False
        _occupant, _last = kind, time.time()
    if other:
        logger.info("slot: unloading %s for %s", other, kind)
        _unload(other)
    return True


def touch(kind: str) -> None:
    """`kind` was just used: its idle clock starts again."""
    global _last
    with _lock:
        if _occupant == kind:
            _last = time.time()


def _unload(kind: str) -> None:
    fn = _unloaders.get(kind)
    if fn is None:
        return
    try:
        fn()
    except Exception as exc:                      # an unloader must never take a caller down
        logger.warning("slot: could not unload %s (%s: %s)", kind, type(exc).__name__, exc)


def reap(now: float | None = None) -> str:
    """Unload the occupant if it has been idle long enough. Returns what was unloaded, or ""."""
    global _occupant
    now = time.time() if now is None else now
    with _lock:
        kind = _occupant
        if not kind or now - _last < IDLE.get(kind, 600):
            return ""
        if kind == ASR and _call_on():
            return ""                                # never under a call's captions
        _occupant = ""
    logger.info("slot: %s idle for %ds — unloading", kind, int(now - _last))
    _unload(kind)
    return kind


def _start_reaper() -> None:
    global _reaper
    if _reaper is not None:
        return
    def loop():
        while not _stop.wait(REAP_EVERY):
            reap()
    _reaper = threading.Thread(target=loop, name="slot-reaper", daemon=True)
    _reaper.start()


@atexit.register
def _shutdown() -> None:
    """FREE THE MODELS BEFORE PYTHON EXITS. A GPU model still loaded while this module's reaper
    thread is alive trips llama.cpp's Metal teardown (`GGML_ASSERT([rsets->data count] == 0)`)
    and prints a crash at every ordinary exit — found 2026-10-03, reproduced in isolation. So the
    reaper stops, the slot's model is unloaded, and so is the assistant's."""
    _stop.set()
    if _occupant:
        _unload(_occupant)
    try:
        from . import models
        if models.loaded():
            models.unload()
    except Exception:
        pass
