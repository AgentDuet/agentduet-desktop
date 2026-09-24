"""What this computer can actually run.

Model choice is only useful next to the machine it runs on: "7 GB" means nothing, "7 GB, and you
have 16" is a decision. Whisper already ships four tiers by weight; this is the other half of
that, and the thing that lets a local LLM be offered honestly rather than optimistically.

DELIBERATELY DEPENDENCY-FREE. Everything here is a file read or a subprocess that exists on the
platform in question. `psutil` would be the obvious answer and it is a compiled wheel — another
native artifact in a binary whose packaging was hard-won, for three numbers.

EVERY NUMBER IS APPROXIMATE AND SAID TO BE. Reported RAM is not available RAM, VRAM detection
does not survive a shared GPU, and a model's file size is not its working set. The advice this
feeds is "this will fit comfortably / this will struggle", never a promise.
"""

import functools
import logging
import os
import platform
import re
import shutil
import subprocess

logger = logging.getLogger("dduet.machine")

#: A rough multiplier from a model's on-disk weight to what it wants resident. Attention and
#: context cost more than the weights, and the gap grows with the context window — this is the
#: conservative end of what people report rather than a computed figure.
WORKING_SET = 1.3


def total_ram_gb() -> float:
    """Installed RAM in GB, or 0.0 when it cannot be read."""
    try:
        if hasattr(os, "sysconf") and "SC_PAGE_SIZE" in os.sysconf_names \
                and "SC_PHYS_PAGES" in os.sysconf_names:
            # Linux and macOS both answer this; no parsing, no subprocess.
            return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024**3
        if platform.system() == "Windows":
            import ctypes

            class _Status(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            st = _Status()
            st.dwLength = ctypes.sizeof(_Status)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
            return st.ullTotalPhys / 1024**3
    except Exception as exc:                       # a number we cannot read is not an error
        logger.debug("could not read RAM: %s", exc)
    return 0.0


def available_ram_gb() -> float:
    """RAM that could be used right now, without swapping. 0.0 when unreadable.

    NOT "free". On Linux most memory shows as used because the page cache holds it, and that is
    reclaimable the instant something asks — `MemFree` is routinely a rounding error on a machine
    with plenty to spare, so sizing against it would refuse models that would load fine.
    `MemAvailable` is the kernel's own estimate of what a new process could get, which is exactly
    the question being asked.

    WHY THIS IS SEPARATE FROM `budget_gb`. This number moves: it changes when a browser tab is
    closed. A model list that reordered itself between page loads would be unusable, so the
    LIST is sized against the machine and this is used to warn at the moment of choosing —
    "this fits your machine, but not while those other things are open".
    """
    try:
        if platform.system() == "Linux":
            for line in open("/proc/meminfo"):
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024**2      # kB -> GB
        if platform.system() == "Darwin":
            # Free pages alone understate it badly; inactive and speculative are reclaimable.
            out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
            page = 4096
            for l in out.splitlines():
                if "page size of" in l:
                    page = int(l.split("page size of")[1].split()[0])
            counts = {}
            for l in out.splitlines():
                if ":" in l and l.strip().endswith("."):
                    k, v = l.split(":", 1)
                    counts[k.strip()] = int(v.strip().rstrip("."))
            pages = sum(counts.get(k, 0) for k in
                        ("Pages free", "Pages inactive", "Pages speculable", "Pages speculative"))
            return pages * page / 1024**3
        if platform.system() == "Windows":
            import ctypes

            class _S(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            st = _S(); st.dwLength = ctypes.sizeof(_S)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
            return st.ullAvailPhys / 1024**3
    except Exception as exc:
        logger.debug("could not read available RAM: %s", exc)
    return 0.0


@functools.lru_cache(maxsize=1)
def gpu() -> dict:
    """What accelerator is here, if any. Always answers; never raises.

    CACHED, because the automatic pick asks on every model call — five times per pick, through
    `verdict` — and on a machine with an NVIDIA card each ask was an `nvidia-smi` subprocess. The
    hardware cannot change while the app runs.

    `kind` is what decides the advice, and the three cases behave differently: CUDA has its own
    VRAM and is the only one where a model can be too big for the card rather than the machine;
    Apple Silicon shares memory with the CPU, so the RAM figure already covers it; nothing means
    the CPU does the work and size matters more.
    """
    if platform.system() == "Darwin" and platform.machine() in ("arm64", "aarch64"):
        # Unified memory: the GPU draws on the same pool, so there is no separate budget.
        return {"kind": "apple", "name": "Apple Silicon (unified memory)", "vram_gb": 0.0}
    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5)
            first = (out.stdout or "").strip().splitlines()[0]
            name, mb = [x.strip() for x in first.split(",")[:2]]
            return {"kind": "cuda", "name": name, "vram_gb": float(mb) / 1024}
        except Exception as exc:
            logger.debug("nvidia-smi present but unreadable: %s", exc)
    return {"kind": "cpu", "name": "", "vram_gb": 0.0}


# Apple's published unified-memory bandwidth, GB/s, per chip. A chip sold in two GPU sizes has
# two figures, keyed by the smallest GPU-core count that earns the higher one. Checked against
# Apple's own spec pages; the M5 Pro/Max/Ultra are left out until someone checks them, and a chip
# missing here falls back to the probe rather than a guess.
APPLE_SPEC_GBPS: dict[str, tuple[tuple[int, float], ...]] = {
    "Apple M1": ((0, 68.25),), "Apple M1 Pro": ((0, 200.0),),
    "Apple M1 Max": ((0, 400.0),), "Apple M1 Ultra": ((0, 800.0),),
    "Apple M2": ((0, 100.0),), "Apple M2 Pro": ((0, 200.0),),
    "Apple M2 Max": ((0, 400.0),), "Apple M2 Ultra": ((0, 800.0),),
    "Apple M3": ((0, 100.0),), "Apple M3 Pro": ((0, 150.0),),
    "Apple M3 Max": ((0, 300.0), (40, 400.0)), "Apple M3 Ultra": ((0, 819.0),),
    "Apple M4": ((0, 120.0),), "Apple M4 Pro": ((0, 273.0),),
    "Apple M4 Max": ((0, 410.0), (40, 546.0)),
    "Apple M5": ((0, 153.0),),
}

# The share of the published figure a model actually decodes at. ONE DATA POINT: the 16 GB M5,
# rated 153, where Qwen3 8B decoded at 101.6 GB/s effective (0.66). Assumed for every other chip,
# which is why the timed run after the first download is still the real check — larger chips may
# well reach a smaller share, and then this over-predicts them.
APPLE_EFFICIENCY = 0.66


@functools.lru_cache(maxsize=1)
def apple_chip() -> tuple[str, int]:
    """(chip name, GPU cores) on Apple Silicon, e.g. ("Apple M5", 10). ("", 0) anywhere else.

    The name is `sysctl machdep.cpu.brand_string`; the core count is the GPU driver's
    `gpu-core-count` in the IORegistry. Both are plain reads — no permission, no timing.
    """
    if gpu()["kind"] != "apple":
        return "", 0
    name, cores = "", 0
    try:
        name = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"],
                              capture_output=True, text=True, timeout=5).stdout.strip()
        out = subprocess.run(["ioreg", "-rc", "AGXAccelerator", "-d1"],
                             capture_output=True, text=True, timeout=5).stdout
        m = re.search(r'"gpu-core-count"\s*=\s*(\d+)', out)
        cores = int(m.group(1)) if m else 0
    except Exception as exc:
        logger.debug("could not read the Apple chip: %s", exc)
    return name, cores


