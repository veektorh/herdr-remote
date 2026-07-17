import asyncio
import importlib
import os
import stat
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


class StubHeaders:
    def __init__(self, items=()):
        self._items = list(items)

    def get(self, name, default=None):
        wanted = name.lower()
        for key, value in self._items:
            if key.lower() == wanted:
                return value
        return default

    def raw_items(self):
        return iter(self._items)


class StubResponse:
    def __init__(self, status_code, reason_phrase, headers, body):
        self.status_code = status_code
        self.reason_phrase = reason_phrase
        self.headers = headers
        self.body = body


def install_websockets_stubs():
    modules = {
        "websockets": types.ModuleType("websockets"),
        "websockets.asyncio": types.ModuleType("websockets.asyncio"),
        "websockets.asyncio.server": types.ModuleType("websockets.asyncio.server"),
        "websockets.exceptions": types.ModuleType("websockets.exceptions"),
        "websockets.http11": types.ModuleType("websockets.http11"),
        "websockets.datastructures": types.ModuleType("websockets.datastructures"),
    }
    modules["websockets.asyncio.server"].serve = object()
    modules["websockets.exceptions"].ConnectionClosedError = type("ConnectionClosedError", (Exception,), {})
    modules["websockets.exceptions"].ConnectionClosedOK = type("ConnectionClosedOK", (Exception,), {})
    modules["websockets.http11"].Response = StubResponse
    modules["websockets.datastructures"].Headers = StubHeaders
    sys.modules.update(modules)


def install_qrcode_stubs():
    qrcode = types.ModuleType("qrcode")
    image = types.ModuleType("qrcode.image")
    svg = types.ModuleType("qrcode.image.svg")
    svg.SvgPathImage = object

    class FakeQr:
        def save(self, output):
            output.write(b"<svg>qr</svg>")

    qrcode.make = lambda *args, **kwargs: FakeQr()
    qrcode.image = image
    image.svg = svg
    sys.modules.update({"qrcode": qrcode, "qrcode.image": image, "qrcode.image.svg": svg})


class Request:
    def __init__(self, path="/", method="GET", headers=()):
        self.path = path
        self.headers = StubHeaders(headers)


class RelayHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        install_websockets_stubs()
        install_qrcode_stubs()
        cls.temp_dir = tempfile.TemporaryDirectory()
        os.environ["HERDR_LOG_DIR"] = cls.temp_dir.name
        os.environ["HERDR_CONFIG_DIR"] = cls.temp_dir.name
        os.environ["HERDR_RELAY_TOKEN"] = "test-secret"
        os.environ["HERDR_ALLOWED_ORIGINS"] = "https://app.example"
        os.environ["HERDR_VAPID_PUBLIC"] = "test-public"
        os.environ["HERDR_VAPID_PRIVATE"] = "test-private"
        sys.modules.pop("herdr_relay", None)
        cls.relay = importlib.import_module("herdr_relay")

    @classmethod
    def tearDownClass(cls):
        for handler in cls.relay.log.handlers + cls.relay.audit_log.handlers:
            handler.close()
        cls.temp_dir.cleanup()

    def call(self, request):
        return asyncio.run(self.relay.process_request(None, request))

    def test_websocket_requires_authentication(self):
        response = self.call(Request(headers=[("Upgrade", "websocket")]))
        self.assertEqual(response.status_code, 401)

    def test_websocket_rejects_untrusted_browser_origin(self):
        response = self.call(Request(
            path="/?token=test-secret",
            headers=[("Upgrade", "websocket"), ("Origin", "https://evil.example"), ("Host", "relay.example")],
        ))
        self.assertEqual(response.status_code, 403)

    def test_websocket_accepts_explicit_origin_and_bearer_token(self):
        response = self.call(Request(headers=[
            ("Upgrade", "websocket"),
            ("Origin", "https://app.example"),
            ("Host", "relay.example"),
            ("Authorization", "Bearer test-secret"),
        ]))
        self.assertIsNone(response)

    def test_static_shell_is_public_but_api_is_protected(self):
        shell = self.call(Request())
        self.assertEqual(shell.status_code, 200)
        self.assertEqual(shell.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertIn("frame-ancestors 'none'", shell.headers.get("Content-Security-Policy"))
        unauthorized = self.call(Request(path="/api/vapid-public-key"))
        self.assertEqual(unauthorized.status_code, 401)
        authorized = self.call(Request(
            path="/api/vapid-public-key",
            headers=[("Authorization", "Bearer test-secret")],
        ))
        self.assertEqual(authorized.status_code, 200)

    def test_pairing_attempts_are_rate_limited(self):
        original = self.relay.pairing_limiter
        self.relay.pairing_limiter = self.relay.SlidingWindowLimiter(2, 120)
        try:
            for _ in range(2):
                response = self.call(Request(
                    path="/api/pair/exchange?code=AAAAAAAA&name=Browser",
                    headers=[("Origin", "https://relay.example"), ("Host", "relay.example")],
                ))
                self.assertEqual(response.status_code, 400)
            limited = self.call(Request(
                path="/api/pair/exchange?code=AAAAAAAA&name=Browser",
                headers=[("Origin", "https://relay.example"), ("Host", "relay.example")],
            ))
            self.assertEqual(limited.status_code, 429)
            self.assertEqual(limited.headers.get("Retry-After"), "120")
        finally:
            self.relay.pairing_limiter = original

    def test_websocket_client_limit_is_enforced(self):
        original_clients = set(self.relay.clients)
        self.relay.clients.clear()
        self.relay.clients.update(object() for _ in range(self.relay.MAX_CLIENTS))
        try:
            response = self.call(Request(headers=[
                ("Upgrade", "websocket"),
                ("Host", "relay.example"),
                ("Authorization", "Bearer test-secret"),
            ]))
            self.assertEqual(response.status_code, 503)
        finally:
            self.relay.clients.clear()
            self.relay.clients.update(original_clients)

    def test_pairing_is_single_use_and_device_cannot_create_codes(self):
        started = self.call(Request(
            path="/api/pair/start",
            method="POST",
            headers=[
                ("Host", "relay.example"),
                ("X-Forwarded-Proto", "https"),
                ("Authorization", "Bearer test-secret"),
            ],
        ))
        self.assertEqual(started.status_code, 200)
        start_data = __import__("json").loads(started.body)
        self.assertIn("/#pair=", start_data["pairUrl"])
        self.assertNotIn("test-secret", start_data["pairUrl"])

        exchanged = self.call(Request(
            path=f"/api/pair/exchange?code={start_data['code']}&name=Android",
            method="POST",
            headers=[("Origin", "https://relay.example"), ("Host", "relay.example")],
        ))
        self.assertEqual(exchanged.status_code, 200)
        credential = __import__("json").loads(exchanged.body)

        reused = self.call(Request(
            path=f"/api/pair/exchange?code={start_data['code']}&name=Again",
            method="POST",
            headers=[("Origin", "https://relay.example"), ("Host", "relay.example")],
        ))
        self.assertEqual(reused.status_code, 400)

        denied = self.call(Request(
            path="/api/pair/start",
            method="POST",
            headers=[("Authorization", f"Bearer {credential['token']}")],
        ))
        self.assertEqual(denied.status_code, 401)

        connection = types.SimpleNamespace()
        accepted = asyncio.run(self.relay.process_request(connection, Request(headers=[
            ("Upgrade", "websocket"),
            ("Host", "relay.example"),
            ("Sec-WebSocket-Protocol", f"herdr-v1, herdr-auth.{credential['token']}"),
        ])))
        self.assertIsNone(accepted)
        self.assertTrue(connection.herdr_auth.allows("control"))

    def test_manifest_and_icons_are_served(self):
        for path in ("/manifest.webmanifest", "/icon-192.png", "/icon-512.png", "/icon-maskable-512.png"):
            with self.subTest(path=path):
                self.assertEqual(self.call(Request(path=path)).status_code, 200)

    def test_plugin_event_is_a_partial_agent_update(self):
        update = self.relay.build_agent_update({
            "type": "agent_event", "pane_id": "w1:t1:p1", "agent": "codex",
            "status": "working", "project": "remote", "cwd": "/work/remote",
        })
        self.assertEqual(update["type"], "agent_update")
        self.assertEqual(update["agent"]["pane_id"], "w1:t1:p1")
        self.assertNotIn("agents", update)

    def test_completion_notifications_match_herdr_status_transitions(self):
        for previous, current in (("working", "idle"), ("working", "done"), ("blocked", "idle")):
            with self.subTest(previous=previous, current=current):
                self.assertTrue(self.relay.is_completion_transition(previous, current))
        for previous, current in ((None, "idle"), ("idle", "idle"), ("blocked", "working")):
            with self.subTest(previous=previous, current=current):
                self.assertFalse(self.relay.is_completion_transition(previous, current))

    def test_quiet_mode_persists_without_removing_subscription(self):
        subscription = {
            "endpoint": "https://push.example/device",
            "keys": {"p256dh": "public", "auth": "secret"},
        }
        original = list(self.relay.push_subscriptions)
        self.relay.push_subscriptions[:] = [{
            "deviceId": "device-1", "subscription": subscription, "muted": False,
        }]
        try:
            with patch.object(self.relay, "_save_push_subs") as save:
                self.assertTrue(self.relay._set_push_subscription_muted(
                    subscription, True, "device-1"
                ))
            save.assert_called_once_with()
            self.assertEqual(len(self.relay.push_subscriptions), 1)
            self.assertTrue(self.relay._push_subscription_muted(
                self.relay.push_subscriptions[0]
            ))
            self.assertFalse(self.relay._set_push_subscription_muted(
                subscription, False, "another-device"
            ))
        finally:
            self.relay.push_subscriptions[:] = original

    def test_failed_poll_keeps_last_successful_agent_count(self):
        original_count = self.relay.last_agent_count
        self.relay.last_agent_count = 3
        try:
            with patch.object(self.relay, "query_agents_from_host", return_value=(False, [])):
                self.assertEqual(self.relay.get_all_agents(), [])
            self.assertFalse(self.relay.last_poll_ok)
            self.assertEqual(self.relay.last_agent_count, 3)
        finally:
            self.relay.last_agent_count = original_count

    def test_runtime_state_files_are_private(self):
        self.relay._save_push_subs()
        for path in (self.relay.LOG_FILE, self.relay.AUDIT_FILE, self.relay.PUSH_SUBS_FILE):
            with self.subTest(path=path):
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_submit_text_presses_enter_only_after_insertion_succeeds(self):
        success = types.SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch.object(self.relay.subprocess, "run", return_value=success) as run:
            self.assertTrue(self.relay.submit_text("pane-1", "hello"))
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args_list[0].args[0][-4:], ["pane", "send-text", "pane-1", "hello"])
            self.assertEqual(run.call_args_list[1].args[0][-4:], ["pane", "send-keys", "pane-1", "Enter"])

        failure = types.SimpleNamespace(returncode=1, stdout="", stderr="not logged")
        with patch.object(self.relay.subprocess, "run", return_value=failure) as run:
            self.assertFalse(self.relay.submit_text("pane-1", "keep this"))
            run.assert_called_once()

    def test_submit_text_websocket_returns_correlated_result(self):
        request_id = "request_123"

        class FakeSocket:
            remote_address = ("127.0.0.1", 12345)
            request = Request(headers=[("User-Agent", "test")])
            herdr_auth = types.SimpleNamespace(allows=lambda scope: scope == "control")

            def __init__(self):
                self.messages = [__import__("json").dumps({
                    "type": "submit_text", "pane_id": "pane-1", "text": "hello",
                    "request_id": request_id,
                })]
                self.sent = []

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not self.messages:
                    raise StopAsyncIteration
                return self.messages.pop(0)

            async def send(self, value):
                self.sent.append(__import__("json").loads(value))

        socket = FakeSocket()
        self.relay.known_panes.add("pane-1")
        try:
            with patch.object(self.relay, "submit_text", return_value=True) as submit:
                asyncio.run(self.relay.handle_client(socket))
            submit.assert_called_once_with("pane-1", "hello", remote=None)
            self.assertEqual(socket.sent, [{
                "type": "command_result", "action": "submit_text", "ok": True,
                "pane_id": "pane-1", "request_id": request_id,
            }])
        finally:
            self.relay.known_panes.discard("pane-1")

    def test_submit_text_unknown_pane_returns_correlated_failure(self):
        class FakeSocket:
            remote_address = ("127.0.0.1", 12345)
            request = Request(headers=[("User-Agent", "test")])
            herdr_auth = types.SimpleNamespace(allows=lambda scope: scope == "control")

            def __init__(self):
                self.message = __import__("json").dumps({
                    "type": "submit_text", "pane_id": "missing", "text": "hello",
                    "request_id": "missing_123",
                })
                self.sent = []

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self.message is None:
                    raise StopAsyncIteration
                message, self.message = self.message, None
                return message

            async def send(self, value):
                self.sent.append(__import__("json").loads(value))

        socket = FakeSocket()
        asyncio.run(self.relay.handle_client(socket))
        self.assertEqual(socket.sent, [{
            "type": "command_result", "action": "submit_text", "ok": False,
            "pane_id": "missing", "request_id": "missing_123",
        }])

    def test_safe_keys_return_correlated_result(self):
        class FakeSocket:
            remote_address = ("127.0.0.1", 12345)
            request = Request(headers=[("User-Agent", "Android")])
            herdr_auth = types.SimpleNamespace(allows=lambda scope: scope == "control")

            def __init__(self):
                self.messages = [__import__("json").dumps({
                    "type": "send_keys", "pane_id": "pane-1", "keys": ["Ctrl+c"],
                    "request_id": "interrupt_123",
                })]
                self.sent = []

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not self.messages:
                    raise StopAsyncIteration
                return self.messages.pop(0)

            async def send(self, value):
                self.sent.append(__import__("json").loads(value))

        socket = FakeSocket()
        self.relay.known_panes.add("pane-1")
        try:
            with patch.object(self.relay, "run_herdr_result", return_value=(True, "")) as run:
                asyncio.run(self.relay.handle_client(socket))
            run.assert_called_once_with("pane", "send-keys", "pane-1", "C-c", remote=None)
            self.assertEqual(socket.sent, [{
                "type": "command_result", "action": "send_keys", "ok": True,
                "pane_id": "pane-1", "request_id": "interrupt_123",
            }])
        finally:
            self.relay.known_panes.discard("pane-1")

    def test_unauthorized_control_is_not_executed_and_returns_failure(self):
        class FakeSocket:
            remote_address = ("127.0.0.1", 12345)
            request = Request(headers=[("User-Agent", "test")])
            herdr_auth = types.SimpleNamespace(allows=lambda scope: False)

            def __init__(self):
                self.message = __import__("json").dumps({
                    "type": "respond", "pane_id": "pane-1", "text": "yes",
                    "request_id": "approval_123",
                })
                self.sent = []

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self.message is None:
                    raise StopAsyncIteration
                message, self.message = self.message, None
                return message

            async def send(self, value):
                self.sent.append(__import__("json").loads(value))

        socket = FakeSocket()
        with patch.object(self.relay, "run_herdr_result") as run:
            asyncio.run(self.relay.handle_client(socket))
        run.assert_not_called()
        self.assertEqual(socket.sent, [{
            "type": "command_result", "action": "respond", "ok": False,
            "pane_id": "pane-1", "request_id": "approval_123",
        }])

    def test_health_reports_request_proxy_and_push_subscription_count(self):
        self.relay.push_subscriptions[:] = []
        response = self.call(Request(
            path="/api/health",
            headers=[("Host", "device.tailnet.ts.net"), ("Authorization", "Bearer test-secret")],
        ))
        data = __import__("json").loads(response.body)
        self.assertEqual(data["proxy"], "tailscale-serve")
        self.assertEqual(data["push"], "configured-no-subscriptions")
        self.assertEqual(data["pushSubscriptions"], 0)

    def test_push_test_requires_scope_and_returns_delivery_result(self):
        original = self.relay.send_web_push

        async def fake_send(*args, **kwargs):
            return {"attempted": 1, "sent": 1, "failed": 0, "removed": 0}

        self.relay.send_web_push = fake_send
        try:
            unauthorized = self.call(Request(path="/api/push/test"))
            self.assertEqual(unauthorized.status_code, 401)
            response = self.call(Request(
                path="/api/push/test",
                headers=[("Authorization", "Bearer test-secret")],
            ))
            data = __import__("json").loads(response.body)
            self.assertEqual(data["sent"], 1)
        finally:
            self.relay.send_web_push = original

    def test_revocation_disconnects_device_and_removes_owned_push(self):
        credential = self.relay.pairing.exchange(
            self.relay.pairing.start()["code"], "Revoked Android"
        )
        device_auth = self.relay.pairing.authenticate(credential["token"], "test-secret")

        class FakeClient:
            def __init__(self, auth):
                self.herdr_auth = auth
                self.close_code = None

            async def close(self, code, reason):
                self.close_code = code

        client = FakeClient(device_auth)
        self.relay.clients.add(client)
        self.relay.push_subscriptions[:] = [{
            "deviceId": credential["deviceId"],
            "subscription": {"endpoint": "https://push.example", "keys": {}},
        }]
        try:
            response = self.call(Request(
                path=f"/api/devices/revoke?id={credential['deviceId']}",
                headers=[("Authorization", "Bearer test-secret")],
            ))
            data = __import__("json").loads(response.body)
            self.assertTrue(data["revoked"])
            self.assertEqual(data["disconnected"], 1)
            self.assertEqual(data["removedPushSubscriptions"], 1)
            self.assertEqual(client.close_code, 4003)
            self.assertIsNone(self.relay.pairing.authenticate(credential["token"], "test-secret"))
        finally:
            self.relay.clients.discard(client)
            self.relay.push_subscriptions[:] = []


if __name__ == "__main__":
    unittest.main()
