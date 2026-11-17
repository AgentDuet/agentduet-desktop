"""Issue #16: switching and disconnecting connections, without a real phone line.

Run: python tests/test_connector.py. All files, credentials and SDK sessions are disposable.
The HTTP routes and channel lifecycle are real; only the platform connection is replaced.
"""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


class ConnectorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from agentduet_desktop import callmode, connector, oauth, owner, paths, settings, status, web
        from agentduet_desktop import secretary_agent
        from aiohttp.test_utils import TestClient, TestServer

        self.connector, self.oauth, self.owner = connector, oauth, owner
        self.paths, self.settings, self.status, self.sa = paths, settings, status, secretary_agent
        self.tmp = tempfile.TemporaryDirectory(prefix="connector-case-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patches = [
            mock.patch.object(paths, "ENV_FILE", self.root / ".env"),
            mock.patch.object(paths, "SETTINGS", self.root / "settings.md"),
            mock.patch.object(owner, "PROFILE", self.root / "settings.md"),
            mock.patch.object(oauth, "STORE", self.root / "oauth.json"),
            mock.patch.object(status, "NUMBER_FILE", self.root / "channel-number"),
            mock.patch.dict(status._state, number="", channel="unset"),
            mock.patch.object(connector, "_revision", 0),
            mock.patch.object(connector, "KEY_FILE", self.root / "shared-key"),
            mock.patch.object(connector, "UUID_FILE", self.root / "shared-uuid"),
            mock.patch.object(secretary_agent, "CONNECTOR_POLL_SECONDS", 0.01),
            mock.patch.dict(os.environ, {}, clear=False),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        for key in (connector.API_KEY, connector.UUID, "OWNER_PHONE", "SECRETARY_CALLS"):
            os.environ.pop(key, None)
        paths.SETTINGS.write_text("# Settings\n\n## Calls\ncarry\n\n## Phone\n+6599999999\n")
        self.key, self.uuid = "test-key-old", "11111111-1111-4111-8111-111111111111"
        self.new_uuid = "22222222-2222-4222-8222-222222222222"
        settings.save_connector(self.key, self.uuid)
        status.set_number("+6560000001")
        connector.KEY_FILE.write_text("shared-test-key")
        connector.UUID_FILE.write_text("shared-test-uuid")
        callmode.release()
        self.addCleanup(callmode.release)
        self.client = TestClient(TestServer(web.make_app("test-token")))
        self.addAsyncCleanup(self.client.close)
        await self.client.start_server()

    async def post(self, route, body=None):
        response = await self.client.post(route, json=body or {}, headers={"X-Token": "test-token"})
        self.assertEqual(response.status, 200)
        return await response.json()

    def number(self):
        return self.status.snapshot()["number"]

    async def test_changed_pair_clears_number_and_next_call_can_learn(self):
        for key, uuid in [("test-key-new", self.uuid), ("test-key-new", self.new_uuid)]:
            with self.subTest(key=key, uuid=uuid), mock.patch.object(
                    self.connector, "verify", mock.AsyncMock(return_value=(True, "checked"))):
                result = await self.post("/api/setup/connector", {"key": key, "uuid": uuid})
                self.assertTrue(result["ok"])
                self.assertEqual(self.number(), "")
                self.assertEqual(self.status.NUMBER_FILE.read_text(), "")
                self.status.set_number("+6560000002")
                self.assertEqual(self.number(), "+6560000002")
        # An explicit callback number is separate from the learned line.
        self.assertEqual(self.owner.phone(), "+6599999999")

    async def test_identical_pair_preserves_number_and_connection(self):
        before = self.connector.connection_state()
        result = await self.post("/api/setup/connector", {"key": f" {self.key} ", "uuid": self.uuid})
        self.assertTrue(result["ok"])
        self.assertEqual(self.number(), "+6560000001")
        self.assertEqual(self.connector.connection_state(), before)

    async def test_failed_verification_changes_nothing(self):
        before = self.paths.ENV_FILE.read_bytes(), self.connector.connection_state()
        with mock.patch.object(self.connector, "verify", mock.AsyncMock(return_value=(False, "bad key"))):
            result = await self.post("/api/setup/connector", {"key": "bad-key", "uuid": self.new_uuid})
        self.assertFalse(result["ok"])
        self.assertEqual((self.paths.ENV_FILE.read_bytes(), self.connector.connection_state()), before)
        self.assertEqual(self.number(), "+6560000001")

    async def test_disconnect_is_authenticated_idempotent_and_preserves_data(self):
        self.settings._write_env({"UNRELATED_SETTING": "keep"})
        recording = self.root / "recording.m4a"
        recording.write_bytes(b"recorded audio")
        profile = self.paths.SETTINGS.read_bytes()
        response = await self.client.post("/api/connector/disconnect")
        self.assertEqual(response.status, 401)
        self.assertTrue(self.connector.configured())
        self.assertEqual(self.number(), "+6560000001")
        for _ in range(2):
            self.assertTrue((await self.post("/api/connector/disconnect"))["ok"])
        self.assertFalse(self.connector.configured())
        self.assertEqual(self.paths.ENV_FILE.read_text(), "UNRELATED_SETTING=keep\n")
        for key in (self.connector.API_KEY, self.connector.UUID):
            self.assertNotIn(key, os.environ)
        self.assertEqual(self.number(), "")
        self.assertEqual(recording.read_bytes(), b"recorded audio")
        self.assertEqual(self.paths.SETTINGS.read_bytes(), profile)
        self.assertEqual(self.connector.fill_from_files("", ""), ("shared-test-key", "shared-test-uuid"))

    async def test_restart_cannot_restore_disconnected_credentials_or_old_number(self):
        sessions = self.root / "sessions.json"
        sessions.write_text(json.dumps({"old": {"subscriber": "+6560000001", "last_seen": "2026-10-23"}}))
        await self.post("/api/connector/disconnect")
        # A fresh interpreter proves this is persisted, rather than just the in-memory reset.
        script = """
import json, sys
from pathlib import Path
from dotenv import load_dotenv
from agentduet_desktop import connector, status
root = Path(sys.argv[1])
load_dotenv(root / '.env')
status.NUMBER_FILE = root / 'channel-number'
status.load_number(root / 'sessions.json')
print(json.dumps([connector.configured(), status.snapshot()['number']]))
"""
        result = subprocess.run([sys.executable, "-c", script, str(self.root)],
                                capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), [False, ""])
        self.assertTrue(sessions.exists())

    async def test_oauth_disconnect_refused_and_signout_still_works(self):
        self.oauth._write({"refresh_token": "test-refresh", "connector_uuid": self.new_uuid})
        before = self.paths.ENV_FILE.read_bytes(), self.number()
        self.assertFalse((await self.post("/api/connector/disconnect"))["ok"])
        self.assertTrue(self.oauth.signed_in())
        self.assertEqual((self.paths.ENV_FILE.read_bytes(), self.number()), before)
        self.assertTrue((await self.post("/api/connector/signout"))["ok"])
        self.assertFalse(self.oauth.signed_in())

    async def test_disk_failure_is_reported(self):
        with mock.patch.object(self.settings, "_forget_env", side_effect=OSError("read-only")):
            self.assertFalse((await self.post("/api/connector/disconnect"))["ok"])
        self.assertTrue(self.connector.configured())

    async def test_connection_closes_for_change_and_disconnect_even_if_pair_restored(self):
        for action in ("key", "uuid", "disconnect", "restore"):
            with self.subTest(action=action):
                self.settings.save_connector(self.key, self.uuid)
                connection = self.connector.connection_state()
                sm = mock.Mock(disconnect=mock.AsyncMock())
                watch = asyncio.create_task(self.sa._close_when_signed_out(sm, connection))
                await asyncio.sleep(0.03)
                sm.disconnect.assert_not_awaited()
                if action == "key":
                    self.settings.save_connector("test-key-new", self.uuid)
                elif action == "uuid":
                    self.settings.save_connector(self.key, self.new_uuid)
                else:
                    await self.post("/api/connector/disconnect")
                    if action == "restore":
                        self.settings.save_connector(self.key, self.uuid)
                await asyncio.wait_for(watch, 1)
                sm.disconnect.assert_awaited_once()

    async def test_oauth_refresh_does_not_reconnect(self):
        self.oauth._write({"refresh_token": "first", "connector_uuid": self.uuid})
        before = self.connector.connection_state()
        self.oauth._write({"refresh_token": "rotated", "connector_uuid": self.uuid})
        self.assertEqual(self.connector.connection_state(), before)

    async def test_channel_switch_uses_new_pair_and_rejects_queued_old_calls(self):
        from agentduet_desktop import carry

        class SessionManager:
            def __init__(self, config):
                self.config = config
                self.running, self.closed = asyncio.Event(), asyncio.Event()
                managers.append(self)

            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            def on_incoming_call(self, callback): self.incoming = callback
            def on_outgoing_call(self, callback): self.outgoing = callback
            async def setup_trigger_conditions(self, conditions): pass
            async def disconnect(self): self.closed.set()
            async def open_session(self, *args):
                # Number learning happens when the notification arrives, even if the caller
                # has already hung up by the time the app attaches. No audio or real network.
                raise RuntimeError("simulated call already ended")
            async def run_forever(self, **kwargs):
                self.running.set()
                await self.closed.wait()

        async def wait_running():
            async with asyncio.timeout(1):
                while not managers:
                    await asyncio.sleep(0)
                await managers[-1].running.wait()
            return managers[-1]

        managers = []
        with mock.patch.object(self.sa, "SessionManager", SessionManager), \
             mock.patch.object(self.owner, "ai_on", return_value=False), \
             mock.patch.object(carry, "handle", wraps=carry.handle) as handle:
            channel = asyncio.create_task(self.sa.run_channel())
            old = await wait_running()
            # Queue a notification, then change credentials BEFORE its task executes.
            await old.incoming(mock.Mock(subscriber_id="+6560000001"))
            self.settings.save_connector("test-key-new", self.new_uuid)
            await asyncio.wait_for(channel, 1)
            handle.assert_not_awaited()
            self.assertEqual(self.number(), "")

            managers.clear()
            channel = asyncio.create_task(self.sa.run_channel())
            new = await wait_running()
            self.assertEqual(new.config.api_key, "test-key-new")
            self.assertEqual(new.config.connector_uuid, self.new_uuid)
            await new.outgoing(mock.Mock(subscriber_id="+6560000002"))
            await asyncio.sleep(0)
            self.assertEqual(self.number(), "+6560000002")
            self.assertEqual(self.status.NUMBER_FILE.read_text(), "+6560000002")
            await self.post("/api/connector/disconnect")
            await asyncio.wait_for(channel, 1)
            self.assertEqual(self.number(), "")


if __name__ == "__main__":
    # Set before the first package import: paths seeds an instance on import.
    with tempfile.TemporaryDirectory(prefix="connector-suite-") as home, mock.patch.dict(
            os.environ, {"AGENTDUET_HOME": home, "AGENTDUET_EDITION": "recorder", "AGENTDUET_QUIET": "1"}):
        unittest.main()
