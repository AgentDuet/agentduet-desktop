"""Tests for Hardware Profiling, Dynamic Model Adviser, Capability Gating, and Model Unloading."""

import asyncio
import gc
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# Ensure agentduet_desktop is on sys.path
HERE = pathlib.Path(__file__).parent
ROOT = HERE.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentduet_desktop import hardware, llm, paths, transcribe
from agentduet_desktop.hardware import (
    HardwareInsufficientError,
    STT_MODELS,
    TIER_TO_MODEL,
    check_capability,
    get_accelerator_info,
    get_disk_info,
    get_hardware_profile,
    get_memory_info,
    recommend_models,
    validate_model_action,
)


class TestHardwareProfiling(unittest.TestCase):
    def test_get_memory_info(self):
        mem = get_memory_info()
        self.assertIn("total_mb", mem)
        self.assertIn("available_mb", mem)
        self.assertIn("total_gb", mem)
        self.assertGreater(mem["total_mb"], 0)
        self.assertGreater(mem["available_mb"], 0)

    def test_get_disk_info(self):
        disk = get_disk_info()
        self.assertIn("total_mb", disk)
        self.assertIn("free_mb", disk)
        self.assertIn("free_gb", disk)
        self.assertGreater(disk["total_mb"], 0)

    def test_get_accelerator_info(self):
        accel = get_accelerator_info()
        self.assertIn("type", accel)
        self.assertIn("chip_name", accel)
        self.assertIn("ane_supported", accel)

    def test_get_hardware_profile(self):
        hw = get_hardware_profile()
        self.assertIn("memory", hw)
        self.assertIn("disk", hw)
        self.assertIn("accelerator", hw)
        self.assertIn("chip_name", hw)


class TestModelAdvisorAndGating(unittest.TestCase):
    def test_model_specs_catalog(self):
        self.assertIn("tiny", STT_MODELS)
        self.assertIn("base", STT_MODELS)
        self.assertIn("small", STT_MODELS)
        self.assertIn("medium", STT_MODELS)
        self.assertIn("large-v3", STT_MODELS)
        for name, spec in STT_MODELS.items():
            self.assertGreater(spec["disk_mb"], 0)
            self.assertGreater(spec["ram_mb"], 0)
            self.assertTrue(spec["tier"])

    def test_check_capability_normal_system(self):
        # Base model should be supported on any standard machine
        cap = check_capability("base")
        self.assertIn("can_download", cap)
        self.assertIn("can_load", cap)
        self.assertEqual(cap["tier"], "fast")

    def test_insufficient_ram_blocks_load_and_download(self):
        # Mock low memory (500 MB total, 200 MB available)
        low_mem = {
            "total_mb": 500,
            "available_mb": 200,
            "free_mb": 100,
            "total_gb": 0.5,
            "available_gb": 0.2,
        }
        with patch("agentduet_desktop.hardware.get_memory_info", return_value=low_mem):
            cap = check_capability("large-v3")
            self.assertFalse(cap["can_load"])
            self.assertFalse(cap["can_download"])
            self.assertEqual(cap["status"], "blocked")
            self.assertIn("RAM", cap["reason"])

            # validate_model_action should raise HardwareInsufficientError
            with self.assertRaises(HardwareInsufficientError):
                validate_model_action("large-v3", "download")

            with self.assertRaises(HardwareInsufficientError):
                validate_model_action("large-v3", "load")

    def test_insufficient_disk_blocks_download(self):
        # Mock low disk space (50 MB free)
        low_disk = {
            "total_mb": 100000,
            "free_mb": 50,
            "used_mb": 99950,
            "free_gb": 0.05,
        }
        with patch("agentduet_desktop.hardware.get_disk_info", return_value=low_disk):
            cap = check_capability("medium")
            self.assertFalse(cap["can_download"])
            self.assertIn("storage", cap["reason"].lower())

            with self.assertRaises(HardwareInsufficientError):
                validate_model_action("medium", "download")

    def test_recommend_models_adaptation(self):
        # High-end hardware: recommend max or medium
        high_mem = {
            "total_mb": 32768,
            "available_mb": 24000,
            "free_mb": 20000,
            "total_gb": 32.0,
            "available_gb": 24.0,
        }
        with patch("agentduet_desktop.hardware.get_memory_info", return_value=high_mem):
            rec = recommend_models()
            self.assertIn(rec["recommended_tier"], ("accurate", "max"))
            self.assertIn(rec["recommended_model"], ("medium", "large-v3"))

        # Low-end hardware (2 GB RAM): recommend fast/base
        low_mem = {
            "total_mb": 2048,
            "available_mb": 1200,
            "free_mb": 800,
            "total_gb": 2.0,
            "available_gb": 1.2,
        }
        with patch("agentduet_desktop.hardware.get_memory_info", return_value=low_mem):
            rec = recommend_models()
            self.assertEqual(rec["recommended_tier"], "fast")
            self.assertEqual(rec["recommended_model"], "base")


