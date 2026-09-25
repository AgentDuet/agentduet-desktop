"""One local model, one job at a time, the most urgent first.

WHY. Gemma is a single engine with a single working memory (its KV cache), and several things
want it: the owner's question, the calendar suggestion after a call, and the background work
that keeps the assistant fast. Nothing serialised them — the assistant and the suggestion worker
could call the same engine from two threads at once — and nothing ranked them, so a suggestion
could hold the model while the owner waited.

THE ORDER, most urgent first: QUESTION, SUGGEST, PERSON, FOLD, PREWARM. A job states its priority
with `with gate.priority(gate.SUGGEST):` around the call; anything that does not is a question.

GIVING WAY. A background job generates token by token and stops at the next token when a more
urgent job arrives; it is then run again once the model is free. The READING of a prompt cannot
be stopped part-way, so a question can wait for the rest of that — at worst about one
512-token chunk, ~1.3 s on an M5, plus whatever of a prewarm is left.

NOTHING IN THE BACKGROUND DURING A CALL. Live captions share the GPU, and a caption running late
matters more than a summary running late. Questions still run.

KEEPING THE ASSISTANT WARM (measured on the M5, Gemma 4 E4B, 2026-09-25). Reading 4,000 tokens
cold took 10.8 s; the same 4,000 with 100 new ones after took 0.39 s, because the engine reuses
a prompt's unchanged start. A background job replaces that memory. So before one runs, the
assistant's state is saved (0.29 s, 229 MB at 4,000 tokens), and it is restored before the next
question (0.54 s) — about 1 s, where re-reading would have been 10.8 s.
"""
from __future__ import annotations

import contextlib
import contextvars
import heapq
import itertools
import logging
import threading

logger = logging.getLogger("secretary.gate")

QUESTION, SUGGEST, PERSON, FOLD, PREWARM = 0, 1, 2, 3, 4

#: Jobs that run in the assistant's own context, so they keep its state rather than replace it.
_ASSISTANT = {QUESTION, PREWARM}

_prio: contextvars.ContextVar[int] = contextvars.ContextVar("llm_priority", default=QUESTION)


@contextlib.contextmanager
def priority(p: int):
    """Run the model calls inside at priority `p`. Carried into `asyncio.to_thread`."""
    token = _prio.set(p)
    try:
        yield
    finally:
        _prio.reset(token)


def current() -> int:
    return _prio.get()


class Preempted(Exception):
    """A more urgent job arrived; this one stopped and will be run again."""


class Ticket:
    def __init__(self, prio: int, seq: int):
        self.prio, self.seq = prio, seq
        self.cancel = threading.Event()

    def __lt__(self, other: "Ticket") -> bool:
        return (self.prio, self.seq) < (other.prio, other.seq)


_cv = threading.Condition()
_waiting: list[Ticket] = []
_holder: Ticket | None = None
_seq = itertools.count()


def _call_on() -> bool:
    """A call in progress: live captions running, or a call ringing or live in the app."""
    try:
        from . import live, phone
        return bool(live._calls) or phone._active is not None
    except Exception:
        return False


def question_waiting() -> bool:
    with _cv:
        return any(t.prio == QUESTION for t in _waiting)


def acquire(prio: int) -> Ticket:
    """Wait for the model. Blocks; call from a worker thread, never the event loop."""
    global _holder
    t = Ticket(prio, next(_seq))
    with _cv:
        heapq.heappush(_waiting, t)
        if _holder is not None and _holder.prio > prio:
            _holder.cancel.set()
        while True:
            if (_holder is None and _waiting[0] is t
                    and not (prio != QUESTION and _call_on())):
                heapq.heappop(_waiting)
                _holder = t
                return t
            # A TIMEOUT, not only a notify: "is a call on?" changes with nobody to notify.
            _cv.wait(timeout=1.0)


def release(t: Ticket) -> None:
    global _holder
    with _cv:
        if _holder is t:
            _holder = None
        _cv.notify_all()


# ---- the assistant's saved state -----------------------------------------------------------

_owner = ""                 # "assistant" | "background" — whose memory the engine holds now
_saved = None               # the assistant's state, while a background job has the engine
_saved_for = 0              # id() of the engine it was saved from


def before(engine, prio: int) -> None:
    """Called with the model held, before a job uses it: keep the assistant's memory."""
    global _owner, _saved, _saved_for
    try:
        if prio in _ASSISTANT:
            if _owner == "background" and _saved is not None and _saved_for == id(engine):
                engine.load_state(_saved)
            _saved = None
            _owner = "assistant"
        else:
            if _owner == "assistant":
                _saved, _saved_for = engine.save_state(), id(engine)
            _owner = "background"
    except Exception as exc:        # a lost state costs one cold read, never a failed job
        logger.info("model state not kept (%s: %s)", type(exc).__name__, exc)
        _saved, _owner = None, ""
