"""A small decision model for the questions the summary must not get wrong (2026-10-03).

WHAT IT IS. Strands Decider (`StrandsAgents/strands-decider-2B-hobson-v19`, Apache-2.0): Qwen3.5
2B with a LoRA adapter and a pointer head. It does not write text — it picks one of the options
it is given, and every answer carries a calibrated probability. Run here as the q8 ONNX
conversion (`onnx-community/strands-decider-2B-hobson-v19-ONNX`) on ONNX Runtime's CPU provider.

WHY. Two of the summary's checks are judgements code could only approximate by matching words:
whether a call said who the caller is to the owner, and whether a sentence is a lasting fact or
a plan. Measured 2026-10-03 (scratch harness, 22 scripted cases + 16 real calls): who-they-are
7/8 scripted where the word rule got 2/8, and 16/16 real calls with nothing invented. It is
worse than code at dates and at languages, so those stay code's.

A CHILD PROCESS THAT STAYS WHILE IT IS USED (2026-10-03). The model is loaded in
`agentduet-desktop decide`, which answers one JSON request per line on stdin until stdin closes.
It is kept between requests — loading costs ~0.4 s, and summaries come in runs — and ENDED when
the slot needs it gone (slot.py): idle for two minutes, or the speech model claiming the room.
Ending the process is still how its memory (0.5 GB working, 1.8 GB mapped) is certainly returned,
whatever ONNX Runtime's allocator keeps. Measured: unloading in-process left the 0.5 GB allocated.

NEVER REQUIRED. Missing files, a missing runtime, a crash or a timeout all answer None, and the
caller keeps its code rule. Nothing waits on a download.

The prompt rendering below is ported from `strands_decider.prompting` and `infer` (Apache-2.0,
Strands Labs) — the model reads exactly this layout, so it is copied, not paraphrased.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import threading

from . import paths

logger = logging.getLogger("secretary.decider")

REPO = "onnx-community/strands-decider-2B-hobson-v19-ONNX"
#: PINNED: the files below are checked by size, so a new upload must be a deliberate bump.
REVISION = "31a83e0244b26d939ae33f87189d6c8204003e53"
FILES = (("onnx/model_quantized.onnx", 515126),
         ("onnx/model_quantized.onnx_data", 1800931328),
         ("tokenizer.json", 19989592),
         ("config.json", 1806))
MB = sum(size for _, size in FILES) // 2**20

#: The worker's whole budget, load included. A summary waits for nothing, so a slow answer is
#: simply no answer.
TIMEOUT = 90
#: How much of the window a question may take before the state is cut (upstream's default).
QUESTION_FRACTION = 0.75


def folder():
    return paths.HOME / "models" / "decider"


def ready() -> bool:
    """The files are here, whole, and the runtime can be imported."""
    import importlib.util
    if not all((folder() / name).is_file() and (folder() / name).stat().st_size == size
               for name, size in FILES):
        return False
    return all(importlib.util.find_spec(m) is not None for m in ("onnxruntime", "tokenizers"))


_fetching = threading.Lock()


def fetch() -> bool:
    """Download what is missing (resuming a partial file). True when everything is here."""
    from . import models
    if not _fetching.acquire(blocking=False):
        return False
    try:
        for name, size in FILES:
            target = folder() / name
            target.parent.mkdir(parents=True, exist_ok=True)
            models.fetch_file(f"https://huggingface.co/{REPO}/resolve/{REVISION}/{name}", target, size)
        return True
    except Exception as exc:                       # a network or disk failure is retried later
        logger.warning("decider download stopped: %s", exc)
        return False
    finally:
        _fetching.release()


_started = False


def start() -> bool:
    """Start the download now unless it is running or done — the wizard's and Settings' call.
    True when a download is (now) running."""
    if ready():
        return False
    if not _fetching.locked():
        logger.info("fetching the decision model (%d MB) in the background", MB)
        threading.Thread(target=fetch, name="decider-fetch", daemon=True).start()
    return True


def fetch_in_background() -> None:
    """Start the download once per process, when the files are not here. Never waits on it."""
    global _started
    if _started or ready():
        return
    _started = True
    start()


def progress() -> dict:
    """What a progress bar needs, read from disk: whole files plus any partial one."""
    got = 0
    for name, _size in FILES:
        target = folder() / name
        part = target.with_name(target.name + ".part")
        got += target.stat().st_size if target.is_file() else (part.stat().st_size if part.is_file() else 0)
    return {"ready": ready(), "mb": MB, "got_mb": got // 2**20, "running": _fetching.locked()}


# ---- questions -------------------------------------------------------------------------------

def choice(instructions: str, options: dict[str, str]) -> dict:
    """Pick one of `options` ({name: description})."""
    return {"kind": "choice", "instructions": instructions, "options": options}


def yes_no(instructions: str) -> dict:
    return {"kind": "noul", "instructions": instructions,
            "options": {"false": "the statement does not hold for this state",
                        "true": "the statement holds for this state"}}


def _render_question(q: dict) -> tuple[str, list[str], list[tuple[int, int]]]:
    """(text, option names in slot order, each option line's character span in the text)."""
    header = {"noul": "Decide whether the statement is true of the state.",
              "choice": "Select exactly one option."}[q["kind"]]
    lines, spans, cursor = [], [], 0
    for i, (name, desc) in enumerate(q["options"].items()):
        desc = " ".join((desc or "").split())
        line = f"{i + 1}. {name}" + (f" — {desc}" if desc else "")
        lines.append(line)
        spans.append((cursor, cursor + len(line)))
        cursor += len(line) + 1
    prefix = f'<question type="{q["kind"]}">\n{header}\n{q["instructions"].strip()}\n<options>\n'
    text = prefix + "\n".join(lines) + "\n</options>\n</question>\n<answer>"
    base = len(prefix)
    return text, list(q["options"]), [(base + s, base + e) for s, e in spans]


def _render_state(state: str) -> str:
    return f"<state>\n{state.strip()}\n</state>\n"


def _last_token_of(offsets, span) -> int:
    """The last token lying wholly inside an option's line — the one the head reads it from."""
    a, b = span
    last = -1
    for j, (lo, hi) in enumerate(offsets):
        if hi > lo and lo >= a and hi <= b:
            last = j
    if last < 0:
        raise ValueError("an option was truncated away")
    return last


class _Engine:
    """The tokenizer and the ONNX session. Lives only inside the worker process."""

    def __init__(self) -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer
        cfg = json.loads((folder() / "config.json").read_text())["decider"]
        self.max_len = int(cfg.get("max_length", 4096))
        self.temps = cfg["temperature_by_kind"]
        self.tok = Tokenizer.from_file(str(folder() / "tokenizer.json"))
        so = ort.SessionOptions()
        so.log_severity_level = 3
        self.sess = ort.InferenceSession(str(folder() / "onnx" / "model_quantized.onnx"), so,
                                         providers=["CPUExecutionProvider"])

    def ask(self, state: str, q: dict) -> dict:
        import numpy as np
        text, names, spans = _render_question(q)
        enc = self.tok.encode(text, add_special_tokens=False)
        ids, offsets = list(enc.ids), list(enc.offsets)
        # THE QUESTION HAS FIRST CLAIM ON THE WINDOW, cut from the front if it must be: its options
        # and the closing <answer> are what the head reads. The state takes what is left.
        reserve = min(len(ids), max(1, int(self.max_len * QUESTION_FRACTION)))
        cut = len(ids) - reserve
        ids, offsets = ids[cut:], offsets[cut:]
        s = self.tok.encode(_render_state(state), add_special_tokens=True).ids
        s = s[: max(1, self.max_len - reserve)]
        opt = [len(s) + _last_token_of(offsets, span) for span in spans]
        seq = np.array([s + ids], np.int64)
        (logits,) = self.sess.run(None, {"input_ids": seq, "attention_mask": np.ones_like(seq),
                                         "answer_pos": np.array([seq.shape[1] - 1], np.int64),
                                         "option_pos": np.array([opt], np.int64)})
        z = logits[0, : len(names)] / float(self.temps[q["kind"]])
        p = np.exp(z - z.max())
        p = (p / p.sum()).tolist()
        probs = dict(zip(names, p))
        n, top = len(p), max(p)
        # Normalised max-probability (upstream's derive_confidence): uniform 0, certain 1.
        confidence = (n * top - 1) / (n - 1) if n > 1 else 1.0
        if q["kind"] == "noul":
            return {"yes": probs["true"]}
        return {"choice": max(probs, key=probs.get), "confidence": round(confidence, 4),
                "probabilities": {k: round(v, 4) for k, v in probs.items()}}


def serve_stdin() -> int:
    """`agentduet-desktop decide`: one JSON request per LINE on stdin, one answer per line on
    stdout, until stdin closes. The model loads once, on the first request."""
    engine = None
    for line in sys.stdin:
        if not line.strip():
            continue
        req = json.loads(line)
        engine = engine or _Engine()
        out = {name: engine.ask(req.get("state", ""), q)
               for name, q in (req.get("questions") or {}).items()}
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()
    return 0


def _worker() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "decide"]
    return [sys.executable, "-m", "agentduet_desktop.cli", "decide"]


