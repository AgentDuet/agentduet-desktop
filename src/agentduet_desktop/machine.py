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

import logging
import os
import platform
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


def gpu() -> dict:
    """What accelerator is here, if any. Always answers; never raises.

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


def budget_gb() -> float:
    """How much a model may reasonably use.

    NOT the whole machine. The owner is running a browser and their own work; a model that fits
    only when nothing else is open is a model that swaps the moment they do anything. Two thirds
    of RAM is the conservative share, and a CUDA card is judged on its own VRAM instead.
    """
    g = gpu()
    if g["kind"] == "cuda" and g["vram_gb"] > 0:
        return g["vram_gb"] * 0.9          # the card is doing nothing else
    ram = total_ram_gb()
    return round(ram * 0.66, 1) if ram else 0.0


def verdict(size_gb: float) -> tuple[str, str]:
    """(fits|tight|no|unknown, one sentence) for a model of this on-disk size."""
    budget = budget_gb()
    if not budget:
        return "unknown", "Could not read this machine's memory, so this is unchecked."
    need = size_gb * WORKING_SET
    if need <= budget * 0.6:
        return "fits", f"Comfortable — about {need:.1f} GB against {budget:.0f} GB usable."
    if need <= budget:
        return "tight", (f"Should run, with little to spare — about {need:.1f} GB against "
                         f"{budget:.0f} GB usable. Expect it to slow down with other apps open.")
    return "no", (f"Too big for this machine — wants about {need:.1f} GB against {budget:.0f} GB "
                  f"usable. It would swap, which is slower than the hosted model.")


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
