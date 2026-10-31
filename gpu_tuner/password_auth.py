"""Password hashing for container-based GPU Tuner authentication."""

import hashlib
import hmac
import os

SALT_BYTES = 16
KEY_BYTES = 32
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
PREFIX = "scrypt"


def validate_password(password):
    if not isinstance(password, str):
        raise ValueError("Password must be text")

    if not 12 <= len(password) <= 1024:
        raise ValueError("Password must contain 12 to 1024 characters")

    if "\x00" in password:
        raise ValueError("Password must not contain NUL characters")

    return password


def hash_password(password):
    """Generate a salted password hash; never persist plaintext."""
    validate_password(password)

    salt = os.urandom(SALT_BYTES)
    key = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=KEY_BYTES,
    )

    return f"{PREFIX}${salt.hex()}${key.hex()}"



def valid_password_hash(stored):
    """Validate the complete stored hash format without hashing a password."""
    if not isinstance(stored, str):
        return False

    parts = stored.split("$")
    if len(parts) != 3 or parts[0] != PREFIX:
        return False

    salt_hex, key_hex = parts[1:]
    if len(salt_hex) != SALT_BYTES * 2 or len(key_hex) != KEY_BYTES * 2:
        return False

    try:
        bytes.fromhex(salt_hex)
        bytes.fromhex(key_hex)
    except ValueError:
        return False

    return True

def verify_password(password, stored):
    """Verify a password against a stored hash."""
    if not isinstance(password, str) or not isinstance(stored, str):
        return False

    try:
        prefix, salt_hex, key_hex = stored.split("$")
        if prefix != PREFIX:
            return False

        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(key_hex)

        if len(salt) != SALT_BYTES or len(expected) != KEY_BYTES:
            return False

        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=SCRYPT_N,
            r=SCRYPT_R,
            p=SCRYPT_P,
            dklen=KEY_BYTES,
        )
        return hmac.compare_digest(actual, expected)

    except (ValueError, TypeError, UnicodeError):
        return False
