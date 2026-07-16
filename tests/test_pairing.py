import json
import os
import stat
import tempfile
import unittest

from pairing import PairingError, PairingManager


class PairingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.now = [1000]
        self.devices_file = os.path.join(self.temp_dir.name, "config", "devices.json")
        self.manager = PairingManager(self.devices_file, ttl_seconds=120, clock=lambda: self.now[0])

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_code_is_single_use_and_device_token_is_hashed(self):
        pairing = self.manager.start()
        self.assertEqual(len(pairing["code"]), 8)
        credential = self.manager.exchange(pairing["code"], "Android phone")
        self.assertTrue(self.manager.authenticate(credential["token"], "admin-secret").allows("control"))
        self.assertFalse(self.manager.authenticate(credential["token"], "admin-secret").allows("pair"))
        with self.assertRaises(PairingError):
            self.manager.exchange(pairing["code"], "Second phone")

        with open(self.devices_file, encoding="utf-8") as handle:
            persisted = handle.read()
        self.assertNotIn(credential["token"], persisted)
        self.assertIn("Android phone", persisted)
        self.assertEqual(stat.S_IMODE(os.stat(self.devices_file).st_mode), 0o600)

    def test_expired_code_is_rejected(self):
        pairing = self.manager.start()
        self.now[0] += 121
        with self.assertRaises(PairingError):
            self.manager.exchange(pairing["code"], "Late phone")

    def test_admin_and_revocation(self):
        admin = self.manager.authenticate("admin-secret", "admin-secret")
        self.assertTrue(admin.allows("pair"))
        credential = self.manager.exchange(self.manager.start()["code"], "Windows")
        self.assertTrue(self.manager.revoke(credential["deviceId"]))
        self.assertIsNone(self.manager.authenticate(credential["token"], "admin-secret"))
        self.assertFalse(self.manager.revoke(credential["deviceId"]))

    def test_pending_code_and_device_limits(self):
        limited = PairingManager(
            self.devices_file, ttl_seconds=120, clock=lambda: self.now[0],
            max_pending=1, max_devices=1,
        )
        first = limited.start()
        with self.assertRaisesRegex(PairingError, "too many active pairing codes"):
            limited.start()
        limited.exchange(first["code"], "First")
        second = limited.start()
        with self.assertRaisesRegex(PairingError, "device limit"):
            limited.exchange(second["code"], "Second")


if __name__ == "__main__":
    unittest.main()
