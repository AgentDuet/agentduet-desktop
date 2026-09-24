"""The local models we offer, and the files behind them.

WHY THIS EXISTS RATHER THAN OLLAMA. Ollama made model management free, and cost the one thing
this product sells: an owner who downloads one binary and it works. Asking someone to install a
second program before their phone can be answered is the hosting decision we already removed,
wearing a different hat. So the weights are ours to fetch, store and run.

THE SHAPE OF THIS CATALOGUE IS FROM KC's `hardware.py` (PR #3,
okchoong:feature-model-adviser-and-unload) — repo, exact GGUF filename, sizes, a description of
what each model is FOR. The entries themselves were replaced on 2026-08-27: his were Qwen2.5,
Gemma 2, Llama 3.2, Phi 3.5 and Mistral v0.3, which were current when written and are a
generation behind now.

EVERY SIZE HERE WAS READ FROM THE HUGGING FACE API, not from memory — `dl_mb` is the actual
byte count of that exact file. The model landscape moves faster than any assistant's training
data, so the list was rebuilt by asking rather than by recalling.

WHAT EACH FIGURE MEANS, because two of them look alike and are not:
  dl_mb   what lands on disk — real, and what the progress bar counts to.
  ram_mb  what it occupies once loaded. DERIVED, not measured: dl_mb x WORKING_SET. Measuring
          it honestly would mean downloading twenty-two gigabytes and loading each one, so this
          is the same arithmetic machine.py has always used for a model nobody has profiled.
          Treat it as the estimate it is.

NO SPEED COLUMN. The old catalogue carried tokens/sec per model and this one does not, because
nobody here has run these on this hardware and a number that looks measured and is not is worse
than an absent one. It comes back when someone times them.

THREE STATES, NOT TWO. A model is absent, or on disk, or resident in memory. `machine.py` says
whether one COULD run here; this says which of the three it is right now. Conflating the last
two is how a laptop ends up holding five gigabytes for a model nobody is using.
"""

import json
import logging
import os
import pathlib
import threading
import time
import urllib.parse
import urllib.request

from . import machine, paths

logger = logging.getLogger("dduet.models")

#: Where weights live. Inside the instance, never the install directory — an upgrade replaces
#: that wholesale, and re-downloading gigabytes because the app updated is not acceptable.
STORE = paths.HOME / "models"

#: Disk headroom over the download size. A filesystem with exactly the file's size free is a
#: filesystem that fills during the write.
DISK_HEADROOM = 1.25

