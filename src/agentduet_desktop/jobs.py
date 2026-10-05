"""Background model work: ONE pending job per subject, run behind the owner's questions.

`gate` ranks and serialises calls to the model, but it does not know what a call is FOR, so
asking twice for the same summary would run it twice. This layer does know:

- A job has a KEY — `person:+6591234567`, `fold:assistant`. Requesting a key that is already
  waiting does nothing: the waiting job reads its input when it STARTS, not when it was asked
  for, so it covers everything that arrived meanwhile. Five quick turns become one fold.
- A request for a key that is RUNNING marks it dirty, and it runs exactly once more afterwards.
- The job itself checks its WATERMARK — the last call or turn its summary absorbed — and ends
  without touching the model when nothing is newer. That also covers a restart, and a job that
  gave way to a question and reran.

A FAILING JOB BACKS OFF (2026-10-05). A summary that could not get the model failed, was asked
for again by the next sweep 20 s later, and failed again — 371 times in 40 minutes on a tester's
Mac, keeping the model busy loading on a machine already short of memory. So a key that fails
is not run again until `backoff(n)` has passed: 30 s, doubling, at most 30 minutes. A success
clears it.

One worker thread, most urgent first. The model calls inside a job go through `gate` at the
job's priority, so a question still overtakes it at the next token.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from . import gate

logger = logging.getLogger("secretary.jobs")

_cv = threading.Condition()
_pending: dict[str, tuple[int, Callable[[], None]]] = {}
_dirty: set[str] = set()
_running = ""
_thread: threading.Thread | None = None
#: key -> (failures in a row, not before this time)
_failed: dict[str, tuple[int, float]] = {}

BACKOFF_FIRST, BACKOFF_MAX = 30.0, 1800.0


def backoff(n: int) -> float:
    """Seconds to wait after the n-th failure in a row."""
    return min(BACKOFF_MAX, BACKOFF_FIRST * 2 ** max(0, n - 1))


def request(key: str, prio: int, fn: Callable[[], None]) -> None:
    """Ask for `fn` to run in the background. Coalesces with a waiting or running `key`."""
    global _thread
    with _cv:
        if _failed.get(key, (0, 0.0))[1] > time.time():
            return                         # failed lately: asked again once its wait is over
        if key == _running:
            _dirty.add(key)
        elif key not in _pending:
            _pending[key] = (prio, fn)
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_work, name="model-jobs", daemon=True)
            _thread.start()
        _cv.notify_all()


def pending() -> list[str]:
    with _cv:
        return sorted(_pending) + ([_running] if _running else [])


def _work() -> None:
    global _running
    while True:
        with _cv:
            while not _pending:
                _cv.wait()
            key = min(_pending, key=lambda k: _pending[k][0])
            prio, fn = _pending.pop(key)
            _running = key
        try:
            with gate.priority(prio):
                fn()
            with _cv:
                _failed.pop(key, None)
        except Exception as exc:          # one bad job must not stop the rest
            with _cv:
                n = _failed.get(key, (0, 0.0))[0] + 1
                _failed[key] = (n, time.time() + backoff(n))
                _dirty.discard(key)       # not again straight away, however it was asked for
            logger.warning("job %s failed: %s: %s — not again for %ds", key, type(exc).__name__,
                           exc, int(backoff(n)))
        finally:
            with _cv:
                _running = ""
                if key in _dirty:
                    _dirty.discard(key)
                    _pending.setdefault(key, (prio, fn))
