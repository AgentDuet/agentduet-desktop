"""The local models we offer, and the files behind them.

WHY THIS EXISTS RATHER THAN OLLAMA. Ollama made model management free, and cost the one thing
this product sells: an owner who downloads one binary and it works. Asking someone to install a
second program before their phone can be answered is the hosting decision we already removed,
wearing a different hat. So the weights are ours to fetch, store and run.

THE CATALOGUE IS FROM KC's `hardware.py` (PR #3, okchoong:feature-model-adviser-and-unload) and
is the part of that work with the longest shelf life: the repo, the exact GGUF filename, the
download size, the resident size and a measured speed for each model. Runtime-independent, and
hours of research nobody should do twice.

WHAT EACH FIGURE MEANS, because two of them look alike and are not:
  dl_mb   what lands on disk — what the progress bar counts to.
  ram_mb  what it occupies once loaded. Always larger: weights plus the KV cache and context.

THREE STATES, NOT TWO. A model is absent, or on disk, or resident in memory. `machine.py` says
whether one COULD run here; this says which of the three it is right now. Conflating the last
two is how a laptop ends up holding five gigabytes for a model nobody is using.
"""

import json
import logging
import os
import pathlib
import threading
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
    "smollm2-360m": dict(
        name="SmolLM2 360M Instruct", brand="SMOLLM", params="360M",
        dl_mb=230, ram_mb=450,
        speed="110-150 tok/s",
        repo="bartowski/SmolLM2-360M-Instruct-GGUF",
        filename="SmolLM2-360M-Instruct-Q4_K_M.gguf",
        url="https://huggingface.co/bartowski/SmolLM2-360M-Instruct-GGUF/resolve/main/SmolLM2-360M-Instruct-Q4_K_M.gguf",
        what="Ultra-lightweight featherweight model. Fits easily in under 500 MB RAM, perfect for fast transcription summaries."),
    "qwen-2.5-0.5b": dict(
        name="Qwen2.5 0.5B Instruct", brand="QWEN", params="0.49B",
        dl_mb=398, ram_mb=650,
        speed="95-125 tok/s",
        repo="Qwen/Qwen2.5-0.5B-Instruct-GGUF",
        filename="qwen2.5-0.5b-instruct-q4_k_m.gguf",
        url="https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/qwen2.5-0.5b-instruct-q4_k_m.gguf",
        what="Sub-1GB edge model. Extremely fast with strong multilingual understanding and low latency triage."),
    "llama-3.2-1b": dict(
        name="Llama 3.2 1B Instruct", brand="META", params="1.2B",
        dl_mb=750, ram_mb=1150,
        speed="70-90 tok/s",
        repo="bartowski/Llama-3.2-1B-Instruct-GGUF",
        filename="Llama-3.2-1B-Instruct-Q4_K_M.gguf",
        url="https://huggingface.co/bartowski/Llama-3.2-1B-Instruct-GGUF/resolve/main/Llama-3.2-1B-Instruct-Q4_K_M.gguf",
        what="Meta's ultra-efficient 1B model. Excellent balance of speed, summarization, and low-latency agent tasks."),
    "qwen-2.5-1.5b": dict(
        name="Qwen2.5 1.5B Instruct", brand="QWEN", params="1.5B",
        dl_mb=986, ram_mb=1350,
        speed="65-80 tok/s",
        repo="Qwen/Qwen2.5-1.5B-Instruct-GGUF",
        filename="qwen2.5-1.5b-instruct-q4_k_m.gguf",
        url="https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/qwen2.5-1.5b-instruct-q4_k_m.gguf",
        what="Top recommendation for 8GB Macs. Instant streaming with strong multilingual, reasoning, and summarization skills."),
    "smollm2-1.7b": dict(
        name="SmolLM2 1.7B Instruct", brand="SMOLLM", params="1.7B",
        dl_mb=1050, ram_mb=1400,
        speed="65-85 tok/s",
        repo="HuggingFaceTB/SmolLM2-1.7B-Instruct-GGUF",
        filename="smollm2-1.7b-instruct-q4_k_m.gguf",
        url="https://huggingface.co/HuggingFaceTB/SmolLM2-1.7B-Instruct-GGUF/resolve/main/smollm2-1.7b-instruct-q4_k_m.gguf",
        what="Compact powerhouse outperforming earlier 3B models in multi-turn reasoning and instruction following."),
    "gemma-2-2b": dict(
        name="Gemma 2 2B Instruct", brand="GOOGLE", params="2.6B",
        dl_mb=1650, ram_mb=2100,
        speed="45-60 tok/s",
        repo="bartowski/gemma-2-2b-it-GGUF",
        filename="gemma-2-2b-it-Q4_K_M.gguf",
        url="https://huggingface.co/bartowski/gemma-2-2b-it-GGUF/resolve/main/gemma-2-2b-it-Q4_K_M.gguf",
        what="Google's class-leading compact model with exceptional knowledge retrieval and dialogue capabilities."),
    "llama-3.2-3b": dict(
        name="Llama 3.2 3B Instruct", brand="META", params="3.2B",
        dl_mb=1980, ram_mb=2600,
        speed="35-48 tok/s",
        repo="bartowski/Llama-3.2-3B-Instruct-GGUF",
        filename="Llama-3.2-3B-Instruct-Q4_K_M.gguf",
        url="https://huggingface.co/bartowski/Llama-3.2-3B-Instruct-GGUF/resolve/main/Llama-3.2-3B-Instruct-Q4_K_M.gguf",
        what="Meta's highly capable 3B edge model for multilingual dialogue, agentic tasks, and creative workflows."),
    "qwen-2.5-3b": dict(
        name="Qwen2.5 3B Instruct", brand="QWEN", params="3.1B",
        dl_mb=1930, ram_mb=2600,
        speed="35-50 tok/s",
        repo="Qwen/Qwen2.5-3B-Instruct-GGUF",
        filename="qwen2.5-3b-instruct-q4_k_m.gguf",
        url="https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/main/qwen2.5-3b-instruct-q4_k_m.gguf",
        what="Exceptional depth for complex creative writing, precise multi-turn dialogues, and in-depth reasoning."),
    "phi-3.5-mini": dict(
        name="Phi 3.5 Mini 3.8B", brand="MICROSOFT", params="3.8B",
        dl_mb=2300, ram_mb=3100,
        speed="30-45 tok/s",
        repo="bartowski/Phi-3.5-mini-instruct-GGUF",
        filename="Phi-3.5-mini-instruct-Q4_K_M.gguf",
        url="https://huggingface.co/bartowski/Phi-3.5-mini-instruct-GGUF/resolve/main/Phi-3.5-mini-instruct-Q4_K_M.gguf",
        what="Microsoft's compact powerhouse optimized for code reasoning, structured JSON outputs, and dense logic."),
    "qwen-2.5-7b": dict(
        name="Qwen2.5 7B Instruct", brand="QWEN", params="7.6B",
        dl_mb=4700, ram_mb=5500,
        speed="20-30 tok/s",
        repo="bartowski/Qwen2.5-7B-Instruct-GGUF",
        filename="Qwen2.5-7B-Instruct-Q4_K_M.gguf",
        url="https://huggingface.co/bartowski/Qwen2.5-7B-Instruct-GGUF/resolve/main/Qwen2.5-7B-Instruct-Q4_K_M.gguf",
        what="High-tier open weights model matching proprietary API quality for complex reasoning and enterprise tools."),
    "mistral-7b": dict(
        name="Mistral 7B Instruct v0.3", brand="MISTRAL", params="7.2B",
        dl_mb=4400, ram_mb=5300,
        speed="20-30 tok/s",
        repo="bartowski/Mistral-7B-Instruct-v0.3-GGUF",
        filename="Mistral-7B-Instruct-v0.3-Q4_K_M.gguf",
        url="https://huggingface.co/bartowski/Mistral-7B-Instruct-v0.3-GGUF/resolve/main/Mistral-7B-Instruct-v0.3-Q4_K_M.gguf",
        what="Industry benchmark 7B open model with extended context window and superior instruction following."),
}


