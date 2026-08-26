"""Hardware capabilities analyzer, dynamic model adviser, and resource gating.

Inspects host hardware (RAM, storage, CPU architecture, Apple Silicon / GPU accelerators)
and evaluates whether on-device models can safely be downloaded and loaded without
causing system instability or out-of-memory crashes.
"""

from __future__ import annotations

import logging
import os
import pathlib
import platform
import shutil
import subprocess
import sys
from typing import Any

from . import paths

logger = logging.getLogger("secretary.hardware")

#: Memory reserve (MB) kept free for the OS, window server, and background services.
SYSTEM_RAM_RESERVE_MB = 1024

#: Disk safety multiplier when downloading model weights.
DISK_SAFETY_FACTOR = 1.2

#: Model resource specification map: disk download size (MB) and resident RAM needed (MB).
STT_MODELS: dict[str, dict[str, Any]] = {
    "tiny": {
        "id": "tiny",
        "name": "Whisper Tiny",
        "tier": "fast",
        "disk_mb": 75,
        "ram_mb": 390,
        "description": "Ultra lightweight, minimal resource usage (~75 MB disk, ~390 MB RAM).",
    },
    "base": {
        "id": "base",
        "name": "Whisper Base",
        "tier": "fast",
        "disk_mb": 142,
        "ram_mb": 500,
        "description": "Fast and responsive on CPU (~142 MB disk, ~500 MB RAM).",
    },
    "small": {
        "id": "small",
        "name": "Whisper Small",
        "tier": "balanced",
        "disk_mb": 464,
        "ram_mb": 1000,
        "description": "Balanced accuracy and speed (~464 MB disk, ~1.0 GB RAM).",
    },
    "medium": {
        "id": "medium",
        "name": "Whisper Medium",
        "tier": "accurate",
        "disk_mb": 1500,
        "ram_mb": 2600,
        "description": "High accuracy for varied accents (~1.5 GB disk, ~2.6 GB RAM).",
    },
    "large-v3": {
        "id": "large-v3",
        "name": "Whisper Large v3",
        "tier": "max",
        "disk_mb": 2900,
        "ram_mb": 4700,
        "description": "Maximum accuracy, high memory requirement (~2.9 GB disk, ~4.7 GB RAM).",
    },
}

#: Tier alias to canonical STT model id
TIER_TO_MODEL: dict[str, str] = {
    "fast": "base",
    "balanced": "small",
    "accurate": "medium",
    "max": "large-v3",
}


class HardwareInsufficientError(RuntimeError):
    """Raised when an action exceeds host hardware capabilities."""


def get_memory_info() -> dict[str, Any]:
    """Return memory statistics in megabytes and gigabytes: total_mb, available_mb, used_mb, total_gb, available_gb, used_gb."""
    res = None
    # 1. Try psutil if installed
    try:
        import psutil
        v = psutil.virtual_memory()
        res = {
            "total_mb": int(v.total / (1024 * 1024)),
            "available_mb": int(v.available / (1024 * 1024)),
            "used_mb": int(v.used / (1024 * 1024)),
        }
    except Exception:
        pass

    # 2. Native macOS inspection
    if not res and platform.system() == "Darwin":
        total_mb = 0
        avail_mb = 0
        try:
            # Total RAM via sysctl
            out = subprocess.check_output(["sysctl", "-n", "hw.memsize"], timeout=2)
            total_bytes = int(out.strip())
            total_mb = int(total_bytes / (1024 * 1024))
        except Exception:
            pass

        try:
            # Available RAM estimation via vm_stat
            out = subprocess.check_output(["vm_stat"], timeout=2).decode("utf-8")
            page_size = 4096
            free_pages = 0
            inactive_pages = 0
            speculative_pages = 0
            for line in out.splitlines():
                if "page size of" in line:
                    parts = line.split("page size of")
                    if len(parts) > 1:
                        try:
                            page_size = int(parts[1].split("bytes")[0].strip())
                        except ValueError:
                            pass
                elif "Pages free:" in line:
                    free_pages = int(line.split(":")[1].strip().rstrip("."))
                elif "Pages inactive:" in line:
                    inactive_pages = int(line.split(":")[1].strip().rstrip("."))
                elif "Pages speculative:" in line:
                    speculative_pages = int(line.split(":")[1].strip().rstrip("."))
            avail_bytes = (free_pages + inactive_pages + speculative_pages) * page_size
            avail_mb = int(avail_bytes / (1024 * 1024))
        except Exception:
            pass

        if total_mb > 0:
            if avail_mb == 0:
                avail_mb = int(total_mb * 0.5)  # reasonable estimate fallback
            res = {
                "total_mb": total_mb,
                "available_mb": avail_mb,
                "used_mb": max(0, total_mb - avail_mb),
            }

    # 3. Native Linux inspection
    if not res and platform.system() == "Linux":
        try:
            meminfo = pathlib.Path("/proc/meminfo").read_text()
            data = {}
            for line in meminfo.splitlines():
                parts = line.split(":")
                if len(parts) == 2:
                    k = parts[0].strip()
                    val = parts[1].strip().split()[0]
                    data[k] = int(val)  # in kB
            total_mb = int(data.get("MemTotal", 0) / 1024)
            avail_mb = int(data.get("MemAvailable", data.get("MemFree", 0)) / 1024)
            res = {
                "total_mb": total_mb,
                "available_mb": avail_mb,
                "used_mb": max(0, total_mb - avail_mb),
            }
        except Exception:
            pass

    # 4. Fallback safe defaults (e.g. 8 GB total, 4 GB free)
    if not res:
        res = {
            "total_mb": 8192,
            "available_mb": 4096,
            "used_mb": 4096,
        }

    res["total_gb"] = round(res["total_mb"] / 1024, 1)
    res["available_gb"] = round(res["available_mb"] / 1024, 1)
    res["used_gb"] = round(res["used_mb"] / 1024, 1)
    return res