CATALOGUE = {
    "glm-edge-1.5b": dict(
        name="GLM Edge 1.5B", brand="GLM", params="1.5B",
        dl_mb=935, ram_mb=1215,
        repo="zai-org/glm-edge-1.5b-chat-gguf",
        filename="ggml-model-Q4_K_M.gguf",
        url="https://huggingface.co/zai-org/glm-edge-1.5b-chat-gguf/resolve/main/ggml-model-Q4_K_M.gguf",
        what="Z.ai's open family, the third Chinese lab worth carrying after Qwen and DeepSeek. Strong on structured output and long documents."),
    "glm-edge-4b": dict(
        name="GLM Edge 4B", brand="GLM", params="4B",
        dl_mb=2505, ram_mb=3256,
        repo="zai-org/glm-edge-4b-chat-gguf",
        filename="ggml-model-Q4_K_M.gguf",
        url="https://huggingface.co/zai-org/glm-edge-4b-chat-gguf/resolve/main/ggml-model-Q4_K_M.gguf",
        what="Z.ai's open family, the third Chinese lab worth carrying after Qwen and DeepSeek. Strong on structured output and long documents."),
    "glm-4-9b": dict(
        name="GLM 4 9B", brand="GLM", params="9B",
        dl_mb=5880, ram_mb=7644,
        repo="unsloth/GLM-4-9B-0414-GGUF",
        filename="GLM-4-9B-0414-Q4_K_M.gguf",
        url="https://huggingface.co/unsloth/GLM-4-9B-0414-GGUF/resolve/main/GLM-4-9B-0414-Q4_K_M.gguf",
        what="Z.ai's open family, the third Chinese lab worth carrying after Qwen and DeepSeek. Strong on structured output and long documents."),
    "qwen3-0.6b": dict(
        name="Qwen3 0.6B", brand="QWEN", params="0.6B",
        dl_mb=378, ram_mb=491,
        repo="unsloth/Qwen3-0.6B-GGUF",
        filename="Qwen3-0.6B-Q4_K_M.gguf",
        url="https://huggingface.co/unsloth/Qwen3-0.6B-GGUF/resolve/main/Qwen3-0.6B-Q4_K_M.gguf",
        what="Multilingual and even-handed. The safest default if your calls are not all in English."),
    "qwen3-1.7b": dict(
        name="Qwen3 1.7B", brand="QWEN", params="1.7B",
        dl_mb=1056, ram_mb=1372,
        repo="unsloth/Qwen3-1.7B-GGUF",
        filename="Qwen3-1.7B-Q4_K_M.gguf",
        url="https://huggingface.co/unsloth/Qwen3-1.7B-GGUF/resolve/main/Qwen3-1.7B-Q4_K_M.gguf",
        what="Multilingual and even-handed. The safest default if your calls are not all in English."),
    "qwen3-4b": dict(
        name="Qwen3 4B", brand="QWEN", params="4B",
        dl_mb=2381, ram_mb=3095,
        repo="Qwen/Qwen3-4B-GGUF",
        filename="Qwen3-4B-Q4_K_M.gguf",
        url="https://huggingface.co/Qwen/Qwen3-4B-GGUF/resolve/main/Qwen3-4B-Q4_K_M.gguf",
        what="Multilingual and even-handed. The safest default if your calls are not all in English."),
    "qwen3-8b": dict(
        name="Qwen3 8B", brand="QWEN", params="8B",
        dl_mb=4794, ram_mb=6232,
        repo="Qwen/Qwen3-8B-GGUF",
        filename="Qwen3-8B-Q4_K_M.gguf",
        url="https://huggingface.co/Qwen/Qwen3-8B-GGUF/resolve/main/Qwen3-8B-Q4_K_M.gguf",
        what="Multilingual and even-handed. The safest default if your calls are not all in English."),
    "qwen3-14b": dict(
        name="Qwen3 14B", brand="QWEN", params="14B",
        dl_mb=8584, ram_mb=11159,
        repo="Qwen/Qwen3-14B-GGUF",
        filename="Qwen3-14B-Q4_K_M.gguf",
        url="https://huggingface.co/Qwen/Qwen3-14B-GGUF/resolve/main/Qwen3-14B-Q4_K_M.gguf",
        what="Multilingual and even-handed. The safest default if your calls are not all in English."),
    "deepseek-r1-1.5b": dict(
        name="DeepSeek R1 Distill 1.5B", brand="DEEPSEEK", params="1.5B",
        dl_mb=1065, ram_mb=1384,
        repo="unsloth/DeepSeek-R1-Distill-Qwen-1.5B-GGUF",
        filename="DeepSeek-R1-Distill-Qwen-1.5B-Q4_K_M.gguf",
        url="https://huggingface.co/unsloth/DeepSeek-R1-Distill-Qwen-1.5B-GGUF/resolve/main/DeepSeek-R1-Distill-Qwen-1.5B-Q4_K_M.gguf",
        what="Reasoning distilled from R1. Thinks before answering — better on multi-step work, slower and more verbose."),
    "deepseek-r1-7b": dict(
        name="DeepSeek R1 Distill 7B", brand="DEEPSEEK", params="7B",
        dl_mb=4466, ram_mb=5805,
        repo="bartowski/DeepSeek-R1-Distill-Qwen-7B-GGUF",
        filename="DeepSeek-R1-Distill-Qwen-7B-Q4_K_M.gguf",
        url="https://huggingface.co/bartowski/DeepSeek-R1-Distill-Qwen-7B-GGUF/resolve/main/DeepSeek-R1-Distill-Qwen-7B-Q4_K_M.gguf",
        what="Reasoning distilled from R1. Thinks before answering — better on multi-step work, slower and more verbose."),
    "deepseek-r1-14b": dict(
        name="DeepSeek R1 Distill 14B", brand="DEEPSEEK", params="14B",
        dl_mb=8571, ram_mb=11142,
        repo="unsloth/DeepSeek-R1-Distill-Qwen-14B-GGUF",
        filename="DeepSeek-R1-Distill-Qwen-14B-Q4_K_M.gguf",
        url="https://huggingface.co/unsloth/DeepSeek-R1-Distill-Qwen-14B-GGUF/resolve/main/DeepSeek-R1-Distill-Qwen-14B-Q4_K_M.gguf",
        what="Reasoning distilled from R1. Thinks before answering — better on multi-step work, slower and more verbose."),
    "gemma-3-270m": dict(
        name="Gemma 3 270M", brand="GOOGLE", params="270M",
        dl_mb=241, ram_mb=313,
        repo="unsloth/gemma-3-270m-it-GGUF",
        filename="gemma-3-270m-it-Q4_K_M.gguf",
        url="https://huggingface.co/unsloth/gemma-3-270m-it-GGUF/resolve/main/gemma-3-270m-it-Q4_K_M.gguf",
        what="Google's open family. Concise and well-behaved on summarising and rewriting."),
    "gemma-3-1b": dict(
        name="Gemma 3 1B", brand="GOOGLE", params="1B",
        dl_mb=957, ram_mb=1244,
        repo="google/gemma-3-1b-it-qat-q4_0-gguf",
        filename="gemma-3-1b-it-q4_0.gguf",
        url="https://huggingface.co/google/gemma-3-1b-it-qat-q4_0-gguf/resolve/main/gemma-3-1b-it-q4_0.gguf",
        what="Google's open family. Concise and well-behaved on summarising and rewriting."),
    "gemma-3-4b": dict(
        name="Gemma 3 4B", brand="GOOGLE", params="4B",
        dl_mb=2374, ram_mb=3086,
        repo="unsloth/gemma-3-4b-it-GGUF",
        filename="gemma-3-4b-it-Q4_K_M.gguf",
        url="https://huggingface.co/unsloth/gemma-3-4b-it-GGUF/resolve/main/gemma-3-4b-it-Q4_K_M.gguf",
        what="Google's open family. Concise and well-behaved on summarising and rewriting."),
    "gemma-3-12b": dict(
        name="Gemma 3 12B", brand="GOOGLE", params="12B",
        dl_mb=6962, ram_mb=9050,
        repo="unsloth/gemma-3-12b-it-GGUF",
        filename="gemma-3-12b-it-Q4_K_M.gguf",
        url="https://huggingface.co/unsloth/gemma-3-12b-it-GGUF/resolve/main/gemma-3-12b-it-Q4_K_M.gguf",
        what="Google's open family. Concise and well-behaved on summarising and rewriting."),
    "llama-3.2-1b": dict(
        name="Llama 3.2 1B", brand="META", params="1.2B",
        dl_mb=770, ram_mb=1001,
        repo="bartowski/Llama-3.2-1B-Instruct-GGUF",
        filename="Llama-3.2-1B-Instruct-Q4_K_M.gguf",
        url="https://huggingface.co/bartowski/Llama-3.2-1B-Instruct-GGUF/resolve/main/Llama-3.2-1B-Instruct-Q4_K_M.gguf",
        what="Meta's instruct family. Widely used and predictable; the small ones summarise better than they decide."),
    "llama-3.2-3b": dict(
        name="Llama 3.2 3B", brand="META", params="3.2B",
        dl_mb=1925, ram_mb=2502,
        repo="bartowski/Llama-3.2-3B-Instruct-GGUF",
        filename="Llama-3.2-3B-Instruct-Q4_K_M.gguf",
        url="https://huggingface.co/bartowski/Llama-3.2-3B-Instruct-GGUF/resolve/main/Llama-3.2-3B-Instruct-Q4_K_M.gguf",
        what="Meta's instruct family. Widely used and predictable; the small ones summarise better than they decide."),
    "ministral-3-3b": dict(
        name="Ministral 3 3B", brand="MISTRAL", params="3B",
        dl_mb=2047, ram_mb=2661,
        repo="lmstudio-community/Ministral-3-3B-Instruct-2512-GGUF",
        filename="Ministral-3-3B-Instruct-2512-Q4_K_M.gguf",
        url="https://huggingface.co/lmstudio-community/Ministral-3-3B-Instruct-2512-GGUF/resolve/main/Ministral-3-3B-Instruct-2512-Q4_K_M.gguf",
        what="Terse and even-tempered, with a long context window — a whole transcript fits."),
    "ministral-3-8b": dict(
        name="Ministral 3 8B", brand="MISTRAL", params="8B",
        dl_mb=4957, ram_mb=6444,
        repo="lmstudio-community/Ministral-3-8B-Instruct-2512-GGUF",
        filename="Ministral-3-8B-Instruct-2512-Q4_K_M.gguf",
        url="https://huggingface.co/lmstudio-community/Ministral-3-8B-Instruct-2512-GGUF/resolve/main/Ministral-3-8B-Instruct-2512-Q4_K_M.gguf",
        what="Terse and even-tempered, with a long context window — a whole transcript fits."),
    "ministral-3-14b": dict(
        name="Ministral 3 14B", brand="MISTRAL", params="14B",
        dl_mb=7857, ram_mb=10214,
        repo="lmstudio-community/Ministral-3-14B-Instruct-2512-GGUF",
        filename="Ministral-3-14B-Instruct-2512-Q4_K_M.gguf",
        url="https://huggingface.co/lmstudio-community/Ministral-3-14B-Instruct-2512-GGUF/resolve/main/Ministral-3-14B-Instruct-2512-Q4_K_M.gguf",
        what="Terse and even-tempered, with a long context window — a whole transcript fits."),
    "gpt-oss-20b": dict(
        name="GPT-OSS 20B", brand="OPENAI", params="21B MoE",
        dl_mb=11086, ram_mb=14411,
        repo="unsloth/gpt-oss-20b-GGUF",
        filename="gpt-oss-20b-Q4_K_M.gguf",
        url="https://huggingface.co/unsloth/gpt-oss-20b-GGUF/resolve/main/gpt-oss-20b-Q4_K_M.gguf",
        what="OpenAI's open-weight model. A mixture of experts, so it runs far faster than its size suggests — but the whole file still has to fit in memory."),
}


