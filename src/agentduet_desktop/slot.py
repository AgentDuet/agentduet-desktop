"""The models' memory: a budget, shared by what is loaded, and who goes when it runs out.

FOUR MODELS WANT MEMORY on a Mac that may have 8 GB: the assistant's model (Gemma, ~3.6–5.7 GB),
speech (Qwen3-ASR, ~2.6 GB), the decision model (~0.6 GB, in a child process) and the search model
(~0.6 GB). Each is loaded on use and stays, because loading costs seconds and the next use is
often soon. This module decides what may stay together.

WHY A BUDGET (2026-10-05). It replaced ONE SLOT that speech, the decision and search models took
turns in, with Gemma pinned outside it. Two days in, that shape was wrong on both ends: on a
16 GB Mac it unloaded speech for the decision model when everything fitted; on an 8 GB Mac it let
Gemma and speech load together when they did not fit, and a tester's calls lost their
transcripts to `llama_decode returned -3` (Metal out of memory). A budget answers both from one
rule: a model loads beside the others if it fits, and if it does not, others make room.

THE RULE. `claim(kind)` before a use. If the model is loaded already, that is all. Otherwise its
size (`register`) must fit in `budget_mb()` minus what is loaded. When it does not, loaded models
are unloaded until it does, in this order:

  1. NEVER a protected one: speech during a call, or any model more important than the one
     asking that was used in the last `HOLD` seconds. The after-call transcript follows the call
     within seconds; taking its memory between two pieces would make it fail.
  2. A PARTNER LAST: the decision model works with the assistant's model (a person's summary asks
     both), so making room for one does not throw out the other while anything else can go.
  3. THE LESS IMPORTANT FIRST (`PRIORITY`): search, then the decision model, then the assistant,
     then speech. Recording and transcribing are the product; a summary can wait.
  4. THE LEAST RECENTLY USED among equals.

If nothing can make enough room, a model too big for the budget alone still loads once all else
is gone — the budget is an estimate, not a wall. The claim is refused only when a protected model
stands in the way: the job that asked fails and backs off
(jobs.py), and speech's own claim is never refused, since the call is the product.

THE SIZES ARE ESTIMATES, measured where they could be (models.resident_mb). A model that runs
out of memory anyway reports it (`out_of_memory`), and the budget shrinks to what was loaded at
that moment, so the next claim makes room where this one did not.

LAZY TEARDOWN. Nothing leaves only because something else finished. It leaves when room is
needed, or when it has sat unused for `IDLE[kind]` seconds (the reaper). The assistant's model
has no idle limit: its warm prompt is what makes a follow-up question 0.7 s instead of 12.5 s.

Each model registers how it is unloaded, and its size. An unloader must be safe to call at any
time and must wait for a use in progress — speech takes `transcribe._qwen_lock`, the assistant's
model `models._use_lock` (after asking a background job to stop at its next token), the decision
model ends its worker process.
"""
from __future__ import annotations

import atexit
import logging
import threading
import time
from typing import Callable

logger = logging.getLogger("secretary.slot")

ASR, DECIDER, EMBED, LLM = "asr", "decider", "embed", "llm"

#: What goes first when room is needed: lower first.
PRIORITY = {EMBED: 0, DECIDER: 1, LLM: 2, ASR: 3}

#: Models that work together, so making room for one keeps the other while anything else can go.
PARTNERS = {LLM: {DECIDER}, DECIDER: {LLM}}

#: Sizes in MB, for a model whose size is not given when it registers. Speech is the model and its
#: audio encoder (~2.4 GB on disk, ~2.5 GB resident) with a little room; the decision model is
#: ~0.5 GB working in its child process; search ~0.6 GB.
SIZE_MB = {ASR: 2600, DECIDER: 600, EMBED: 600}

#: Seconds unused before the reaper unloads. None: never by idleness.
IDLE: dict[str, float | None] = {ASR: 600, DECIDER: 120, EMBED: 300, LLM: None}

#: A model more important than the one asking is not unloaded within this many seconds of use.
HOLD = 60

#: WHAT A FULL GPU LOOKS LIKE from llama.cpp: `llama_decode returned -3` is a compute that failed,
#: which on a Mac is Metal out of memory (a tester's 8 GB Mac, 2026-10-05).
MEMORY_FAILURE = ("llama_decode returned -3", "Failed to evaluate chunk", "out of memory",
                  "failed to allocate", "kIOGPUCommandBufferCallbackErrorOutOfMemory")


def is_memory_failure(exc_or_text) -> bool:
    text = exc_or_text if isinstance(exc_or_text, str) else f"{type(exc_or_text).__name__}: {exc_or_text}"
    return any(m in text for m in MEMORY_FAILURE)


#: How often the reaper looks.
REAP_EVERY = 30

_lock = threading.Lock()
_loaded: dict[str, float] = {}                  # kind -> when it was last used
_unloaders: dict[str, Callable[[], None]] = {}
_sizes: dict[str, Callable[[], int]] = {}
_learned_mb: float | None = None                # the budget, once a model ran out of memory
_reaper: threading.Thread | None = None
_stop = threading.Event()


def register(kind: str, unload: Callable[[], None], size_mb: Callable[[], int] | None = None) -> None:
    """How `kind` is unloaded, and what it occupies once loaded. Called once by its module."""
    _unloaders[kind] = unload
    if size_mb is not None:
        _sizes[kind] = size_mb


