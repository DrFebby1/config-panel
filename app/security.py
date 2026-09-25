"""Password hashing and session token handling.

Uses only the standard library: scrypt for password hashes and
secrets/hmac for tokens. That keeps `requirements.txt` free of crypto packages
that would need compiling.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from . import config

# scrypt work factors. 2**14 * 8 * 128 bytes = 16 MiB, comfortably inside the
# OpenSSL default 32 MiB maxmem limit.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32


def hash_password(password: str) -> str:
    """Return a self-describing `scrypt$<salt_hex>$<hash_hex>` string."""
    if not password:
        raise ValueError("password must not be empty")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verification of a password against a stored hash."""
    if not password or not stored:
        return False
    try:
        algo, salt_hex, hash_hex = stored.split("$")
    except ValueError:
        return False
    if algo != "scrypt":
        return False
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except ValueError:
        return False
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=len(expected) or _SCRYPT_DKLEN,
    )
    return hmac.compare_digest(digest, expected)


def new_token(length: int = 32) -> str:
    """URL-safe random token used for subscriptions and API keys."""
    return secrets.token_urlsafe(length)


def token_fingerprint(token: str) -> str:
    """Store session tokens hashed so a database leak cannot be replayed."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def session_expiry(now: int | None = None) -> int:
    return int(now or time.time()) + config.SESSION_TTL


def safe_equals(left: str, right: str) -> bool:
    """Timing-safe comparison that tolerates None/empty input."""
    if not left or not right:
        return False
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
