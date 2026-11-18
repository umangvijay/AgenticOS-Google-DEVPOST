import base64
import os
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from backend.security.secrets_vault import SecretsVault


class SecretsVaultFormatTests(unittest.TestCase):
    def setUp(self):
        self.vault = SecretsVault()
        self.vault._master_key = "test-only-high-entropy-master-key-0123456789"
        self.vault.use_gcp = False
        self.vault._initialized = True

    def test_new_ciphertext_is_versioned_and_roundtrips(self):
        encrypted = self.vault.encrypt("credential-value")
        self.assertTrue(encrypted.startswith("v2:"))
        self.assertEqual(self.vault.decrypt(encrypted), "credential-value")

    def test_legacy_pbkdf2_ciphertext_remains_readable(self):
        salt, nonce = os.urandom(16), os.urandom(12)
        key = self.vault._derive_legacy_key(salt)
        ciphertext = AESGCM(key).encrypt(nonce, b"legacy-value", None)
        legacy = base64.urlsafe_b64encode(salt + nonce + ciphertext).decode("utf-8")
        self.assertEqual(self.vault.decrypt(legacy), "legacy-value")

    def test_authentication_tag_detects_tampering(self):
        encrypted = self.vault.encrypt("credential-value")
        raw = bytearray(base64.urlsafe_b64decode(encrypted[3:]))
        raw[-1] ^= 1
        tampered = "v2:" + base64.urlsafe_b64encode(raw).decode("utf-8")
        with self.assertRaises(ValueError):
            self.vault.decrypt(tampered)

    def test_production_requires_persistent_master_key(self):
        vault = SecretsVault()
        with patch("backend.security.secrets_vault.settings") as configured:
            configured.APP_ENV = "production"
            configured.SECRETS_MASTER_KEY = None
            with self.assertRaisesRegex(ValueError, "required in production"):
                vault.initialize()


if __name__ == "__main__":
    unittest.main()