def size_mb(kind: str) -> int:
    try:
        if kind in _sizes:
            return int(_sizes[kind]() or 0)
    except Exception:
        pass
    return SIZE_MB.get(kind, 0)


def budget_mb() -> float:
    """What the models may hold together: `machine.budget_gb()` (two thirds of RAM on a Mac),
    or less once a model ran out of memory. 0 when the machine cannot be read — then nothing is
    unloaded for room, since nothing would be taken away on a guess."""
    try:
        from . import machine
        base = machine.budget_gb() * 1024
    except Exception:
        base = 0.0
    if _learned_mb is not None and base:
        return min(base, _learned_mb)
    return base


def loaded() -> list[str]:
    """What the budget counts as loaded, most recently used first."""
    with _lock:
        return sorted(_loaded, key=lambda k: -_loaded[k])


def occupant() -> str:
    """The most recently used model, or "" (kept for the log export and older callers)."""
    got = loaded()
    return got[0] if got else ""


def tight() -> bool:
    """Whether this machine cannot hold the assistant's model and speech together — the case
    where they take turns. For the log export and for `llm.preload`, which does not load the
    assistant's model at start where speech would have to unload it."""
    budget = budget_mb()
    return bool(budget) and size_mb(LLM) + size_mb(ASR) > budget


def _call_on() -> bool:
    try:
        from . import gate
        return gate._call_on()
    except Exception:
        return False


def _protected(kind: str, asking: str, now: float) -> bool:
    if kind == ASR and _call_on():
        return True                              # speech during a call, whoever asks
    return (PRIORITY.get(kind, 0) > PRIORITY.get(asking, 0)
            and now - _loaded.get(kind, 0.0) < HOLD)


def _victims(asking: str, now: float) -> list[str] | None:
    """What to unload so `asking` fits, in order; None if it cannot be made to fit."""
    budget = budget_mb()
    if not budget:
        return []
    need = size_mb(asking)
    free = budget - sum(size_mb(k) for k in _loaded)
    if free >= need:
        return []
    partners = PARTNERS.get(asking, set())
    candidates = sorted((k for k in _loaded if not _protected(k, asking, now)),
                        key=lambda k: (k in partners, PRIORITY.get(k, 0), _loaded[k]))
    out = []
    for k in candidates:
        if free >= need:
            break
        out.append(k)
        free += size_mb(k)
    if free >= need:
        return out
    # TOO BIG FOR THE BUDGET EVEN ALONE (an estimate high for this machine, or a budget shrunk by
    # `out_of_memory`): it still loads when only what may go stands in its way, everything else
    # going first. Refused only for a protected model — speech during a call, or in use.
    if all(not _protected(k, asking, now) for k in _loaded):
        return out
    return None


def claim(kind: str) -> bool:
    """Make room for `kind`, unloading others first if it does not fit. False when it may not
    load now. Call just before using the model; the model itself loads on use, as before."""
    _start_reaper()
    now = time.time()
    with _lock:
        if kind in _loaded:
            _loaded[kind] = now
            return True
        victims = _victims(kind, now)
        if victims is None:
            if kind != ASR:
                logger.info("slot: %s waits — no room it may take (%s loaded)", kind,
                            ", ".join(sorted(_loaded)) or "nothing")
                return False
            victims = [k for k in _loaded if k != kind]   # SPEECH IS NEVER REFUSED: all else goes
        for k in victims:
            _loaded.pop(k, None)
        _loaded[kind] = now
    for k in victims:
        logger.info("slot: unloading %s for %s", k, kind)
        _unload(k)
    return True


def touch(kind: str) -> None:
    """`kind` was just used: its idle clock starts again."""
    with _lock:
        if kind in _loaded:
            _loaded[kind] = time.time()


def released(kind: str) -> None:
    """`kind` was unloaded by its own module (an owner's Unload, a worker that ended): stop
    counting it. Safe to call when it is not counted."""
    with _lock:
        _loaded.pop(kind, None)


def out_of_memory(kind: str) -> None:
    """`kind` ran out of memory despite the budget: the estimates were high for this machine, so
    the budget becomes what was loaded then, less a margin. Lasts until the app restarts."""
    global _learned_mb
    with _lock:
        held = sum(size_mb(k) for k in _loaded)
    if held:
        _learned_mb = max(1024.0, held - 512.0)
        logger.warning("slot: %s ran out of memory with %.1f GB loaded — budget now %.1f GB",
                       kind, held / 1024, _learned_mb / 1024)


def _unload(kind: str) -> None:
    fn = _unloaders.get(kind)
    if fn is None:
        return
    try:
        fn()
    except Exception as exc:                      # an unloader must never take a caller down
        logger.warning("slot: could not unload %s (%s: %s)", kind, type(exc).__name__, exc)


def reap(now: float | None = None) -> list[str]:
    """Unload whatever has been idle past its limit. Returns what was unloaded."""
    now = time.time() if now is None else now
    gone = []
    with _lock:
        for kind, last in list(_loaded.items()):
            limit = IDLE.get(kind, 600)
            if limit is None or now - last < limit:
                continue
            if kind == ASR and _call_on():
                continue                          # never under a call's captions
            _loaded.pop(kind)
            gone.append(kind)
    for kind in gone:
        logger.info("slot: %s idle — unloading", kind)
        _unload(kind)
    return gone


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
    reaper stops and every model is unloaded, the assistant's included."""
    _stop.set()
    for kind in loaded():
        _unload(kind)
    try:
        from . import models
        if models.loaded():
            models.unload()
    except Exception:
        pass
