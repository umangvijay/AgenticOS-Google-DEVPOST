"""Password hashing and account lockout helpers.

New passwords use Argon2id. Existing bcrypt hashes remain verifiable and are
upgraded after a successful login. Passwords are hashed, never encrypted.
"""

import hashlib
import hmac
import re
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple

import bcrypt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

logger = logging.getLogger(__name__)

# ── Policy constants ──────────────────────────────────────────────
MIN_LENGTH = 8
MAX_LENGTH = 256
LEGACY_BCRYPT_MAX_LENGTH = 72
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_DURATION_MINUTES = 15

# Top 100 most common passwords (subset for fast rejection)
COMMON_PASSWORDS = frozenset({
    "password", "123456", "12345678", "qwerty", "abc123", "monkey",
    "1234567", "letmein", "trustno1", "dragon", "baseball", "iloveyou",
    "master", "sunshine", "ashley", "bailey", "passw0rd", "shadow",
    "123456789", "1234567890", "password1", "password123", "admin",
    "welcome", "football", "charlie", "donald", "login", "princess",
    "starwars", "solo", "qwerty123", "welcome1", "hello", "charlie1",
})

# OWASP minimum profile; benchmark and tune against production login latency
# and concurrent memory limits before raising these costs further.
_ARGON2 = PasswordHasher(time_cost=2, memory_cost=19_456, parallelism=1,
                          hash_len=32, salt_len=16)


def _material(password: str) -> bytes:
    """Legacy bcrypt material, with HMAC-SHA256 when a pepper is configured."""
    from backend.config.settings import settings
    pepper = (settings.PASSWORD_PEPPER or "").encode("utf-8")
    raw = password.encode("utf-8")
    if not pepper:
        return raw[:LEGACY_BCRYPT_MAX_LENGTH]
    return hmac.new(pepper, raw, hashlib.sha256).hexdigest().encode("utf-8")


def hash_password(password: str) -> str:
    """Hash a password using memory-hard Argon2id and optional HMAC pepper."""
    from backend.config.settings import settings
    pepper = (settings.PASSWORD_PEPPER or "").encode("utf-8")
    material = hmac.new(pepper, password.encode("utf-8"), hashlib.sha256).hexdigest() if pepper else password
    return _ARGON2.hash(material)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify Argon2id and legacy bcrypt hashes during the migration window."""
    if not hashed_password:
        return False
    if hashed_password.startswith("$argon2id$"):
        from backend.config.settings import settings
        pepper = (settings.PASSWORD_PEPPER or "").encode("utf-8")
        material = hmac.new(pepper, plain_password.encode("utf-8"), hashlib.sha256).hexdigest() if pepper else plain_password
        try:
            return _ARGON2.verify(hashed_password, material)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    # Compatibility for existing bcrypt rows. bcrypt truncates inputs to 72
    # bytes, so preserve that exact legacy behavior only for these old hashes.
    stored = hashed_password.encode("utf-8")
    try:
        if bcrypt.checkpw(_material(plain_password), stored):
            return True
    except ValueError:
        return False
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8")[:LEGACY_BCRYPT_MAX_LENGTH], stored)
    except ValueError:
        return False


def password_hash_needs_upgrade(hashed_password: str) -> bool:
    """Return true for legacy bcrypt hashes or outdated Argon2 parameters."""
    if not hashed_password or not hashed_password.startswith("$argon2id$"):
        return True
    try:
        return _ARGON2.check_needs_rehash(hashed_password)
    except (VerificationError, InvalidHashError):
        return True


def validate_password_strength(password: str) -> Tuple[bool, Optional[str]]:
    """
    Validate password meets policy requirements.
    Returns (is_valid, error_message).
    """
    if len(password) < MIN_LENGTH:
        return False, f"Password must be at least {MIN_LENGTH} characters"

    if len(password) > MAX_LENGTH:
        return False, f"Password must be at most {MAX_LENGTH} characters"

    if password.lower() in COMMON_PASSWORDS:
        return False, "This password is too common. Choose a stronger one."

    if not re.search(r"[A-Z]", password):
        return False, "Password must contain at least one uppercase letter"

    if not re.search(r"[a-z]", password):
        return False, "Password must contain at least one lowercase letter"

    if not re.search(r"\d", password):
        return False, "Password must contain at least one digit"

    if not re.search(r"[!@#$%^&*()_+\-=\[\]{};':\"\\|,.<>\/?]", password):
        return False, "Password must contain at least one special character"

    return True, None


def check_lockout(failed_attempts: int, locked_until: Optional[str]) -> Tuple[bool, Optional[str]]:
    """
    Check if an account is locked out.
    Returns (is_locked, reason_message).
    """
    if locked_until:
        lock_time = datetime.fromisoformat(locked_until)
        now = datetime.now(timezone.utc)
        if now < lock_time:
            remaining = (lock_time - now).seconds // 60
            return True, f"Account locked. Try again in {remaining + 1} minute(s)."

    return False, None


def should_lock_account(failed_attempts: int) -> bool:
    """Check if the account should be locked after this failed attempt."""
    return failed_attempts >= MAX_FAILED_ATTEMPTS


def get_lockout_until() -> datetime:
    """Get the lockout expiry timestamp."""
    return datetime.now(timezone.utc) + timedelta(minutes=LOCKOUT_DURATION_MINUTES)