_proc: "subprocess.Popen | None" = None
_lines: "queue.Queue | None" = None
_worker_lock = threading.Lock()


def _start_worker() -> "subprocess.Popen":
    """The worker, and a thread that hands its answers over line by line (so a read can time out)."""
    global _proc, _lines
    log = open(paths.RUN / "decider.log", "a")
    _proc = subprocess.Popen(_worker(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                             text=True, bufsize=1, env={**os.environ, "PYTHONUNBUFFERED": "1"})
    _lines = queue.Queue()
    proc, lines = _proc, _lines

    def pump():
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)                           # the worker ended
    threading.Thread(target=pump, name="decider-out", daemon=True).start()
    return _proc


def _stop_worker() -> None:
    """End the worker — the slot's call (slot.py). Its memory goes with it."""
    global _proc
    with _worker_lock:
        p, _proc = _proc, None
    from . import slot
    slot.released(slot.DECIDER)
    if p is None:
        return
    try:
        p.stdin.close()
        p.wait(timeout=3)
    except Exception:
        p.kill()
    logger.info("decision model unloaded")


def _register_slot() -> None:
    from . import slot
    slot.register(slot.DECIDER, _stop_worker)


_register_slot()


def ask(state: str, questions: dict[str, dict]) -> dict | None:
    """Ask several questions about one state. None when the model is not here, may not run now
    (a call is using the speech model — slot.py), or fails."""
    from . import slot
    if not questions or not ready():
        return None
    # THE SLOT FIRST, outside the worker's lock: claiming it may wait for the speech model to
    # finish a piece, and the speech side claims while holding its own lock.
    if not slot.claim(slot.DECIDER):
        return None
    with _worker_lock:
        try:
            p = _proc if (_proc is not None and _proc.poll() is None) else _start_worker()
            p.stdin.write(json.dumps({"state": state, "questions": questions}) + "\n")
            p.stdin.flush()
            line = _lines.get(timeout=TIMEOUT)
        except (OSError, queue.Empty, ValueError) as exc:
            logger.warning("decider did not answer: %s", type(exc).__name__)
            line = None
        if line is None:
            _kill_locked()
            return None
    slot.touch(slot.DECIDER)
    try:
        return json.loads(line)
    except ValueError:
        logger.warning("decider answered something that is not JSON")
        return None


def _kill_locked() -> None:
    """A worker that failed or hung is ended; the next question starts a fresh one."""
    global _proc
    p, _proc = _proc, None
    if p is not None:
        try:
            p.kill()
        except Exception:
            pass