# ---- what is on disk -----------------------------------------------------------------------

#: Families that REASON OUT LOUD by default — they emit a <think> monologue before answering.
#: Prefixes rather than a flag on every catalogue row: it is a property of the family, and a
#: hand-maintained per-model boolean is one more thing to forget when a model is added.
REASONING_FAMILIES = ("qwen3-", "deepseek-r1-")


def thinks(model: str) -> bool:
    """Does this model monologue before answering?

    Measured on an M5 (docs/experiments/local-model-speed.md): Qwen3 8B took 10.84s per turn
    thinking and 1.50s with it suppressed — 7.2x — because it wrote 237 tokens where 18 were
    needed. On "Hi, are you there?" that was 454 tokens of <think> and 41 characters of reply.
    """
    return model.lower().startswith(REASONING_FAMILIES)


def spec_of(model: str) -> dict | None:
    """The catalogue entry, or a custom one the owner added. Curated wins on a name clash."""
    return CATALOGUE.get(model) or custom().get(model)


def path_of(model: str) -> pathlib.Path | None:
    """Where this model's weights would live, or None if we do not know it."""
    spec = spec_of(model)
    return (STORE / model / spec["filename"]) if spec else None


def is_downloaded(model: str) -> bool:
    p = path_of(model)
    return bool(p and p.is_file() and p.stat().st_size > 0)


def disk_free_mb() -> int:
    """Free space where the weights go. The store may not exist yet, so ask its nearest parent."""
    p = STORE
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        st = os.statvfs(p)
        return int(st.f_bavail * st.f_frsize / 1024 / 1024)
    except (OSError, AttributeError):
        try:
            import shutil
            return int(shutil.disk_usage(p).free / 1024 / 1024)
        except Exception as exc:
            logger.debug("could not read free disk at %s: %s", p, exc)
            return 0


def can_download(model: str) -> tuple[bool, str]:
    """Is there room for the FILE. Separate from whether it could then run — a laptop with a
    big disk and little memory can hold a model it cannot load, and saying so at the point of
    download is kinder than a five-gigabyte fetch that ends in a refusal."""
    spec = spec_of(model)
    if not spec:
        return False, "not a model we know"
    free = disk_free_mb()
    if not free:
        return True, ""                      # unknown: do not invent an obstacle
    need = int(spec["dl_mb"] * DISK_HEADROOM)
    if free < need:
        return False, f"needs about {need / 1024:.1f} GB free, and {free / 1024:.1f} GB is left"
    return True, ""


