import unittest

from relay_security import (
    SlidingWindowLimiter,
    ValidationError,
    is_loopback_host,
    origin_is_allowed,
    request_token,
    require_secure_bind,
    tokens_match,
    validate_message,
)


class Headers(dict):
    pass


class RelaySecurityTests(unittest.TestCase):
    def test_sliding_window_rate_limiter(self):
        now = [100.0]
        limiter = SlidingWindowLimiter(2, 10, clock=lambda: now[0])
        self.assertTrue(limiter.allow("device"))
        self.assertTrue(limiter.allow("device"))
        self.assertFalse(limiter.allow("device"))
        self.assertTrue(limiter.allow("other-device"))
        now[0] += 11
        self.assertTrue(limiter.allow("device"))

    def test_rate_limiter_bounds_distinct_client_keys(self):
        limiter = SlidingWindowLimiter(1, 60, max_keys=2)
        self.assertTrue(limiter.allow("first"))
        self.assertTrue(limiter.allow("second"))
        self.assertTrue(limiter.allow("third"))
        self.assertLessEqual(len(limiter._events), 2)

    def test_loopback_detection(self):
        self.assertTrue(is_loopback_host("127.0.0.1"))
        self.assertTrue(is_loopback_host("::1"))
        self.assertFalse(is_loopback_host("0.0.0.0"))
        self.assertFalse(is_loopback_host("192.168.1.2"))

    def test_nonlocal_bind_requires_token(self):
        with self.assertRaises(RuntimeError):
            require_secure_bind("0.0.0.0", "")
        require_secure_bind("0.0.0.0", "secret")
        require_secure_bind("127.0.0.1", "")

    def test_tokens_and_legacy_query_fallback(self):
        self.assertEqual(request_token(Headers(Authorization="Bearer secret"), "/"), "secret")
        self.assertEqual(
            request_token(Headers({"Sec-WebSocket-Protocol": "herdr-v1, herdr-auth.device-token"}), "/"),
            "device-token",
        )
        self.assertEqual(request_token(Headers(), "/?token=legacy"), "legacy")
        self.assertTrue(tokens_match("secret", "secret"))
        self.assertFalse(tokens_match("wrong", "secret"))

    def test_origin_policy(self):
        self.assertTrue(origin_is_allowed("", "relay.example", set()))
        self.assertTrue(origin_is_allowed("https://relay.example", "relay.example", set()))
        self.assertTrue(origin_is_allowed("https://app.example", "relay.example", {"https://app.example"}))
        self.assertFalse(origin_is_allowed("https://evil.example", "relay.example", set()))

    def test_command_and_line_validation(self):
        message = validate_message({"type": "send_keys", "pane_id": "pane-1", "keys": ["Ctrl+c"]})
        self.assertEqual(message["keys"], ["C-c"])
        with self.assertRaises(ValidationError):
            validate_message({"type": "send_keys", "pane_id": "pane-1", "keys": ["F12"]})
        with self.assertRaises(ValidationError):
            validate_message({"type": "read_pane", "pane_id": "pane-1", "lines": 5001})
        with self.assertRaises(ValidationError):
            validate_message({"type": "send_text", "pane_id": "pane-1", "text": "bad\x00text"})
        submitted = validate_message({
            "type": "submit_text", "pane_id": "pane-1", "text": "hello",
            "request_id": "request_123",
        })
        self.assertEqual(submitted["request_id"], "request_123")
        with self.assertRaises(ValidationError):
            validate_message({
                "type": "submit_text", "pane_id": "pane-1", "text": "hello",
                "request_id": "bad request id",
            })

    def test_response_allowlist(self):
        validate_message({"type": "respond", "pane_id": "pane-1", "text": "yes"})
        with self.assertRaises(ValidationError):
            validate_message({"type": "respond", "pane_id": "pane-1", "text": "run anything"})

    def test_push_subscription_validation(self):
        valid = {
            "type": "push_subscribe",
            "subscription": {
                "endpoint": "https://push.example/subscription",
                "keys": {"p256dh": "public", "auth": "auth"},
            },
        }
        validate_message(valid)
        valid["subscription"]["endpoint"] = "http://push.example"
        with self.assertRaises(ValidationError):
            validate_message(valid)


if __name__ == "__main__":
    unittest.main()
