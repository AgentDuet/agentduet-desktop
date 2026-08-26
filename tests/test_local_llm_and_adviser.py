from __future__ import annotations

import os
import pathlib
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

# Ensure agentduet_desktop is on sys.path
HERE = pathlib.Path(__file__).parent
ROOT = HERE.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

try:
    from aiohttp import web
    from aiohttp.test_utils import AioHTTPTestCase
    from agentduet_desktop import web as web_module
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False
    web_module = None  # type: ignore
    class AioHTTPTestCase(unittest.TestCase):  # type: ignore
        pass

from agentduet_desktop import hardware, llm, owner, paths, tools


class TestHardwareLLMAdviser(unittest.TestCase):
    """Test hardware profiling, specs catalog, and dynamic LLM recommendations."""

    def test_llm_catalog_entries(self):
        self.assertIn("llama-3.2-1b", hardware.LLM_MODELS)
        self.assertIn("qwen-2.5-1.5b", hardware.LLM_MODELS)
        self.assertIn("llama-3.2-3b", hardware.LLM_MODELS)
        self.assertIn("qwen-2.5-7b", hardware.LLM_MODELS)
        self.assertIn("mistral-7b", hardware.LLM_MODELS)

    def test_resolve_llm_spec(self):
        spec = hardware.resolve_llm_spec("llama-3.2-1b")
        self.assertIsNotNone(spec)
        self.assertIn("Llama 3.2 1B", spec["name"])
        self.assertEqual(spec["ram_mb"], 1800)

        # Inferred / partial match
        spec2 = hardware.resolve_llm_spec("mistral-7b-instruct")
        self.assertIsNotNone(spec2)
        self.assertEqual(spec2["key"], "mistral-7b")

    def test_recommend_llm_low_ram(self):
        # 4 GB system RAM -> should recommend 1B model, block 7B models
        low_hw = {
            "chip_name": "Apple M1",
            "cpu_count": 8,
            "memory": {"total_gb": 4.0, "available_gb": 2.5, "used_percent": 37.5},
            "disk": {"total_gb": 256.0, "free_gb": 50.0, "used_percent": 80.0},
            "accelerator": {"type": "apple_silicon", "available": True},
        }
        res = hardware.recommend_llm_models(low_hw)
        self.assertIn(res["recommended_model"], ("llama-3.2-1b", "qwen-2.5-1.5b"))
        self.assertEqual(res["models"]["mistral-7b"]["status"], "blocked")
        self.assertEqual(res["models"]["qwen-2.5-7b"]["status"], "blocked")

    def test_recommend_llm_high_ram(self):
        # 32 GB system RAM -> capable of 7B models
        high_hw = {
            "chip_name": "Apple M2 Max",
            "cpu_count": 12,
            "memory": {"total_gb": 32.0, "available_gb": 24.0, "used_percent": 25.0},
            "disk": {"total_gb": 1000.0, "free_gb": 500.0, "used_percent": 50.0},
            "accelerator": {"type": "apple_silicon", "available": True},
        }
        res = hardware.recommend_llm_models(high_hw)
        self.assertIn(res["models"]["mistral-7b"]["status"], ("compatible", "recommended"))
        self.assertIn(res["models"]["llama-3.2-1b"]["status"], ("compatible", "recommended"))

    def test_check_llm_capability_gating(self):
        with patch.object(hardware, "get_hardware_profile") as mock_hw:
            mock_hw.return_value = {
                "memory": {"total_gb": 4.0, "available_gb": 1.0},
                "disk": {"free_gb": 2.0},
            }
            # 7B model requires 16GB RAM and 5GB disk -> should be blocked
            cap = hardware.check_llm_capability("mistral-7b")
            self.assertFalse(cap["can_load"])
            self.assertFalse(cap["can_download"])
            self.assertIn("exceed", cap["reason"].lower())

            with self.assertRaises((ValueError, hardware.HardwareInsufficientError)):
                hardware.validate_llm_action("mistral-7b", "load")