def can_run(model: str) -> tuple[str, str]:
    """(fits|tight|no|unknown, why) — the RESIDENT size against this machine, via machine.py.
    The catalogue's ram_mb is measured, so it is used directly rather than re-derived from the
    download size the way an unknown Ollama name has to be."""
    spec = spec_of(model)
    if not spec:
        return "unknown", ""
    return machine.verdict(spec["ram_mb"] / 1024 / machine.WORKING_SET)


def best_of(brand: str) -> str:
    """The largest weight of this family this machine can hold, or the smallest if none fit.

    WHICH FAMILY IS A PREFERENCE — someone knows Llama, or trusts Mistral. WHICH WEIGHT IS AN
    ARITHMETIC QUESTION about their machine, and it has a right answer we can compute. Showing
    every variant made the owner do both, and gave whichever vendor we happened to carry most
    variants of the most shelf space.

    Falling back to the smallest rather than nothing is deliberate: a family that cannot run
    here should still be VISIBLE, saying what it would need. Hiding it answers a question the
    owner did not ask ("would this work on a bigger machine?") by pretending the option does
    not exist.
    """
    mine = [(n, sp) for n, sp in CATALOGUE.items() if sp["brand"] == brand]
    if not mine:
        return ""
    ok = [(n, sp) for n, sp in mine if can_run(n)[0] in ("fits", "tight")]
    pool = ok or mine
    return max(pool, key=lambda t: t[1]["ram_mb"])[0] if ok else \
        min(pool, key=lambda t: t[1]["ram_mb"])[0]


def families() -> list[str]:
    """One id per family, alphabetical by brand. What the curated list actually shows."""
    return [best_of(b) for b in sorted({sp["brand"] for sp in CATALOGUE.values()})]


def variants(model: str) -> list[dict]:
    """Every weight of this model's family, smallest first — the sizes behind one row."""
    spec = CATALOGUE.get(model)
    if not spec:
        return []
    out = []
    for name, sp in sorted(CATALOGUE.items(), key=lambda kv: kv[1]["ram_mb"]):
        if sp["brand"] != spec["brand"]:
            continue
        fit, why = can_run(name)
        out.append({"id": name, "name": sp["name"], "params": sp.get("params", ""),
                    "dl_gb": round(sp["dl_mb"] / 1024, 1),
                    "ram_gb": round(sp["ram_mb"] / 1024, 1),
                    "fit": fit, "why": why, "state": state(name)})
    return out


# ---- fetching ------------------------------------------------------------------------------
#
# ONE DOWNLOAD AT A TIME, and its progress readable from anywhere. The page polls; the fetch
# runs on a worker thread. A per-request object would leave the page unable to answer "how far
# along is it?" after a reload, which on a five-gigabyte file is the whole question.

#: A `.part` untouched for longer than this is an interrupted download, not a running one. Long
#: enough to cover a slow chunk on a bad connection; short enough that a genuinely dead fetch
#: stops blocking the page within a minute.
STALE_PART_SECONDS = 90

#: Why the last attempt at each model failed, kept AFTER the attempt is over so the page can
#: say what went wrong. Without it a failed download is indistinguishable from one that never
#: started: the progress bar simply never appears. That is exactly how a9's broken TLS
#: presented — every fetch died on a missing CA bundle, `download()` returned the reason, and
#: nothing anywhere read it.
_failed: dict[str, str] = {}


def failure(model: str) -> str:
    """Why the last attempt at `model` failed, or ''."""
    with _lock:
        return _failed.get(model, "")


def note_failure(model: str, why: str) -> None:
    with _lock:
        _failed[model] = why


def forget_failure(model: str) -> None:
    """Clear it — called when a retry starts, so a stale reason cannot outlive its cause."""
    with _lock:
        _failed.pop(model, None)


_lock = threading.Lock()

#: TWO AT A TIME, and the reason is not speed. One connection split three ways makes every
#: download slower and none of them finish sooner. What a cap actually buys is disk: the
#: `can_download` check below is PER MODEL, so three 5 GB fetches each pass it individually and
#: together fill the volume. Two also keeps things moving when one stalls.
MAX_CONCURRENT_DOWNLOADS = 2

#: Model -> live progress, one entry per RUNNING fetch. It was a single global slot until
#: 2026-09-08, which meant asking for a second model answered "Already downloading qwen3-8b" —
#: a refusal where the owner meant "and this one too".
_jobs: dict[str, dict] = {}

#: Models asked for while both slots were busy, in the order they were asked for. A queued
#: model MUST say so on its card: a queue nobody can see is the same failure as the silent
#: download this whole area has been fixing all day.
_waiting: list[str] = []


def slots() -> tuple[int, int]:
    """(running, waiting) — what the page needs to explain itself."""
    with _lock:
        return len(_jobs), len(_waiting)


def queued() -> list[str]:
    """Models waiting for a slot, in turn order."""
    with _lock:
        return list(_waiting)


def _percent(done_mb: int, total_mb: int) -> int:
    return int(done_mb * 100 / total_mb) if total_mb else 0


def jobs() -> dict:
    """Every fetch this process is running: model -> progress."""
    with _lock:
        out = {m: dict(j) for m, j in _jobs.items()}
    for m, j in out.items():
        j["model"] = m
        j["percent"] = _percent(j["done_mb"], j["total_mb"])
    return out