# ---- what is on disk -----------------------------------------------------------------------

def path_of(model: str) -> pathlib.Path | None:
    """Where this model's weights would live, or None if we do not offer it."""
    spec = CATALOGUE.get(model)
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
    spec = CATALOGUE.get(model)
    if not spec:
        return False, "not a model we offer"
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
    spec = CATALOGUE.get(model)
    if not spec:
        return "unknown", ""
    return machine.verdict(spec["ram_mb"] / 1024 / machine.WORKING_SET)


def recommended() -> str:
    """The largest model that fits comfortably. A list where everything is merely possible
    leaves the choice entirely to someone with no way to make it — and the picker's own sizing
    was actively misleading once already: it called a 3B model `fits` when that model could not
    drive the tool protocol at all, while the 8B it called `tight` worked first time. Size is
    not capability, so this prefers capable-and-comfortable over small-and-safe."""
    best, best_ram = "", 0
    for name, spec in CATALOGUE.items():
        fit, _ = can_run(name)
        if fit == "fits" and spec["ram_mb"] > best_ram:
            best, best_ram = name, spec["ram_mb"]
    if best:
        return best
    for name, spec in sorted(CATALOGUE.items(), key=lambda kv: kv[1]["ram_mb"]):
        if can_run(name)[0] == "tight":
            return name
    return min(CATALOGUE, key=lambda n: CATALOGUE[n]["ram_mb"])


