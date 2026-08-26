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

#: Open-source Local LLM resource specifications catalog
LLM_MODELS: dict[str, dict[str, Any]] = {
    "qwen-2.5-1.5b": {
        "id": "qwen-2.5-1.5b",
        "brand": "QWEN",
        "brand_color": "#7c3aed",
        "name": "Qwen2.5 1.5B Instruct",
        "provider": "local",
        "params": "1.5B",
        "dl_mb": 982,
        "dl_display": "982 MB",
        "ram_mb": 1800,
        "ram_display": "1.2 GB",
        "min_ram_gb": 4,
        "speed_rating": "⚡⚡⚡⚡",
        "speed_tok": "~65-80 tok/s",
        "description": "Top recommendation for 8GB Macs. Instant streaming with strong multilingual, reasoning, and summarization skills.",
    },
    "qwen-2.5-3b": {
        "id": "qwen-2.5-3b",
        "brand": "QWEN",
        "brand_color": "#7c3aed",
        "name": "Qwen2.5 3B Instruct",
        "provider": "local",
        "params": "3.1B",
        "dl_mb": 1980,
        "dl_display": "1.93 GB",
        "ram_mb": 3500,
        "ram_display": "2.2 GB",
        "min_ram_gb": 8,
        "speed_rating": "⚡⚡⚡",
        "speed_tok": "~35-50 tok/s",
        "description": "Exceptional depth for complex creative writing, precise multi-turn dialogues, and in-depth reasoning.",
    },
    "llama-3.2-3b": {
        "id": "llama-3.2-3b",
        "brand": "META",
        "brand_color": "#2563eb",
        "name": "Llama 3.2 3B Instruct",
        "provider": "local",
        "params": "3.2B",
        "dl_mb": 1920,
        "dl_display": "1.88 GB",
        "ram_mb": 3400,
        "ram_display": "2.3 GB",
        "min_ram_gb": 8,
        "speed_rating": "⚡⚡⚡",
        "speed_tok": "~35-48 tok/s",
        "description": "Meta's highly capable 3B edge model for multilingual dialogue, agentic tasks, and creative workflows.",
    },
    "llama-3.2-1b": {
        "id": "llama-3.2-1b",
        "brand": "META",
        "brand_color": "#2563eb",
        "name": "Llama 3.2 1B Instruct",
        "provider": "local",
        "params": "1.2B",
        "dl_mb": 1280,
        "dl_display": "1.25 GB",
        "ram_mb": 1800,
        "ram_display": "1.1 GB",
        "min_ram_gb": 4,
        "speed_rating": "⚡⚡⚡⚡",
        "speed_tok": "~70-90 tok/s",
        "description": "Meta's ultra-lightweight 1B edge model for fast summarization, triage, and low-latency agent tasks.",
    },
    "phi-3.5-mini": {
        "id": "phi-3.5-mini",
        "brand": "MICROSOFT",
        "brand_color": "#0891b2",
        "name": "Phi 3.5 Mini 3.8B",
        "provider": "local",
        "params": "3.8B",
        "dl_mb": 2400,
        "dl_display": "2.34 GB",
        "ram_mb": 3600,
        "ram_display": "2.6 GB",
        "min_ram_gb": 8,
        "speed_rating": "⚡⚡⚡",
        "speed_tok": "~30-45 tok/s",
        "description": "Microsoft's compact powerhouse optimized for code reasoning, structured JSON outputs, and dense logic.",
    },
    "qwen-2.5-7b": {
        "id": "qwen-2.5-7b",
        "brand": "QWEN",
        "brand_color": "#7c3aed",
        "name": "Qwen2.5 7B Instruct",
        "provider": "local",
        "params": "7.6B",
        "dl_mb": 4700,
        "dl_display": "4.59 GB",
        "ram_mb": 6500,
        "ram_display": "5.2 GB",
        "min_ram_gb": 16,
        "speed_rating": "⚡⚡",
        "speed_tok": "~20-30 tok/s",
        "description": "High-tier open weights model matching proprietary API quality for complex reasoning and enterprise tools.",
    },
    "mistral-7b": {
        "id": "mistral-7b",
        "brand": "MISTRAL",
        "brand_color": "#ea580c",
        "name": "Mistral 7B Instruct v0.3",
        "provider": "local",
        "params": "7.2B",
        "dl_mb": 4800,
        "dl_display": "4.69 GB",
        "ram_mb": 6800,
        "ram_display": "5.5 GB",
        "min_ram_gb": 16,
        "speed_rating": "⚡⚡",
        "speed_tok": "~20-30 tok/s",
        "description": "Industry benchmark 7B open model with extended context window and superior instruction following.",
    },
}

