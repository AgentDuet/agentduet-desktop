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

One worker thread, most urgent first. The model calls inside a job go through `gate` at the
job's priority, so a question still overtakes it at the next token.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

from . import gate

logger = logging.getLogger("secretary.jobs")

_cv = threading.Condition()
_pending: dict[str, tuple[int, Callable[[], None]]] = {}
_dirty: set[str] = set()
_running = ""
_thread: threading.Thread | None = None


def request(key: str, prio: int, fn: Callable[[], None]) -> None:
    """Ask for `fn` to run in the background. Coalesces with a waiting or running `key`."""
    global _thread
    with _cv:
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
        except Exception as exc:          # one bad job must not stop the rest
            logger.warning("job %s failed: %s: %s", key, type(exc).__name__, exc)
        finally:
            with _cv:
                _running = ""
                if key in _dirty:
                    _dirty.discard(key)
                    _pending.setdefault(key, (prio, fn))