class TestModelUnloadLifecycle(unittest.TestCase):
    def setUp(self):
        # Reset any loaded model state before test
        transcribe._local_model = None
        transcribe._loaded_name = ""

    def tearDown(self):
        transcribe._local_model = None
        transcribe._loaded_name = ""

    def test_transcribe_unload_when_not_loaded(self):
        was_loaded, model_name, freed_mb = transcribe.unload()
        self.assertFalse(was_loaded)
        self.assertEqual(model_name, "")
        self.assertEqual(freed_mb, 0)
        self.assertFalse(transcribe.is_loaded())
        self.assertEqual(transcribe.loaded_model(), "")

        info = transcribe.loaded_info()
        self.assertFalse(info["loaded"])
        self.assertEqual(info["model"], "")

    def test_transcribe_unload_when_loaded(self):
        # Simulate a loaded model
        fake_model = MagicMock()
        transcribe._local_model = fake_model
        transcribe._loaded_name = "small"

        self.assertTrue(transcribe.is_loaded())
        self.assertEqual(transcribe.loaded_model(), "small")

        info = transcribe.loaded_info()
        self.assertTrue(info["loaded"])
        self.assertEqual(info["model"], "small")
        self.assertEqual(info["ram_mb"], 1000)

        was_loaded, model_name, freed_mb = transcribe.unload()
        self.assertTrue(was_loaded)
        self.assertEqual(model_name, "small")
        self.assertEqual(freed_mb, 1000)
        self.assertIsNone(transcribe._local_model)
        self.assertEqual(transcribe._loaded_name, "")
        self.assertFalse(transcribe.is_loaded())

    def test_transcribe_fetch_gated_by_hardware(self):
        # Mock low memory to ensure fetch() raises HardwareInsufficientError
        low_mem = {
            "total_mb": 500,
            "available_mb": 200,
            "free_mb": 100,
            "total_gb": 0.5,
            "available_gb": 0.2,
        }
        with patch("agentduet_desktop.hardware.get_memory_info", return_value=low_mem):
            with self.assertRaises(HardwareInsufficientError):
                transcribe.fetch("large-v3")

    def test_transcribe_load_gated_by_hardware(self):
        low_mem = {
            "total_mb": 500,
            "available_mb": 200,
            "free_mb": 100,
            "total_gb": 0.5,
            "available_gb": 0.2,
        }
        with patch("agentduet_desktop.hardware.get_memory_info", return_value=low_mem):
            with self.assertRaises(HardwareInsufficientError):
                transcribe._load("large-v3")

    def test_llm_unload(self):
        # Populate llm._cached with dummy clients
        llm._cached["dummy1"] = MagicMock()
        llm._cached["dummy2"] = MagicMock()
        self.assertGreater(len(llm._cached), 0)

        llm.unload()
        self.assertEqual(len(llm._cached), 0)


class TestWebAPIHardwareAndUnload(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Set up a test aiohttp application
        from aiohttp import web
        from agentduet_desktop import web as web_module

        self.token = "test-token"
        self.app = web_module.make_app(chat=None, token=self.token)

    async def test_api_hardware_endpoint(self):
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(self.app)) as client:
            resp = await client.get(f"/api/hardware?t={self.token}")
            self.assertEqual(resp.status, 200)
            data = await resp.json()
            self.assertIn("hardware", data)
            self.assertIn("models", data)
            self.assertIn("recommended_tier", data)
            self.assertIn("loaded_model", data)

    async def test_api_model_unload_endpoint(self):
        from aiohttp.test_utils import TestClient, TestServer

        # Simulate loaded transcribe model
        transcribe._local_model = MagicMock()
        transcribe._loaded_name = "small"

        async with TestClient(TestServer(self.app)) as client:
            resp = await client.post(f"/api/model/unload?t={self.token}")
            self.assertEqual(resp.status, 200)
            data = await resp.json()
            self.assertTrue(data["ok"])
            self.assertTrue(data["stt_unloaded"])
            self.assertEqual(data["model_unloaded"], "small")
            self.assertFalse(transcribe.is_loaded())

    async def test_api_setup_stt_hardware_gating(self):
        from aiohttp.test_utils import TestClient, TestServer

        # Mock low memory so STT download is blocked
        low_mem = {
            "total_mb": 500,
            "available_mb": 200,
            "free_mb": 100,
            "total_gb": 0.5,
            "available_gb": 0.2,
        }
        with patch("agentduet_desktop.hardware.get_memory_info", return_value=low_mem):
            with patch("agentduet_desktop.transcribe.local_model", return_value="large-v3"):
                async with TestClient(TestServer(self.app)) as client:
                    resp = await client.post(f"/api/setup/stt?t={self.token}")
                    self.assertEqual(resp.status, 400)
                    data = await resp.json()
                    self.assertFalse(data["ok"])
                    self.assertTrue(data["blocked"])
                    self.assertIn("RAM", data["error"])

    async def test_api_setup_setting_gating_transcription_quality(self):
        from aiohttp.test_utils import TestClient, TestServer

        # Mock low memory so 'max' tier is blocked
        low_mem = {
            "total_mb": 500,
            "available_mb": 200,
            "free_mb": 100,
            "total_gb": 0.5,
            "available_gb": 0.2,
        }
        with patch("agentduet_desktop.hardware.get_memory_info", return_value=low_mem):
            async with TestClient(TestServer(self.app)) as client:
                resp = await client.post(
                    f"/api/setup/setting?t={self.token}",
                    json={"field": "transcription", "value": "max"},
                )
                data = await resp.json()
                self.assertFalse(data["ok"])
                self.assertIn("Hardware capability exceeded", data["message"])


if __name__ == "__main__":
    unittest.main()
