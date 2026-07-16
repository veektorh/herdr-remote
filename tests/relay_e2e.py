#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["websockets>=14.0"]
# ///
"""Network-level relay tests using a real WebSocket server and client."""

import asyncio
import json
import os
import pathlib
import sys
import tempfile
import unittest


TEMP_DIR = tempfile.TemporaryDirectory()
os.environ.update({
    "HERDR_LOG_DIR": TEMP_DIR.name,
    "HERDR_CONFIG_DIR": TEMP_DIR.name,
    "HERDR_RELAY_TOKEN": "e2e-secret",
    "HERDR_ALLOWED_ORIGINS": "https://app.example",
    "HERDR_VAPID_PUBLIC": "test-public",
    "HERDR_VAPID_PRIVATE": "test-private",
})
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "relay"))

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import InvalidStatus

import herdr_relay as relay


class RelayNetworkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_runner = relay.run_herdr_result
        self.commands = []

        def fake_runner(*args, remote=None):
            self.commands.append((args, remote))
            return True, ""

        relay.run_herdr_result = fake_runner
        relay.known_panes.add("pane-e2e")
        self.server = await serve(
            relay.handle_client,
            "127.0.0.1",
            0,
            process_request=relay.process_request,
            subprotocols=["herdr-v1"],
        )
        self.port = self.server.sockets[0].getsockname()[1]
        self.uri = f"ws://127.0.0.1:{self.port}"

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()
        relay.run_herdr_result = self.original_runner
        relay.known_panes.discard("pane-e2e")
        relay.clients.clear()

    async def test_handshake_requires_authentication_and_allowed_origin(self):
        with self.assertRaises(InvalidStatus) as missing:
            async with connect(self.uri, subprotocols=["herdr-v1"]):
                pass
        self.assertEqual(missing.exception.response.status_code, 401)

        with self.assertRaises(InvalidStatus) as untrusted:
            async with connect(
                self.uri,
                subprotocols=["herdr-v1", "herdr-auth.e2e-secret"],
                origin="https://evil.example",
            ):
                pass
        self.assertEqual(untrusted.exception.response.status_code, 403)

    async def test_validated_command_receives_correlated_acknowledgement(self):
        async with connect(
            self.uri,
            subprotocols=["herdr-v1", "herdr-auth.e2e-secret"],
            origin="https://app.example",
        ) as websocket:
            self.assertEqual(websocket.subprotocol, "herdr-v1")
            await websocket.send(json.dumps({
                "type": "submit_text",
                "pane_id": "pane-e2e",
                "text": "hello from e2e",
                "request_id": "e2e_123",
            }))
            result = json.loads(await asyncio.wait_for(websocket.recv(), timeout=2))
            self.assertEqual(result, {
                "type": "command_result",
                "action": "submit_text",
                "ok": True,
                "pane_id": "pane-e2e",
                "request_id": "e2e_123",
            })
            self.assertEqual(self.commands, [
                (("pane", "send-text", "pane-e2e", "hello from e2e"), None),
                (("pane", "send-keys", "pane-e2e", "Enter"), None),
            ])

            await websocket.send(json.dumps({
                "type": "send_keys", "pane_id": "pane-e2e", "keys": ["F12"],
            }))
            rejected = json.loads(await asyncio.wait_for(websocket.recv(), timeout=2))
            self.assertEqual(rejected["type"], "error")
            self.assertIn("disallowed", rejected["message"])


if __name__ == "__main__":
    unittest.main()
