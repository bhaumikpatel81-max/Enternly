"""
Symmetric encryption for OAuth tokens stored at rest (currently: Google
Calendar access/refresh tokens in google_calendar_connection, which were
previously stored in plaintext -- a DB dump/leak gave durable calendar
access for every connected tenant).

Uses Fernet (AES-128-CBC + HMAC), keyed from TOKEN_ENCRYPTION_KEY. Generate
one with:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Without TOKEN_ENCRYPTION_KEY set, encrypt()/decrypt() are no-ops (plaintext
passthrough) so local dev keeps working unmodified -- set it in every real
deployment. decrypt() falls back to returning the raw value on failure
(covers rows written before the key was enabled, or a wrong/rotated key)
rather than breaking the calendar connection outright.
"""
import os
from functools import lru_cache
from typing import Optional


@lru_cache(maxsize=1)
def _fernet():
    key = os.environ.get("TOKEN_ENCRYPTION_KEY", "").strip()
    if not key:
        return None
    from cryptography.fernet import Fernet
    return Fernet(key.encode())


def encrypt(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    f = _fernet()
    if not f:
        return value
    return f.encrypt(value.encode()).decode()


def decrypt(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    f = _fernet()
    if not f:
        return value
    try:
        return f.decrypt(value.encode()).decode()
    except Exception:
        return value
