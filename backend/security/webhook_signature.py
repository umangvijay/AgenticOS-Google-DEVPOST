"""Versioned, timestamp-bound webhook HMAC verification."""
import hashlib
import hmac
import time


def verify_signature(secret: str, timestamp: str, signature: str, body: bytes, now=None) -> bool:
    try:
        if abs((time.time() if now is None else now) - int(timestamp)) > 300:
            return False
    except (ValueError, TypeError):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
