"""Tests for container WebUI password hashing and validation."""

import unittest

from gpu_tuner.password_auth import (
    hash_password,
    valid_password_hash,
    validate_password,
    verify_password,
)


class PasswordAuthTests(unittest.TestCase):

    def test_hash_and_verify(self):
        password = "Example-Strong-Password-123"
        stored = hash_password(password)

        self.assertTrue(valid_password_hash(stored))
        self.assertTrue(verify_password(password, stored))
        self.assertFalse(verify_password("Wrong-Password-123", stored))
        self.assertNotIn(password, stored)

    def test_random_salts(self):
        password = "Example-Strong-Password-123"
        self.assertNotEqual(
            hash_password(password),
            hash_password(password),
        )

    def test_password_requirements(self):
        for password in ("short", "", "Contains\x00NUL"):
            with self.subTest(password=repr(password)):
                with self.assertRaises(ValueError):
                    validate_password(password)

        self.assertEqual(
            validate_password("Valid-Password-123"),
            "Valid-Password-123",
        )

    def test_malformed_hashes(self):
        invalid = (
            "",
            "scrypt$",
            "scrypt$abc$def",
            "sha256$" + "00" * 16 + "$" + "00" * 32,
            "scrypt$" + "zz" * 16 + "$" + "00" * 32,
            "scrypt$" + "00" * 16 + "$" + "00" * 31,
            "scrypt$" + "00" * 16 + "$" + "00" * 32 + "$extra",
        )

        for stored in invalid:
            with self.subTest(stored=stored):
                self.assertFalse(valid_password_hash(stored))
                self.assertFalse(verify_password("Valid-Password-123", stored))


if __name__ == "__main__":
    unittest.main()
