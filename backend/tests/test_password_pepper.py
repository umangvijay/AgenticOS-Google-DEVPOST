"""Password pepper: new hashes use HMAC+bcrypt; legacy bcrypt still verifies."""

import unittest

from backend.security import password as password_mod


class PasswordPepperTests(unittest.TestCase):
    def test_roundtrip_without_pepper(self):
        hashed = password_mod.hash_password("GoodPass1!")
        self.assertTrue(hashed.startswith("$argon2id$"))
        self.assertTrue(password_mod.verify_password("GoodPass1!", hashed))
        self.assertFalse(password_mod.verify_password("WrongPass1!", hashed))
        self.assertFalse(password_mod.password_hash_needs_upgrade(hashed))

    def test_legacy_unpeppered_hash_still_verifies(self):
        import bcrypt
        legacy = bcrypt.hashpw(b"GoodPass1!", bcrypt.gensalt(rounds=4)).decode("utf-8")
        self.assertTrue(password_mod.verify_password("GoodPass1!", legacy))
        self.assertTrue(password_mod.password_hash_needs_upgrade(legacy))

    def test_long_passphrase_uses_argon2_and_verifies(self):
        passphrase = "Long!Passphrase9-" + ("correct-horse-" * 12)
        hashed = password_mod.hash_password(passphrase)
        self.assertTrue(password_mod.verify_password(passphrase, hashed))
        self.assertFalse(password_mod.verify_password(passphrase + "wrong", hashed))


if __name__ == "__main__":
    unittest.main()
