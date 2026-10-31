import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from stockjournal.secrets import SecretStore, SecretStoreError


class SecretStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "private" / "credentials.bin"

    def test_non_windows_credentials_stay_in_memory_without_creating_files(self):
        with patch("stockjournal.secrets.sys", SimpleNamespace(platform="linux")):
            store = SecretStore(self.path)
            self.assertFalse(store.supported)
            self.assertEqual(store.get("missing"), "")
            self.assertEqual(store.get("missing", "fallback"), "fallback")
            store.set("app_secret", "never-on-disk")
            self.assertEqual(store.get("app_secret"), "never-on-disk")
            self.assertEqual(SecretStore(self.path).get("app_secret"), "")
        self.assertFalse(self.path.parent.exists())

    def protected(self):
        transform = lambda raw: bytes(value ^ 0xA5 for value in raw)
        for patcher in (patch("stockjournal.secrets.sys", SimpleNamespace(platform="win32")),
                        patch("stockjournal.secrets._dpapi_encrypt", side_effect=transform),
                        patch("stockjournal.secrets._dpapi_decrypt", side_effect=transform)):
            patcher.start()
            self.addCleanup(patcher.stop)
        return SecretStore(self.path)

    def test_protected_file_reloads_without_plaintext_keys_or_values(self):
        store = self.protected()
        self.assertTrue(store.supported)
        store.set("app_secret", "private-value-한글")
        store.set("account", "12345678901")
        raw = self.path.read_bytes()
        for private in (b"app_secret", b"private-value", b"12345678901"):
            self.assertNotIn(private, raw)
        reloaded = SecretStore(self.path)
        self.assertEqual(reloaded.get("app_secret"), "private-value-한글")
        self.assertEqual(reloaded.get("account"), "12345678901")
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_encryption_failure_preserves_existing_file_and_memory(self):
        store = self.protected()
        store.set("secret", "old")
        original = self.path.read_bytes()
        with patch("stockjournal.secrets._dpapi_encrypt", side_effect=OSError("sensitive diagnostic")):
            with self.assertRaises(SecretStoreError) as caught:
                store.set("secret", "new")
        self.assertNotIn("sensitive", str(caught.exception))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(store.get("secret"), "old")

    def test_atomic_write_failure_does_not_replace_file_or_leave_plaintext(self):
        store = self.protected()
        store.set("secret", "old")
        original = self.path.read_bytes()
        with patch("stockjournal.secrets.os.replace", side_effect=PermissionError("private pathname")):
            with self.assertRaises(SecretStoreError):
                store.set("secret", "new")
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(store.get("secret"), "old")
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_corrupted_or_plaintext_store_is_never_accepted_as_a_fallback(self):
        self.protected()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for raw in (b'{"app_secret":"plaintext"}', b"broken binary"):
            self.path.write_bytes(raw)
            with self.subTest(), self.assertRaises(SecretStoreError):
                SecretStore(self.path)

    @unittest.skipUnless(sys.platform == "win32", "Windows DPAPI integration requires Windows")
    def test_real_windows_dpapi_roundtrip_and_tamper_rejection(self):
        store = SecretStore(self.path)
        store.set("app_secret", "actual-dpapi-test-value")
        raw = self.path.read_bytes()
        self.assertNotIn(b"actual-dpapi-test-value", raw)
        self.assertEqual(SecretStore(self.path).get("app_secret"), "actual-dpapi-test-value")
        damaged = bytearray(raw)
        damaged[-8] ^= 0x80
        self.path.write_bytes(damaged)
        with self.assertRaises(SecretStoreError):
            SecretStore(self.path)


if __name__ == "__main__":
    unittest.main()