def jobs_seen() -> dict:
    """`jobs()`, plus any fetch visible only as a growing `.part` on disk.

    A download started anywhere else — `agentduet-desktop models download`, or the detached
    child `init` spawns — is invisible to `jobs()`, which lives in the memory of whoever is
    fetching. The page showing "not downloaded" while a 4.7 GB file grew beside it reads as the
    app being unable to see its own models.

    Those entries are marked `elsewhere`, because Stop sets a flag THIS process reads, which
    does nothing to another one's fetch — so offering the button would be a lie.

    IN FLIGHT MEANS GROWING. `downloading()` answers "is there a partial file", which is right
    for "how far along is this" and wrong for "is a fetch live". A `.part` abandoned days ago
    satisfies the first and not the second, and treating it as live is what silently disabled
    every download button until 2026-09-08.
    """
    out = jobs()
    for name, done_mb, total_mb in downloading():
        if name in out:
            continue
        target = path_of(name)
        if not target:
            continue
        part = target.with_suffix(target.suffix + ".part")
        try:
            if time.time() - part.stat().st_mtime > STALE_PART_SECONDS:
                continue
        except OSError:
            continue
        out[name] = {"model": name, "done_mb": done_mb, "total_mb": total_mb, "error": "",
                     "cancel": False, "percent": _percent(done_mb, total_mb),
                     "elsewhere": True}
    return out


def downloading() -> list[tuple[str, int, int]]:
    """(model, MB on disk, MB total) for every partial file, READ FROM DISK.

    Unlike `jobs()`, which lives in the memory of whichever process is fetching. That
    distinction is the whole point: `init` starts the download in a DETACHED child and exits, so
    nothing about it is visible in-process afterwards — but the `.part` file is right there, and
    its size against the catalogue's `dl_mb` is the honest answer to "how far along is it".

    A `.part` left by an interrupted fetch reports the same way, which is correct: the next
    attempt resumes from it, so "partially downloaded" IS its state. Whether it is RUNNING is a
    different question — see `jobs_seen()`, which additionally requires the file to be growing.
    """
    out = []
    for name, spec in CATALOGUE.items():
        target = path_of(name)
        if not target:
            continue
        part = target.with_suffix(target.suffix + ".part")
        try:
            if part.is_file():
                out.append((name, int(part.stat().st_size / 1024 / 1024), int(spec["dl_mb"])))
        except OSError:
            continue
    return out


def partial_mb(model: str) -> int:
    """MB already on disk for an unfinished `model`, or 0. What makes a button say Resume."""
    for name, got, _total in downloading():
        if name == model:
            return got
    return 0


#: THE ONE-ENTRY QUEUE, and it is a different queue from `_waiting`. That one is "which
#: downloads are pending"; this is "which model should become the model IN USE when its bytes
#: land". Downloading is not choosing, so "Use this" on a model that has not arrived has to mean
#: something other than nothing happening.
#:
#: One entry, newest wins. A list would change the model in use repeatedly as unrelated
#: downloads finished, in an order set by file size rather than by the owner.
_wanted = ""


def want(model: str) -> str:
    """Queue `model` to become the model in use when its download finishes. Replaces any
    previous want. Pass "" to drop the intent without touching the download."""
    global _wanted
    with _lock:
        was, _wanted = _wanted, model
    if not model:
        return "No model is queued to be used."
    if was and was != model:
        logger.info("queued %s to be used when it lands (replacing %s)", model, was)
    return f"{(spec_of(model) or {}).get('name', model)} will be used when it finishes."


def wanted() -> str:
    """Which model is queued to be used when it lands, or ''."""
    with _lock:
        return _wanted


def claim_wanted(model: str) -> bool:
    """True if `model` is the queued one — and clears it, so it fires once.

    Checked at COMPLETION, not captured when the download started. That is the whole mechanism:
    a want expressed while the bytes were arriving still counts, and one replaced since does not.
    """
    global _wanted
    with _lock:
        if _wanted != model:
            return False
        _wanted = ""
        return True


def cancel(model: str = "") -> str:
    """Ask one download to stop — or every one of them, with no argument.

    Cancels a QUEUED model too, which is the case a running-only version gets wrong: a model
    waiting for a slot has no job to flag, so "Stop" on it did nothing at all.

    The partial file is KEPT: the next attempt resumes from it, which on a 4.6 GB fetch over a
    hotel connection is the difference between an interruption and starting again.
    """
    with _lock:
        running = [m for m in _jobs if not model or m == model]
        for m in running:
            _jobs[m]["cancel"] = True
        dropped = [m for m in _waiting if not model or m == model]
        for m in dropped:
            _waiting.remove(m)
    touched = running + dropped
    if not touched:
        return f"{model} is not downloading." if model else "Nothing is downloading."
    for m in touched:
        if wanted() == m:
            want("")
    return "Stopping " + ", ".join(touched) + "."


