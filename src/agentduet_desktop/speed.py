"""How fast the local model reads and writes ON THIS MACHINE, measured once per model.

Reading a prompt is bound by the GPU's compute and grows with its length; writing is bound by
memory bandwidth. Both differ several-fold between Macs, so the context budget (`budget.py`) is
derived from what this machine actually did rather than from a constant. Measured on the M5 with
Gemma 4 E4B, 2026-09-25: ~390 tokens/s reading, ~35 tokens/s writing.

HOW. A 1,000-token prompt that starts with a fresh nonce, so none of it is in the engine's
cache, then 32 tokens of output. Reading speed is 1,000 over the time to the first token;
writing speed is 32 over the rest. A few tokens are run first, because the first Metal call of a
process compiles kernels and would make the machine look slower than it is.

WHEN. The first time a model is loaded without a measurement, as a background job: lowest
priority, never during a call, giving way to a question (the measurement is simply dropped and
asked for again the next time the model loads).
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime

from . import gate, jobs, paths

logger = logging.getLogger("secretary.speed")

PROMPT_TOKENS = 1000
WRITE_TOKENS = 32


def _file():
    return paths.RUN / "model_speed.json"


def _all() -> dict:
    try:
        return json.loads(_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def of(model: str) -> dict:
    """{"read_tps", "write_tps", "measured"} for `model` on this machine, or {}."""
    return _all().get(model, {})


def _save(model: str, rec: dict) -> None:
    d = _all()
    d[model] = rec
    f = _file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(d, indent=1), encoding="utf-8")


def measure(model: str) -> dict:
    """Time one cold read and a short write. Returns the record, or {} if it gave way."""
    from . import models
    if of(model):
        return of(model)
    engine, why = models.load(model)
    if engine is None:
        return {}
    ticket = gate.acquire(gate.PREWARM)
    try:
        gate.before(engine, gate.FOLD)            # keeps the assistant's state, like any job
        for _ in engine.generate(engine.tokenize(b"Hello there."), temp=0.0):
            break                                 # warm the kernels
        filler = uuid.uuid4().hex + " " + " ".join(f"note{i} about item {i}." for i in range(900))
        toks = engine.tokenize(filler.encode())[:PROMPT_TOKENS]
        t0, first, n = time.time(), None, 0
        for _ in engine.generate(toks, temp=0.0):
            if ticket.cancel.is_set():
                return {}
            if first is None:
                first = time.time()
            n += 1
            if n > WRITE_TOKENS:
                break
        t1 = time.time()
        rec = {"read_tps": round(len(toks) / (first - t0)),
               "write_tps": round(WRITE_TOKENS / (t1 - first), 1),
               "measured": datetime.now().isoformat(timespec="seconds")}
        _save(model, rec)
        logger.info("%s on this machine: reads %d tokens/s, writes %.1f tokens/s",
                    model, rec["read_tps"], rec["write_tps"])
        return rec
    finally:
        gate.release(ticket)


def request(model: str) -> None:
    if model and not of(model):
        jobs.request("measure:" + model, gate.PREWARM, lambda: measure(model))
