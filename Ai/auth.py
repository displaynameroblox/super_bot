"""Password hashing and token primitives for local accounts."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets

try:  # Argon2id is preferred; scrypt (standard library) is the fallback.
    from argon2 import PasswordHasher

    _ARGON2 = PasswordHasher()
except ImportError:  # pragma: no cover - exercised only without argon2-cffi
    _ARGON2 = None

USERNAME_PATTERN = re.compile(r"[A-Za-z0-9_.-]{3,32}")
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 256
TOKEN_TTL_SECONDS = 7 * 24 * 3600
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**15, 8, 1
SCRYPT_MAXMEM = 128 * 1024 * 1024


class InvalidCredentials(ValueError):
    """Raised when a username or password does not meet the rules."""


def validate_username(username: str) -> str:
    if not isinstance(username, str) or not USERNAME_PATTERN.fullmatch(username):
        raise InvalidCredentials(
            "Usernames must be 3-32 characters: letters, digits, '.', '_' or '-'."
        )
    return username


def validate_password(password: str) -> str:
    if not isinstance(password, str) or not (
        MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH
    ):
        raise InvalidCredentials(
            f"Passwords must be {MIN_PASSWORD_LENGTH}-{MAX_PASSWORD_LENGTH} characters."
        )
    return password


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _scrypt(secret: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        secret.encode("utf-8"), salt=salt, n=n, r=r, p=p, maxmem=SCRYPT_MAXMEM, dklen=32
    )


def hash_password(password: str, *, prefer_argon2: bool = True) -> str:
    """Return a self-describing hash string; the plaintext is never stored."""
    if prefer_argon2 and _ARGON2 is not None:
        return _ARGON2.hash(password)
    salt = secrets.token_bytes(16)
    digest = _scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        if stored.startswith("$argon2"):
            if _ARGON2 is None:
                return False
            return _ARGON2.verify(stored, password)
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = _scrypt(password, base64.b64decode(salt), int(n), int(r), int(p))
        return hmac.compare_digest(actual, expected)
    except Exception:  # malformed hash, mismatch, or unsupported parameters
        return False


def needs_rehash(stored: str) -> bool:
    if _ARGON2 is not None:
        return not stored.startswith("$argon2") or _ARGON2.check_needs_rehash(stored)
    return not stored.startswith(f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}$")


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_digest(token: str) -> bytes:
    """Only this digest is stored, so a database leak does not reveal usable tokens."""
    return hashlib.sha256(token.encode("utf-8")).digest()


def new_recovery_code() -> str:
    raw = secrets.token_hex(10).upper()
    return "-".join(raw[i : i + 5] for i in range(0, len(raw), 5))


def normalize_recovery_code(code: str) -> str:
    return re.sub(r"[^0-9A-Fa-f]", "", code).upper()