class TestLocalLLMLifecycle(unittest.TestCase):
    """Test _LocalLLM class and resident memory lifecycle in llm.py."""

    def setUp(self):
        llm.unload()

    def tearDown(self):
        llm.unload()

    def test_provider_inference(self):
        self.assertEqual(llm.provider("llama-3.2-1b"), "local")
        self.assertEqual(llm.provider("qwen-2.5-3b"), "local")
        self.assertEqual(llm.provider("mistral-7b"), "local")
        self.assertEqual(llm.provider("phi-3.5-mini"), "local")
        self.assertEqual(llm.provider("claude-sonnet-5"), "anthropic")
        self.assertEqual(llm.provider("gemini-3.1-flash"), "gemini")
        self.assertEqual(llm.provider("qwen3"), "dashscope")

    def test_local_configured_and_key_name(self):
        self.assertTrue(llm.configured("llama-3.2-1b"))
        self.assertEqual(llm.key_name("llama-3.2-1b"), "")

    def test_local_llm_verify_and_describe(self):
        ok, msg = llm.verify("llama-3.2-1b")
        self.assertTrue(ok)
        self.assertIn("Working locally", msg)

        desc = llm.describe("llama-3.2-1b")
        self.assertIn("local/llama-3.2-1b", desc)
        self.assertIn("on-device hardware", desc)

    def test_local_llm_load_and_unload(self):
        self.assertFalse(llm.is_loaded())
        info = llm.load("llama-3.2-1b")
        self.assertTrue(info["loaded"])
        self.assertEqual(info["model"], "llama-3.2-1b")
        self.assertTrue(llm.is_loaded())
        self.assertEqual(llm.loaded_model(), "llama-3.2-1b")

        was_loaded, model_name, freed_mb = llm.unload()
        self.assertTrue(was_loaded)
        self.assertEqual(model_name, "llama-3.2-1b")
        self.assertGreaterEqual(freed_mb, 1800)
        self.assertFalse(llm.is_loaded())

    def test_local_llm_download_and_delete(self):
        model_id = "qwen-2.5-1.5b"
        # Download
        res = llm.download(model_id)
        self.assertTrue(res["ok"])
        self.assertEqual(res["model"], model_id)
        self.assertTrue(llm.is_downloaded(model_id))

        # Delete
        del_res = llm.delete(model_id)
        self.assertTrue(del_res["ok"])
        self.assertFalse(llm.is_downloaded(model_id))

    def test_tools_attach_local_model(self):
        with patch.object(tools, "_write_env") as mock_write:
            out = tools.attach_model("", "llama-3.2-1b")
            self.assertIn("Attached local model llama-3.2-1b", out)
            self.assertIn("no API key needed", out)
            mock_write.assert_called_once()

    def test_owner_connect_ai(self):
        with patch.dict(os.environ, {"SECRETARY_CONNECT_AI": "yes"}):
            self.assertTrue(owner.connect_ai())
        with patch.dict(os.environ, {"SECRETARY_CONNECT_AI": "no"}):
            self.assertFalse(owner.connect_ai())


@unittest.skipUnless(HAS_AIOHTTP, "aiohttp required for Web API tests")
class TestWebAPIEndpoints(AioHTTPTestCase):
    """Test REST API endpoints in web.py for Hardware Profiler, Local LLM Load/Unload, and Connect AI."""

    async def get_application(self):
        self.token = "testtoken123"
        return web_module.make_app(chat=None, token=self.token)

    async def test_api_hardware_endpoint(self):
        resp = await self.client.get(f"/api/hardware?t={self.token}")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertIn("hardware", data)
        self.assertIn("llm", data)
        self.assertIn("loaded_llm", data)
        self.assertIn("loaded_model", data)

    async def test_api_llm_download_and_delete_endpoints(self):
        # 1. Download
        resp = await self.client.post(f"/api/llm/download?t={self.token}", json={"model": "qwen-2.5-1.5b", "auto_load": False})
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertTrue(data["ok"])
        self.assertIn("Downloaded", data["message"])

        # 2. Delete
        del_resp = await self.client.post(f"/api/llm/delete?t={self.token}", json={"model": "qwen-2.5-1.5b"})
        self.assertEqual(del_resp.status, 200)
        del_data = await del_resp.json()
        self.assertTrue(del_data["ok"])
        self.assertIn("Deleted", del_data["message"])

    async def test_api_llm_load_and_unload_endpoints(self):
        # 1. Load local LLM
        resp = await self.client.post(f"/api/llm/load?t={self.token}", json={"model": "llama-3.2-1b"})
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertTrue(data["ok"])
        self.assertIn("Successfully loaded", data["message"])
        self.assertTrue(data["loaded_info"]["loaded"])

        # 2. Check panel shows resident loaded info
        panel_resp = await self.client.get(f"/api/panel?t={self.token}")
        panel_data = await panel_resp.json()
        self.assertTrue(panel_data["model"]["loaded_info"]["loaded"])

        # 3. Unload local LLM
        unload_resp = await self.client.post(f"/api/llm/unload?t={self.token}", json={})
        self.assertEqual(unload_resp.status, 200)
        unload_data = await unload_resp.json()
        self.assertTrue(unload_data["ok"])
        self.assertTrue(unload_data["llm_unloaded"])
        self.assertIn("Unloaded", unload_data["message"])

    async def test_api_llm_load_blocked_beyond_hardware(self):
        with patch.object(hardware, "check_llm_capability") as mock_cap:
            mock_cap.return_value = {
                "can_load": False,
                "reason": "Exceeds available system RAM (requires 16 GB, available 4 GB)",
            }
            resp = await self.client.post(f"/api/llm/load?t={self.token}", json={"model": "mistral-7b"})
            self.assertEqual(resp.status, 400)
            data = await resp.json()
            self.assertFalse(data["ok"])
            self.assertTrue(data["blocked"])
            self.assertIn("Exceeds available system RAM", data["error"])

    async def test_api_setup_connect_ai_setting(self):
        resp = await self.client.post(f"/api/setup/setting?t={self.token}", json={
            "field": "connect_ai",
            "value": "yes"
        })
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertTrue(data["ok"])


if __name__ == "__main__":
    unittest.main()