def apple_spec_gbps(name: str, cores: int) -> float:
    """Apple's published bandwidth for this chip, or 0.0 when the table does not know it.

    An unreadable core count takes the LOWER figure of a two-size chip: under-reading picks a
    smaller, faster model, which is the safe direction.
    """
    tiers = APPLE_SPEC_GBPS.get(name)
    if not tiers:
        return 0.0
    return max(gbps for min_cores, gbps in tiers if cores >= min_cores)


_BANDWIDTH: float | None = None


def bandwidth_gbps() -> float:
    """How fast this machine moves memory for a model, in GB/s — what decides how fast it ANSWERS.

    Capacity decides whether a model fits; bandwidth decides its speed, because generating each
    token reads every active weight once. So decode rate is about bandwidth / active bytes.

    ON A MAC, LOOKED UP, NOT MEASURED. macOS reports no bandwidth, but it names the chip exactly,
    and Apple publishes each chip's figure; scaled by `APPLE_EFFICIENCY` that is what a model
    gets. The probe below cannot stand in for it there: it copies on ONE core, and on the M5 that
    happened to match the GPU (102 against 101.6), but a Pro, Max or Ultra GPU reads memory far
    faster than one core can copy it, so the probe would pick those machines a rung too small.

    Everywhere else, and on a chip the table does not know, the probe answers. Cached for the
    life of the process. 0.0 when neither can answer, which callers treat as unknown, never slow.
    """
    global _BANDWIDTH
    if _BANDWIDTH is None:
        spec = apple_spec_gbps(*apple_chip())
        _BANDWIDTH = round(spec * APPLE_EFFICIENCY, 1) if spec else _probe_gbps()
    return _BANDWIDTH