def get_disk_info(target_dir: pathlib.Path | None = None) -> dict[str, Any]:
    """Return storage statistics in megabytes and gigabytes for the storage partition."""
    path = target_dir or paths.HOME
    if not path.exists():
        path = pathlib.Path.home()
    try:
        usage = shutil.disk_usage(path)
        t_mb = int(usage.total / (1024 * 1024))
        f_mb = int(usage.free / (1024 * 1024))
        u_mb = int(usage.used / (1024 * 1024))
        return {
            "total_mb": t_mb,
            "free_mb": f_mb,
            "used_mb": u_mb,
            "total_gb": round(t_mb / 1024, 1),
            "free_gb": round(f_mb / 1024, 1),
            "used_gb": round(u_mb / 1024, 1),
        }
    except Exception as exc:
        logger.debug("could not inspect disk usage at %s: %s", path, exc)
        return {
            "total_mb": 64000,
            "free_mb": 32000,
            "used_mb": 32000,
            "total_gb": 62.5,
            "free_gb": 31.2,
            "used_gb": 31.2,
        }


#: Backward compatibility alias
SPECS = STT_MODELS


def get_accelerator_info() -> dict[str, Any]:
    """Inspect system acceleration (Apple Silicon, CUDA, ANE)."""
    arch = platform.machine().lower()
    is_apple_silicon = platform.system() == "Darwin" and arch in ("arm64", "aarch64")
    chip_name = platform.processor() or "CPU"
    if is_apple_silicon:
        try:
            brand = subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"], timeout=2).decode().strip()
            if brand:
                chip_name = brand
        except Exception:
            chip_name = "Apple Silicon"

    cuda_available = False
    cuda_devices = 0
    try:
        import ctranslate2
        cuda_devices = ctranslate2.get_cuda_device_count()
        cuda_available = cuda_devices > 0
    except Exception:
        pass

    from . import transcribe
    ane_ok, ane_why = transcribe.ane_support()

    return {
        "type": "apple_silicon" if is_apple_silicon else ("cuda" if cuda_available else "cpu"),
        "chip_name": chip_name,
        "is_apple_silicon": is_apple_silicon,
        "cuda_available": cuda_available,
        "cuda_devices": cuda_devices,
        "ane_supported": ane_ok,
        "ane_why": ane_why,
    }


def get_hardware_profile() -> dict[str, Any]:
    """Full hardware snapshot including memory, storage, CPU, and accelerators."""
    mem = get_memory_info()
    disk = get_disk_info()
    accel = get_accelerator_info()

    total_mb = mem.get("total_mb", 8192)
    avail_mb = mem.get("available_mb", 4096)
    used_mb = mem.get("used_mb", max(0, total_mb - avail_mb))

    d_total_mb = disk.get("total_mb", 64000)
    d_free_mb = disk.get("free_mb", 32000)
    d_used_mb = disk.get("used_mb", max(0, d_total_mb - d_free_mb))

    return {
        "os": platform.system(),
        "os_version": platform.release(),
        "architecture": platform.machine().lower(),
        "chip_name": accel.get("chip_name", "CPU"),
        "cpu_count": os.cpu_count() or 1,
        "is_apple_silicon": accel.get("is_apple_silicon", False),
        "accelerator": accel,
        "memory": {
            "total_mb": total_mb,
            "total_gb": mem.get("total_gb", round(total_mb / 1024, 1)),
            "available_mb": avail_mb,
            "available_gb": mem.get("available_gb", round(avail_mb / 1024, 1)),
            "used_mb": used_mb,
            "used_gb": mem.get("used_gb", round(used_mb / 1024, 1)),
        },
        "disk": {
            "total_mb": d_total_mb,
            "total_gb": disk.get("total_gb", round(d_total_mb / 1024, 1)),
            "free_mb": d_free_mb,
            "free_gb": disk.get("free_gb", round(d_free_mb / 1024, 1)),
            "used_mb": d_used_mb,
            "used_gb": disk.get("used_gb", round(d_used_mb / 1024, 1)),
        },
        "accelerators": {
            "apple_silicon_ane": {"supported": accel.get("ane_supported", False), "reason": accel.get("ane_why", "")},
            "cuda": {"supported": accel.get("cuda_available", False), "devices": accel.get("cuda_devices", 0)},
        },
    }


