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

    async def test_native_folder_selection(self):
        folder = self.folder / "Call archive"
        folder.mkdir()
        choice = {"field": "recordings", "value": str(folder)}
        response = await self.client.post("/api/setup/setting", json=choice)
        self.assertEqual(response.status, 401)
        response = await self.client.post("/api/setup/setting", json=choice, headers=self.headers)
        self.assertTrue((await response.json())["ok"])
        current = await self.client.get("/api/setup/current", headers=self.headers)
        self.assertEqual(Path((await current.json())["carried_dir"]), folder)

    async def test_windows_release_comparison(self):
        from agentduet_desktop import update

        installer = {"name": "agentduet-recorder-windows-x86_64-setup.exe"}
        def release(version, assets):
            return {"tag_name": "recorder-v" + version, "assets": assets,
                    "published_at": "2099-01-01T00:00:00Z"}

        self.enterContext(patch.object(update, "CACHE", self.folder / "updates.json"))
        self.enterContext(patch.object(update, "__version__", "0.1.0b11"))
        self.enterContext(patch.object(update, "_built_at", return_value=update.datetime.now(update.timezone.utc)))
        for feed, expected, newer in [
            ([release("0.1.0b12", []), release("0.1.0b11", [installer])], "0.1.0b11", False),
            ([release("0.1.0b12", [installer])], "0.1.0b12", True),
            ([release("0.1.0b12", [])], "", False),
        ]:
            with patch.object(update, "_fetch", return_value=feed):
                result = await self.client.post("/api/about/check", headers=self.headers)
            row = (await result.json())["update"]
            self.assertEqual((row["version"], row["newer"]), (expected, newer))
            self.assertTrue(row["checked"])
            about = await self.client.get("/api/about", headers=self.headers)
            self.assertEqual((await about.json())["update"], row)

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

        from agentduet_desktop import connector
        pair = {"uuid": "test-uuid", "key": "test-key"}
        with patch.object(oauth, "signed_in", return_value=False), patch.object(
                connector, "verify", return_value=(True, "verified")) as verify:
            result = await self.client.post("/api/setup/connector", json=pair, headers=self.headers)
            self.assertTrue((await result.json())["ok"])
            verify.assert_awaited_once_with("test-key", "test-uuid")
            current = await self.client.get("/api/setup/current", headers=self.headers)
            self.assertTrue((await current.json())["connector_configured"])
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