def _probe_gbps() -> float:
    """Measure memory bandwidth by timing a copy. The fallback when the chip is not in the table.

    PURE PYTHON, deliberately. `bytes(bytearray)` is a memcpy underneath and measured the same as
    numpy on the M5 (100.8 against 100.9), so this module stays as dependency-free as the rest of
    it. Best of ten copies of a buffer far larger than any cache — about 0.1 s — so a background
    burst cannot drag the answer down: best-of-five once read 94.7 where the steady figure is
    102, and at the speed floor that gap decides a tier.

    It is single-threaded, so it under-reads any machine where many threads or a GPU can pull
    more than one core — smaller, faster model, the safe direction. 0.0 on failure.
    """
    try:
        import time
        mb = 256
        buf = bytearray(mb * 1024 * 1024)
        bytes(buf)                                    # fault the pages in before timing
        best = 0.0
        for _ in range(10):
            t0 = time.perf_counter()
            copy = bytes(buf)
            dt = time.perf_counter() - t0
            del copy
            if dt > 0:
                best = max(best, 2 * mb / 1024 / dt)  # a copy is a read plus a write
        return round(best, 1)
    except Exception as exc:                          # a number we cannot read is not an error
        logger.debug("could not measure memory bandwidth: %s", exc)
        return 0.0


_OFFLOAD: bool | None = None


def can_offload() -> bool:
    """Whether THIS BUILD of the engine can put layers on a GPU — not whether a GPU exists.

    THE HARDWARE AND THE BUILD ARE DIFFERENT QUESTIONS, and `gpu()` answers only the first.
    Windows and Linux install llama-cpp-python from the `/whl/cpu` index, which cannot offload at
    all, so an NVIDIA card there is invisible to the engine however much VRAM it has. Sizing
    against that VRAM picks a model for a card nothing will ever use: latent while the owner
    chose from a list, a wrong answer the moment the app chooses for them. So ask the engine.

    Not a dependency: the engine is optional, it is imported only if this build carries it, and a
    build without it simply cannot offload. Cached, because the answer is a property of the
    installed binary and cannot change while it runs — and on a Mac the probe initialises Metal.
    """
    global _OFFLOAD
    if _OFFLOAD is None:
        try:
            import llama_cpp
            _OFFLOAD = bool(llama_cpp.llama_supports_gpu_offload())
        except Exception as exc:              # no engine in this build, or one without the call
            logger.debug("cannot ask the engine about GPU offload: %s", exc)
            _OFFLOAD = False
    return _OFFLOAD


def budget_gb() -> float:
    """How much a model may reasonably use.

    NOT the whole machine. The owner is running a browser and their own work; a model that fits
    only when nothing else is open is a model that swaps the moment they do anything. Two thirds
    of RAM is the conservative share, and a CUDA card is judged on its own VRAM instead — but only
    when this build can actually use it (`can_offload`), which the shipped Windows build cannot.
    """
    g = gpu()
    if g["kind"] == "cuda" and g["vram_gb"] > 0 and can_offload():
        return g["vram_gb"] * 0.9          # the card is doing nothing else
    ram = total_ram_gb()
    return round(ram * 0.66, 1) if ram else 0.0


def verdict(size_gb: float) -> tuple[str, str]:
    """(fits|tight|no|unknown, one sentence) for a model of this on-disk size."""
    budget = budget_gb()
    if not budget:
        return "unknown", "Could not read this machine's memory, so this is unchecked."
    # The sentence is PER ROW, so it must carry only what differs per row: the arithmetic.
    # It used to explain the verdict too ("expect it to slow down…"), which meant a list of
    # four similar models printed the same paragraph four times, and the reader stopped
    # reading all of them. What "tight" means is explained once, by the list.
    need = size_gb * WORKING_SET
    if need <= budget * 0.6:
        return "fits", f"needs about {need:.1f} GB of {budget:.0f} GB usable"
    if need <= budget:
        return "tight", f"needs about {need:.1f} GB of {budget:.0f} GB usable"
    return "no", f"needs about {need:.1f} GB, more than the {budget:.0f} GB usable"


def fits_now(size_gb: float) -> tuple[bool, str]:
    """Whether it would load AT THIS MOMENT, given what is currently open.

    Kept apart from `verdict` on purpose. That answers "can this machine run it", which is
    stable and is what the list should be ordered and coloured by. This answers "could it start
    right now", which changes when a browser tab closes — useful as a warning at the moment of
    choosing, useless as a property of a row.
    """
    avail = available_ram_gb()
    if not avail:
        return True, ""                    # unknown: do not invent an obstacle
    need = size_gb * WORKING_SET
    if need <= avail:
        return True, ""
    return False, (f"Right now only {avail:.1f} GB is free, and this wants about {need:.1f} GB. "
                   "It fits this machine, but not while everything currently open stays open.")


def describe() -> dict:
    """Everything a page needs to size a model list against this machine."""
    g = gpu()
    return {"ram_gb": round(total_ram_gb(), 1), "budget_gb": budget_gb(),
            "available_gb": round(available_ram_gb(), 1),
            "gpu_kind": g["kind"], "gpu_name": g["name"], "vram_gb": round(g["vram_gb"], 1)}