def download(model: str) -> str:
    """Fetch the weights. BLOCKING and slow — gigabytes — so call it off the event loop.

    WAITS FOR A SLOT rather than refusing. At most `MAX_CONCURRENT_DOWNLOADS` run at once and
    the rest queue in the order they were asked for, so a third request is "in a moment", not
    "no". Only a second fetch of the SAME model is refused — two writers on one `.part` file.

    Resumes from a previous partial file with a Range request. Writes to `<name>.part` and
    renames only on success, so an interrupted download can never be mistaken for a usable model
    by `is_downloaded`.
    """
    spec = spec_of(model)
    if not spec:
        return f"{model} is not a model we know."
    ok, why = can_download(model)
    if not ok:
        return f"Cannot download {spec['name']}: {why}."

    with _lock:
        if model in _jobs:
            return f"Already downloading {spec['name']}."
        if model in _waiting:
            return f"{spec['name']} is already waiting for a slot."
        _waiting.append(model)

    # FAIR AND CANCELLABLE. Whoever has waited longest goes next, and a model cancelled while
    # queued leaves without ever having opened a connection — `cancel()` removes it from
    # `_waiting`, which is what this notices.
    while True:
        with _lock:
            if model not in _waiting:
                return f"Stopped before {spec['name']} started."
            if len(_jobs) < MAX_CONCURRENT_DOWNLOADS and _waiting[0] == model:
                _waiting.remove(model)
                _jobs[model] = {"done_mb": 0, "total_mb": spec["dl_mb"], "error": "",
                                "cancel": False}
                break
        time.sleep(0.25)

    def _note(**kw) -> None:
        with _lock:
            if model in _jobs:
                _jobs[model].update(**kw)

    def _stopping() -> bool:
        with _lock:
            return bool(_jobs.get(model, {}).get("cancel"))

    target = path_of(model)
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(target.suffix + ".part")
    have = part.stat().st_size if part.is_file() else 0

    try:
        req = urllib.request.Request(spec["url"], headers={"User-Agent": "agentduet-desktop"})
        if have:
            req.add_header("Range", f"bytes={have}-")
        with urllib.request.urlopen(req, timeout=60) as resp:
            # A server that ignores Range answers 200 and sends the whole file; trusting the
            # header instead of the status would append it to what we already had.
            resuming = resp.status == 206
            if not resuming:
                have = 0
            total = int(resp.headers.get("Content-Length") or 0) + have
            _note(total_mb=int(total / 1024 / 1024) or spec["dl_mb"],
                  done_mb=int(have / 1024 / 1024))
            with open(part, "ab" if resuming else "wb") as out:
                got = have
                while True:
                    if _stopping():
                        return f"Stopped. {int(got / 1024 / 1024)} MB kept — it will resume."
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    got += len(chunk)
                    _note(done_mb=int(got / 1024 / 1024))
        part.replace(target)
        logger.info("downloaded %s (%.1f GB)", model, target.stat().st_size / 1024**3)
        return f"Downloaded {spec['name']}."
    except Exception as exc:
        _note(error=f"{type(exc).__name__}: {exc}")
        logger.warning("download of %s failed: %s", model, exc)
        return f"Could not download {spec['name']}: {exc}"
    finally:
        # THE ERROR MUST OUTLIVE THE JOB. Dropping the slot without keeping the reason is how a
        # failed fetch became "the progress bar never appeared" — see `_failed` above.
        with _lock:
            j = _jobs.pop(model, None)
        if j and j.get("error"):
            note_failure(model, j["error"])


def delete(model: str) -> str:
    """Remove the weights, freeing the disk. Refuses the model in use, which is the failure
    `attach_model` exists to prevent — a configured-but-absent model answers nothing."""
    if model == os.getenv("SECRETARY_MODEL"):
        return f"{model} is the model in use. Choose another one first."
    p = path_of(model)
    if not p or not p.parent.is_dir():
        return f"{model} is not downloaded."
    freed = sum(f.stat().st_size for f in p.parent.rglob("*") if f.is_file())
    import shutil
    shutil.rmtree(p.parent, ignore_errors=True)
    if loaded() == model:
        unload()
    forget_custom(model)
    return f"Deleted {(spec_of(model) or {}).get('name', model)}, freeing {freed / 1024**3:.1f} GB."


# ---- residency -----------------------------------------------------------------------------
#
# THE THIRD STATE, and the one a two-state design silently gets wrong. Loading a 7B model holds
# about 5.4 GB for as long as the process lives. On a laptop that is the difference between a
# machine that feels fine and one that swaps while its owner is doing something else, so it has
# to be endable without deleting the file and downloading it again.

_engine = None                 # the live llama_cpp.Llama, or None
_engine_model = ""


def loaded() -> str:
    """Which model is resident right now, or ''."""
    return _engine_model


def available() -> tuple[bool, str]:
    """Whether local inference can run at all in this build."""
    import importlib.util
    if importlib.util.find_spec("llama_cpp") is None:
        return False, ("local models are not in this build — the hosted providers still work, "
                       "and calls are carried and recorded without any model at all")
    return True, ""


def _gpu_layers(model: str) -> tuple[int, str]:
    """How many layers to hand the GPU, and why. THE DEFAULT IS ZERO, which is the whole point.

    Building with Metal or CUDA only makes offload POSSIBLE. llama-cpp-python still runs every
    layer on the CPU unless `n_gpu_layers` is set, so build.yml has been paying extra minutes to
    compile Metal on macOS — and claiming in its own comment that "local models use the GPU
    instead of only the CPU" — while nothing ever asked for a single layer. Compiled-in is not
    used.

    The three cases from machine.gpu() genuinely differ:

    apple  ALL of them. Unified memory means the GPU draws on the same pool that ram_mb already
           accounts for, so there is no second budget to overflow and nothing to be careful
           about.
    cuda   All, but only when the card has room for the resident size plus headroom. A discrete
           GPU is the one case where a model can be too big for the CARD rather than the
           machine, and asking for more than fits fails at load instead of falling back.
    none   Zero, and nothing is lost — there is nowhere to put them.

    AND THE BUILD HAS TO BE ABLE TO. A GPU in the machine is not a GPU the engine can reach:
    Windows and Linux ship the `/whl/cpu` wheel, so every layer runs on the CPU there whatever
    card is fitted. This used to answer -1 for any NVIDIA machine and log "GPU (N GB VRAM)" — a
    log line claiming the card was in use, on a build with no way to use it.
    """
    g = machine.gpu()
    kind = g.get("kind", "")
    if kind in ("apple", "cuda") and not machine.can_offload():
        return 0, "CPU — this build of the engine cannot use the GPU"
    if kind == "apple":
        return -1, "GPU (Metal, unified memory)"
    if kind == "cuda":
        need_gb = (spec_of(model) or {}).get("ram_mb", 0) / 1024
        vram = float(g.get("vram_gb", 0) or 0)
        if vram >= need_gb * 1.15:
            return -1, f"GPU ({vram:.1f} GB VRAM)"
        return 0, f"CPU — {need_gb:.1f} GB does not fit {vram:.1f} GB of VRAM"
    return 0, "CPU — no GPU detected"