#: Cloud AI provider models
CLOUD_LLM_MODELS: dict[str, dict[str, Any]] = {
    "gemini": {
        "id": "gemini",
        "brand": "GOOGLE",
        "brand_color": "#1a73e8",
        "name": "Google Gemini 3.1 Flash",
        "provider": "gemini",
        "requires_key": True,
        "key_env": "GEMINI_API_KEY",
        "description": "Google Gemini 3.1 Flash cloud API.",
    },
    "anthropic": {
        "id": "anthropic",
        "brand": "ANTHROPIC",
        "brand_color": "#d97706",
        "name": "Anthropic Claude 3.7 Sonnet",
        "provider": "anthropic",
        "requires_key": True,
        "key_env": "ANTHROPIC_API_KEY",
        "description": "Claude 3.5 / 3.7 Sonnet cloud API or OAuth.",
    },
    "dashscope": {
        "id": "dashscope",
        "brand": "ALIBABA",
        "brand_color": "#ff6a00",
        "name": "Qwen DashScope Cloud",
        "provider": "dashscope",
        "requires_key": True,
        "key_env": "DASHSCOPE_API_KEY",
        "description": "Alibaba DashScope Qwen cloud API.",
    },
}


def is_downloaded(model_id: str) -> bool:
    """Check whether a model has been downloaded to the local instance directory."""
    if not model_id:
        return False
    spec = resolve_llm_spec(model_id)
    key = spec.get("id", model_id) if spec else model_id
    model_dir = paths.MODELS / key
    if model_dir.is_dir() and any(model_dir.iterdir()):
        return True
    return False


