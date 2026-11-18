"""Exercise the Windows page's authenticated playback and setup contract on any host."""
import os
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer


class RecorderWindow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, AGENTDUET_HOME=tmp, AGENTDUET_EDITION="recorder"))
        from agentduet_desktop import edition, web
        self.enterContext(patch.object(edition, "windows_recorder", return_value=True))
        self.client = TestClient(TestServer(web.make_app("local-token")))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.headers = {"X-Token": "local-token"}
        self.folder = Path(tmp)

    async def test_page_setup_and_audio(self):
        from agentduet_desktop import calls, carry, legal, oauth
        page = await self.client.get("/", headers=self.headers)
        self.assertIn("<title>AgentDuet Recorder</title>", await page.text())
        with patch.object(oauth, "signed_in", return_value=False):
            result = await self.client.post("/api/setup/finish", headers=self.headers)
            self.assertFalse((await result.json())["ok"])
        result = await self.client.post("/api/terms", json={"version": legal.VERSION}, headers=self.headers)
        self.assertTrue((await result.json())["ok"])
        with patch.object(oauth, "signed_in", return_value=True):
            result = await self.client.post("/api/setup/finish", headers=self.headers)
            self.assertTrue((await result.json())["ok"])

        path = self.folder / "call.wav"
        with wave.open(str(path), "wb") as audio:
            audio.setparams((2, 2, 24000, 0, "NONE", "uncompressed"))
            audio.writeframes(bytes(96000))
        url = "/api/recording?id=test"
        self.assertEqual((await self.client.get(url)).status, 401)
        self.assertEqual((await self.client.get(url, headers=self.headers)).status, 404)
        with patch.object(calls, "get", return_value={"call_id": "test"}), patch.object(
                carry, "call_audio", return_value=(self.folder, [path.name])):
            response = await self.client.get(url, headers={**self.headers, "Range": "bytes=0-3"})
            self.assertEqual(response.status, 206)
            self.assertEqual(await response.read(), b"RIFF")


if __name__ == "__main__":
    unittest.main()