def load(model: str, context: int = 8192):
    """Bring a model into memory. Returns (engine, message).

    `context` is 8192 rather than llama_cpp's smaller default deliberately: a call transcript
    plus the tool documentation does not fit in 4096, and what a too-small window produces is
    not an error but a truncated prompt and a confidently wrong answer.
    """
    global _engine, _engine_model
    ok, why = available()
    if not ok:
        return None, why
    if not is_downloaded(model):
        return None, f"{model} is not downloaded."
    if _engine_model == model:
        return _engine, f"{(spec_of(model) or {}).get('name', model)} is already loaded."
    unload()
    try:
        import llama_cpp
        layers, where = _gpu_layers(model)
        _engine = llama_cpp.Llama(model_path=str(path_of(model)), n_ctx=context,
                                  n_gpu_layers=layers, verbose=False)
        _engine_model = model
        spec = spec_of(model) or {}
        logger.info("loaded %s (~%d MB resident, %s)", model, spec.get("ram_mb", 0), where)
        return _engine, f"Loaded {spec.get('name', model)}."
    except Exception as exc:
        _engine, _engine_model = None, ""
        logger.warning("could not load %s: %s", model, exc)
        return None, f"Could not load {model}: {exc}"


def unload() -> str:
    """Release the memory. The file stays on disk."""
    global _engine, _engine_model
    if not _engine_model:
        return "No model is loaded."
    _spec = spec_of(_engine_model) or {}
    was = _spec.get("name", _engine_model)
    freed = _spec.get("ram_mb", 0)
    _engine, _engine_model = None, ""
    import gc
    gc.collect()
    return f"Unloaded {was}, freeing about {freed / 1024:.1f} GB."


def state(model: str) -> str:
    """`loaded` | `downloaded` | `absent` — what the page renders a row from."""
    if loaded() == model:
        return "loaded"
    return "downloaded" if is_downloaded(model) else "absent"


def listing() -> list[dict]:
    """Every model we offer, with everything a row needs and nothing it does not."""
    # ONE ROW PER FAMILY, at the weight this machine can hold — plus anything downloaded or
    # fetched by hand, which must never disappear from the list just because the resolver would
    # have chosen a sibling.
    #
    # ALPHABETICAL, with whatever is in use pinned first. Sorting by size or by our own
    # recommendation ranks the vendors, and there is no honest basis for that; alphabetical is
    # the one order that expresses no opinion.
    chosen = set(families()) | {n for n in CATALOGUE if is_downloaded(n)} | set(custom())
    live = loaded() or os.getenv("SECRETARY_MODEL", "")
    everything = [(n, CATALOGUE[n], False) for n in chosen if n in CATALOGUE] + \
                 [(n, sp, True) for n, sp in custom().items() if n not in CATALOGUE]

    def order(t):
        name, spec, is_custom = t
        return (name != live, is_custom, spec.get("brand", "").lower(), spec.get("name", ""))

    out = []
    for name, spec, is_custom in sorted(everything, key=order):
        fit, why = can_run(name)
        room, room_why = can_download(name)
        out.append({
            "id": name, "name": spec["name"], "brand": spec["brand"],
            "params": spec.get("params", ""), "what": spec.get("what", ""),
            "speed": spec.get("speed", ""),
            "dl_gb": round(spec["dl_mb"] / 1024, 1), "ram_gb": round(spec["ram_mb"] / 1024, 1),
            "state": state(name), "fit": fit, "why": why,
            "can_download": room, "room_why": room_why,
            # ONE ANSWER TO "may this be offered", computed here so no surface has to
            # reconstruct it. The two gates are about different resources — `can_run` is
            # memory, `can_download` is disk, and its docstring says so — and a first-run
            # wizard that filtered on the disk one offered a 9 GB fetch to a machine with no
            # memory to load it, which is exactly the refusal-after-download the split exists
            # to prevent. `init` had it right and the page did not; now neither decides.
            "offerable": fit != "no" and (state(name) == "downloaded" or room),
            "in_use": name == live,
            # The other weights of this family, carried inline: the list is small and a
            # second round trip to expand one row is a spinner for nothing.
            "variants": variants(name) if not is_custom else [],
            "custom": is_custom, "estimated": bool(spec.get("estimated")),
            "repo": spec.get("repo", ""),
        })
    return out