def list_downloaded_models() -> list[str]:
    """Return a list of model IDs currently downloaded on disk."""
    if not paths.MODELS.is_dir():
        return []
    res = []
    for p in paths.MODELS.iterdir():
        if p.is_dir() and any(p.iterdir()):
            res.append(p.name)
    return res


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

    # Evaluate all STT models
    evaluations = {}
    for key, spec in STT_MODELS.items():
        eval_info = check_capability(key, hw)
        if spec["tier"] == recommended_tier and eval_info["status"] in ("compatible", "warning"):
            eval_info["status"] = "recommended"
            eval_info["reason"] = f"⭐ Recommended: Optimal balance of accuracy and performance for your {total_gb} GB system."
        evaluations[key] = eval_info

    llm_rec = recommend_llm_models(hw)

    return {
        "hardware": hw,
        "recommended_tier": recommended_tier,
        "recommended_model": TIER_TO_MODEL.get(recommended_tier, "small"),
        "models": evaluations,
        "llm": llm_rec,
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


def resolve_llm_spec(model_id: str) -> dict[str, Any] | None:
    """Find the specification for a local or cloud LLM model."""
    name = (model_id or "").strip().lower()
    if name in LLM_MODELS:
        res = dict(LLM_MODELS[name])
        res["key"] = res.get("id", name)
        return res
    if name in CLOUD_LLM_MODELS:
        res = dict(CLOUD_LLM_MODELS[name])
        res["key"] = res.get("id", name)
        return res
    for spec in LLM_MODELS.values():
        if spec["id"] == name or spec["name"].lower() == name:
            res = dict(spec)
            res["key"] = res.get("id")
            return res
    for key, spec in LLM_MODELS.items():
        if key in name or name in key:
            res = dict(spec)
            res["key"] = res.get("id", key)
            return res
    return None


def check_llm_capability(model_id: str, profile: dict[str, Any] | None = None) -> dict[str, Any]:
    """Evaluate whether the host machine can safely download and run a local LLM."""
    spec = resolve_llm_spec(model_id)
    if not spec or spec.get("provider") != "local":
        cloud_spec = CLOUD_LLM_MODELS.get(model_id, {})
        return {
            "id": model_id,
            "key": model_id,
            "brand": cloud_spec.get("brand", "CLOUD"),
            "brand_color": cloud_spec.get("brand_color", "#4f46e5"),
            "name": cloud_spec.get("name", model_id),
            "provider": cloud_spec.get("provider", "cloud"),
            "is_local": False,
            "requires_key": True,
            "can_download": True,
            "can_load": True,
            "is_downloaded": True,
            "status": "compatible",
            "reason": "Cloud API model (requires API key).",
            "disk_mb": 0,
            "ram_mb": 0,
            "dl_display": "Cloud API",
            "ram_display": "0 MB",
            "speed_rating": "⚡⚡⚡⚡",
            "speed_tok": "Fast Cloud",
            "description": cloud_spec.get("description", "Hosted cloud model."),
        }

    hw = profile or get_hardware_profile()
    mem = hw.get("memory", {})
    disk = hw.get("disk", {})

    disk_req = spec.get("disk_mb", 1500)
    ram_req = spec.get("ram_mb", 2000)

    # Storage check
    disk_free_mb = disk.get("free_mb", int(disk.get("free_gb", 0) * 1024))
    disk_safety_need = int(disk_req * DISK_SAFETY_FACTOR)
    has_disk = disk_free_mb >= disk_safety_need

    # Memory check
    total_ram = mem.get("total_mb", int(mem.get("total_gb", 0) * 1024))
    avail_ram = mem.get("available_mb", int(mem.get("available_gb", 0) * 1024))

    has_total_ram = total_ram >= (ram_req + SYSTEM_RAM_RESERVE_MB)
    has_avail_ram = avail_ram >= (ram_req * 0.7)

    can_download = has_disk and has_total_ram
    can_load = has_total_ram and has_avail_ram

    status = "compatible"
    reason = f"Runs locally on your device hardware ({spec.get('params', 'N/A')} parameters)."

    # Format needed vs free RAM for pills
    ram_needed_gb = round(ram_req / 1024, 1)
    ram_free_gb = round(avail_ram / 1024, 2)
    total_ram_gb = round(total_ram / 1024, 1)

    if not has_total_ram:
        status = "blocked"
        can_download = False
        can_load = False
        ram_badge = "blocked"
        ram_pill_color = "danger"
        ram_pill_text = f"Low RAM warning ({ram_needed_gb} GB needed · {ram_free_gb} GB free)"
        reason = (
            f"Blocked: {spec['name']} requires ~{ram_req} MB RAM ({spec.get('min_ram_gb', 8)}GB+ RAM recommended), "
            f"but your machine has {total_ram} MB total RAM ({total_ram_gb} GB). Running this model would exceed "
            f"hardware capabilities."
        )
    elif not has_disk:
        status = "blocked"
        can_download = False
        ram_badge = "blocked"
        ram_pill_color = "danger"
        ram_pill_text = f"Insufficient Disk ({disk_req/1024:.1f} GB needed · {disk_free_mb/1024:.1f} GB free)"
        reason = (
            f"Blocked: Insufficient free storage ({disk.get('free_gb', disk_free_mb/1024):.1f} GB free). "
            f"Downloading {spec['name']} requires ~{disk_req} MB."
        )
    elif not has_avail_ram:
        status = "warning"
        ram_badge = "low_warning"
        ram_pill_color = "danger"
        ram_pill_text = f"Low RAM warning ({ram_needed_gb} GB needed · {ram_free_gb} GB free)"
        reason = (
            f"Low memory headroom: {avail_ram} MB available RAM vs ~{ram_req} MB needed. "
            f"Close heavy applications before running."
        )
    elif avail_ram < (ram_req * 1.3):
        status = "warning"
        ram_badge = "high_usage"
        ram_pill_color = "warning"
        ram_pill_text = f"High RAM usage ({ram_needed_gb} GB needed · {ram_free_gb} GB free)"
        reason = f"High RAM usage: model will take up a significant portion of free RAM."
    else:
        ram_badge = "optimal"
        ram_pill_color = "success"
        ram_pill_text = f"Optimal headroom ({ram_needed_gb} GB needed · {ram_free_gb} GB free)"

    model_downloaded = is_downloaded(spec["id"])

    return {
        "id": spec["id"],
        "brand": spec.get("brand", "LOCAL"),
        "brand_color": spec.get("brand_color", "#7c3aed"),
        "name": spec["name"],
        "provider": "local",
        "is_local": True,
        "requires_key": False,
        "params": spec.get("params", ""),
        "dl_mb": disk_req,
        "dl_gb": round(disk_req / 1024, 2),
        "dl_display": spec.get("dl_display", f"{round(disk_req/1024, 2)} GB"),
        "ram_mb": ram_req,
        "ram_gb": round(ram_req / 1024, 2),
        "ram_display": spec.get("ram_display", f"{round(ram_req/1024, 2)} GB"),
        "ram_needed_gb": ram_needed_gb,
        "ram_free_gb": ram_free_gb,
        "ram_badge": ram_badge,
        "ram_pill_color": ram_pill_color,
        "ram_pill_text": ram_pill_text,
        "min_ram_gb": spec.get("min_ram_gb", 4),
        "speed_rating": spec.get("speed_rating", "⚡⚡⚡"),
        "speed_tok": spec.get("speed_tok", "~35-50 tok/s"),
        "description": spec.get("description", ""),
        "can_download": can_download,
        "can_load": can_load,
        "is_downloaded": model_downloaded,
        "status": status,
        "reason": reason,
    }


def recommend_llm_models(profile: dict[str, Any] | None = None) -> dict[str, Any]:
    """Analyze host resources and dynamically recommend the optimal local LLM."""
    hw = profile or get_hardware_profile()
    total_gb = hw["memory"]["total_gb"]
    is_apple_silicon = hw.get("is_apple_silicon", False) or hw.get("accelerator", {}).get("type") == "apple_silicon"

    # Choose optimal local model based on RAM
    if total_gb >= 16:
        recommended_id = "qwen-2.5-7b" if is_apple_silicon else "llama-3.2-3b"
    elif total_gb >= 8:
        recommended_id = "llama-3.2-3b"
    elif total_gb >= 4:
        recommended_id = "llama-3.2-1b"
    else:
        recommended_id = "llama-3.2-1b"

    evaluations = {}
    for key, spec in LLM_MODELS.items():
        eval_info = check_llm_capability(key, hw)
        if key == recommended_id and eval_info["status"] in ("compatible", "warning"):
            eval_info["status"] = "recommended"
            eval_info["reason"] = f"⭐ Recommended Local LLM: Optimal quality and performance for your {total_gb} GB system."
        evaluations[key] = eval_info

    cloud_models = {}
    for key, spec in CLOUD_LLM_MODELS.items():
        cloud_models[key] = {
            "id": spec["id"],
            "name": spec["name"],
            "provider": spec["provider"],
            "is_local": False,
            "requires_key": True,
            "key_env": spec["key_env"],
            "description": spec["description"],
            "status": "compatible",
            "reason": "Cloud API model (requires API key).",
        }

    return {
        "hardware": hw,
        "recommended_model": recommended_id,
        "recommended_name": LLM_MODELS.get(recommended_id, {}).get("name", recommended_id),
        "recommended_spec": LLM_MODELS.get(recommended_id, {}),
        "models": evaluations,
        "local_models": evaluations,
        "cloud_models": cloud_models,
    }


def validate_llm_action(model_id: str, action: str = "load") -> None:
    """Raise HardwareInsufficientError if downloading or loading local LLM exceeds capabilities."""
    info = check_llm_capability(model_id)
    if not info.get("is_local"):
        return
    if action == "download" and not info["can_download"]:
        logger.warning("refusing download for LLM %s: %s", model_id, info["reason"])
        raise HardwareInsufficientError(info["reason"])
    if action == "load" and not info["can_load"]:
        logger.warning("refusing load for LLM %s: %s", model_id, info["reason"])
        raise HardwareInsufficientError(info["reason"])