def resolve_model_spec(model_or_tier: str) -> dict[str, Any] | None:
    """Find the specification for a model name or quality tier."""
    name = (model_or_tier or "").strip().lower()
    if name in STT_MODELS:
        return STT_MODELS[name]
    if name in TIER_TO_MODEL:
        return STT_MODELS[TIER_TO_MODEL[name]]
    for spec in STT_MODELS.values():
        if spec["id"] == name or spec["name"].lower() == name:
            return spec
    return None


def check_capability(model_or_tier: str, profile: dict[str, Any] | None = None) -> dict[str, Any]:
    """Evaluate whether the host machine can safely download and run a model.

    Returns:
      {
        "id": "small",
        "name": "Whisper Small",
        "tier": "balanced",
        "can_download": bool,
        "can_load": bool,
        "status": "recommended" | "compatible" | "warning" | "blocked",
        "reason": str,
        "disk_mb": int,
        "ram_mb": int
      }
    """
    spec = resolve_model_spec(model_or_tier)
    if not spec:
        return {
            "id": model_or_tier,
            "name": model_or_tier,
            "tier": "unknown",
            "can_download": True,
            "can_load": True,
            "status": "compatible",
            "reason": "Custom or hosted model.",
            "disk_mb": 0,
            "ram_mb": 0,
        }

    hw = profile or get_hardware_profile()
    mem = hw["memory"]
    disk = hw["disk"]

    disk_req = spec["disk_mb"]
    ram_req = spec["ram_mb"]

    # 1. Storage check
    disk_safety_need = int(disk_req * DISK_SAFETY_FACTOR)
    has_disk = disk["free_mb"] >= disk_safety_need

    # 2. Total and available memory check
    total_ram = mem["total_mb"]
    avail_ram = mem["available_mb"]

    # Total RAM must comfortably exceed model RAM requirement
    has_total_ram = total_ram >= (ram_req + SYSTEM_RAM_RESERVE_MB)
    # Available RAM headroom check
    has_avail_ram = avail_ram >= (ram_req * 0.7)

    can_download = has_disk and has_total_ram
    can_load = has_total_ram and has_avail_ram

    status = "compatible"
    reason = "Fully compatible with your hardware."

    if not has_total_ram:
        status = "blocked"
        can_download = False
        can_load = False
        reason = (
            f"Blocked: {spec['name']} requires ~{ram_req} MB RAM, but your machine has "
            f"{total_ram} MB total RAM ({mem['total_gb']} GB). Running this model would exceed "
            f"system capabilities."
        )
    elif not has_disk:
        status = "blocked"
        can_download = False
        reason = (
            f"Blocked: Insufficient free storage space ({disk['free_gb']} GB free). "
            f"Downloading {spec['name']} requires ~{disk_req} MB with headroom."
        )
    elif not has_avail_ram:
        status = "warning"
        reason = (
            f"Low memory headroom: {avail_ram} MB available RAM vs ~{ram_req} MB needed. "
            f"Close other heavy applications before loading."
        )

    return {
        "id": spec["id"],
        "name": spec["name"],
        "tier": spec["tier"],
        "disk_mb": disk_req,
        "ram_mb": ram_req,
        "can_download": can_download,
        "can_load": can_load,
        "status": status,
        "reason": reason,
    }


def recommend_models(profile: dict[str, Any] | None = None) -> dict[str, Any]:
    """Analyze host resources and recommend the optimal on-device speech/LLM models."""
    hw = profile or get_hardware_profile()
    total_gb = hw["memory"]["total_gb"]
    is_apple_silicon = hw["is_apple_silicon"]

    # Dynamic tier recommendation based on Unified RAM / System RAM
    if total_gb >= 16:
        recommended_tier = "accurate" if not is_apple_silicon else "accurate"
    elif total_gb >= 8:
        recommended_tier = "balanced"
    elif total_gb >= 4:
        recommended_tier = "fast"
    else:
        recommended_tier = "fast"

    # Evaluate all models
    evaluations = {}
    for key, spec in STT_MODELS.items():
        eval_info = check_capability(key, hw)
        if spec["tier"] == recommended_tier and eval_info["status"] in ("compatible", "warning"):
            eval_info["status"] = "recommended"
            eval_info["reason"] = f"⭐ Recommended: Optimal balance of accuracy and performance for your {total_gb} GB system."
        evaluations[key] = eval_info

    return {
        "hardware": hw,
        "recommended_tier": recommended_tier,
        "recommended_model": TIER_TO_MODEL.get(recommended_tier, "small"),
        "models": evaluations,
    }


def validate_model_action(model_or_tier: str, action: str = "load") -> None:
    """Raise HardwareInsufficientError if downloading or loading exceeds hardware capabilities."""
    info = check_capability(model_or_tier)
    if action == "download" and not info["can_download"]:
        logger.warning("refusing download for %s: %s", model_or_tier, info["reason"])
        raise HardwareInsufficientError(info["reason"])
    if action == "load" and not info["can_load"]:
        logger.warning("refusing load for %s: %s", model_or_tier, info["reason"])
        raise HardwareInsufficientError(info["reason"])