# ---- the escape hatch: anything on Hugging Face ---------------------------------------------
#
# THE CURATED LIST IS ELEVEN MODELS AND IT IS ALREADY BEHIND — Qwen2.5 while Qwen3 ships, Gemma 2
# while Gemma 3 ships. Adding one is a code change and a release, so an owner on a build from
# last month cannot reach a model from this month. Curation still earns its place: every entry
# carries a MEASURED resident size and speed, which is what the fit pill and the recommendation
# are computed from. So both — a short list we stand behind, and a way past it.
#
# ONLY huggingface.co, and the URL is BUILT HERE from a repo and a filename rather than accepted
# from the page. A downloader that takes a URL from its caller is a request-forgery tool with a
# progress bar; this one can only ever fetch from one host.
#
# What we cannot honestly offer for a custom model: a speed, a description, or a measured memory
# figure. The size on disk is real (the API reports it), so the memory estimate is derived the
# same way an unknown model's always was — and it is labelled an estimate, because it is one.

#: PACKAGING NOTE, recorded here because this is where someone looks when local models are
#: missing from a build. llama-cpp-python publishes no wheels on PyPI, and the maintainer's own
#: index at abetlen.github.io serves macOS arm64 wheels that FAIL THEIR OWN CRC — checked on
#: 0.3.35, .34, .33 and .32 on 2026-08-27, all corrupt on `lib/libggml-base.*.dylib`, while the
#: Linux wheel from the same index passes the identical check. So CI builds it from source on
#: macOS (which also turns Metal on) and takes the wheel on Linux. See .github/workflows/build.yml.

HF_API = "https://huggingface.co/api"
HF_FILES = "https://huggingface.co"

#: Custom models live beside curated ones, each with a manifest naming where it came from.
#: Without that a downloaded file is an orphan: nothing knows its repo, its size, or what to
#: call it after a restart.
CUSTOM = "custom.json"


def _hf(url: str, timeout: int = 20):
    req = urllib.request.Request(url, headers={"User-Agent": "agentduet-desktop"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def search(query: str, limit: int = 12) -> list[dict]:
    """GGUF repositories on Hugging Face, most downloaded first."""
    q = (query or "").strip()
    if not q:
        return []
    url = (f"{HF_API}/models?search={urllib.parse.quote(q)}&filter=gguf"
           f"&sort=downloads&direction=-1&limit={int(limit)}")
    try:
        return [{"repo": m.get("modelId", ""), "downloads": m.get("downloads", 0),
                 "likes": m.get("likes", 0)} for m in _hf(url) if m.get("modelId")]
    except Exception as exc:
        logger.warning("hugging face search failed: %s", exc)
        raise RuntimeError(f"Could not reach Hugging Face: {exc}")


def files(repo: str) -> list[dict]:
    """The .gguf files in a repository, with their real sizes.

    A repository is not a model — it is a shelf of quantisations of one, from Q2 to Q8, and the
    choice between them IS the size/quality trade-off. Listing them is the point; picking one
    for the owner would be guessing at exactly the decision they came here to make.
    """
    r = (repo or "").strip().strip("/")
    if not r or r.count("/") != 1:
        return []
    try:
        tree = _hf(f"{HF_API}/models/{urllib.parse.quote(r)}/tree/main")
    except Exception as exc:
        logger.warning("could not list %s: %s", r, exc)
        raise RuntimeError(f"Could not read {r}: {exc}")
    out = []
    for f in tree:
        path = f.get("path", "")
        if not path.lower().endswith(".gguf"):
            continue
        size = (f.get("lfs") or {}).get("size") or f.get("size") or 0
        # An mmproj file is a vision projector, not a model — downloading one produces a file
        # llama_cpp will not load on its own, from a list that implied it would. It is not
        # always a PREFIX: mistralai names theirs `...-BF16-mmproj.gguf`, so a startswith test
        # let it through and offered a 0.8 GB download that could never be used.
        if "mmproj" in path.lower():
            continue
        out.append({"file": path, "mb": int(size / 1024 / 1024)})
    return sorted(out, key=lambda x: x["mb"])


def _custom_file() -> pathlib.Path:
    return STORE / CUSTOM


def custom() -> dict:
    try:
        return json.loads(_custom_file().read_text())
    except (OSError, ValueError):
        return {}


def _remember_custom(key: str, spec: dict) -> None:
    all_ = custom()
    all_[key] = spec
    _custom_file().parent.mkdir(parents=True, exist_ok=True)
    _custom_file().write_text(json.dumps(all_, indent=2))


def forget_custom(model: str) -> None:
    """Drop a custom entry. Curated ones are code and cannot be forgotten."""
    all_ = custom()
    if all_.pop(model, None) is not None:
        _custom_file().write_text(json.dumps(all_, indent=2))


def add_custom(repo: str, filename: str) -> str:
    """Register a Hugging Face file as a model, then fetch it. Returns its id."""
    r, f = (repo or "").strip().strip("/"), (filename or "").strip()
    if r.count("/") != 1 or not f.lower().endswith(".gguf") or "/" in f or ".." in f:
        raise ValueError("Give a repository like owner/name and one .gguf file from it.")
    key = f"{r}/{f}".replace("/", "_").replace(".gguf", "").lower()
    known = {x["file"]: x["mb"] for x in files(r)}
    if f not in known:
        raise ValueError(f"{r} has no file called {f}.")
    mb = known[f]
    _remember_custom(key, {
        "name": f.replace(".gguf", ""), "brand": r.split("/")[0].upper()[:9],
        "params": "", "dl_mb": mb,
        # ESTIMATED, and said to be. A curated entry's ram_mb is measured; this is the same
        # arithmetic machine.py uses for anything it has not been told about.
        "ram_mb": int(mb * machine.WORKING_SET), "estimated": True,
        "speed": "", "repo": r, "filename": f,
        "url": f"{HF_FILES}/{r}/resolve/main/{urllib.parse.quote(f)}",
        "what": f"From {r} on Hugging Face. We have not measured this one.",
    })
    return key