# ---- fetching ------------------------------------------------------------------------------
#
# ONE DOWNLOAD AT A TIME, and its progress readable from anywhere. The page polls; the fetch
# runs on a worker thread. A per-request object would leave the page unable to answer "how far
# along is it?" after a reload, which on a five-gigabyte file is the whole question.

_lock = threading.Lock()
_state: dict = {"model": "", "done_mb": 0, "total_mb": 0, "error": "", "finished": "",
                "cancel": False}


def progress() -> dict:
    with _lock:
        s = dict(_state)
    s["percent"] = int(s["done_mb"] * 100 / s["total_mb"]) if s["total_mb"] else 0
    return s


def cancel() -> str:
    """Ask the running download to stop. The partial file is KEPT — the next attempt resumes
    from it, which on a 4.6 GB fetch over a hotel connection is the difference between an
    interruption and starting again."""
    with _lock:
        if not _state["model"]:
            return "Nothing is downloading."
        _state["cancel"] = True
        return f"Stopping {_state['model']}."


def download(model: str) -> str:
    """Fetch the weights. BLOCKING and slow — gigabytes — so call it off the event loop.

    Resumes from a previous partial file with a Range request. Writes to `<name>.part` and
    renames only on success, so an interrupted download can never be mistaken for a usable
    model by `is_downloaded`.
    """
    spec = CATALOGUE.get(model)
    if not spec:
        return f"{model} is not a model we offer."
    ok, why = can_download(model)
    if not ok:
        return f"Cannot download {spec['name']}: {why}."
    with _lock:
        if _state["model"]:
            return f"Already downloading {_state['model']}."
        _state.update(model=model, done_mb=0, total_mb=spec["dl_mb"], error="", finished="",
                      cancel=False)

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
            with _lock:
                _state["total_mb"] = int(total / 1024 / 1024) or spec["dl_mb"]
                _state["done_mb"] = int(have / 1024 / 1024)
            with open(part, "ab" if resuming else "wb") as out:
                got = have
                while True:
                    with _lock:
                        if _state["cancel"]:
                            return f"Stopped. {int(got / 1024 / 1024)} MB kept — it will resume."
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    got += len(chunk)
                    with _lock:
                        _state["done_mb"] = int(got / 1024 / 1024)
        part.replace(target)
        with _lock:
            _state["finished"] = model
        logger.info("downloaded %s (%.1f GB)", model, target.stat().st_size / 1024**3)
        return f"Downloaded {spec['name']}."
    except Exception as exc:
        with _lock:
            _state["error"] = f"{type(exc).__name__}: {exc}"
        logger.warning("download of %s failed: %s", model, exc)
        return f"Could not download {spec['name']}: {exc}"
    finally:
        with _lock:
            _state.update(model="", cancel=False)


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
    return f"Deleted {CATALOGUE.get(model, {}).get('name', model)}, freeing {freed / 1024**3:.1f} GB."


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
        return _engine, f"{CATALOGUE[model]['name']} is already loaded."
    unload()
    try:
        import llama_cpp
        _engine = llama_cpp.Llama(model_path=str(path_of(model)), n_ctx=context,
                                  verbose=False)
        _engine_model = model
        logger.info("loaded %s (~%d MB resident)", model, CATALOGUE[model]["ram_mb"])
        return _engine, f"Loaded {CATALOGUE[model]['name']}."
    except Exception as exc:
        _engine, _engine_model = None, ""
        logger.warning("could not load %s: %s", model, exc)
        return None, f"Could not load {model}: {exc}"


def unload() -> str:
    """Release the memory. The file stays on disk."""
    global _engine, _engine_model
    if not _engine_model:
        return "No model is loaded."
    was = CATALOGUE.get(_engine_model, {}).get("name", _engine_model)
    freed = CATALOGUE.get(_engine_model, {}).get("ram_mb", 0)
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
    rec = recommended()
    out = []
    for name, spec in sorted(CATALOGUE.items(), key=lambda kv: kv[1]["ram_mb"]):
        fit, why = can_run(name)
        room, room_why = can_download(name)
        out.append({
            "id": name, "name": spec["name"], "brand": spec["brand"],
            "params": spec["params"], "what": spec["what"], "speed": spec["speed"],
            "dl_gb": round(spec["dl_mb"] / 1024, 1), "ram_gb": round(spec["ram_mb"] / 1024, 1),
            "state": state(name), "fit": fit, "why": why,
            "can_download": room, "room_why": room_why,
            "recommended": name == rec,
        })
    return out
