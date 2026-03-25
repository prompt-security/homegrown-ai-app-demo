import logging
import os

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

_raw = os.getenv("ENCRYPTION_KEY", "")
try:
    if _raw and not _raw.startswith("change_me"):
        _fernet = Fernet(_raw.encode() if isinstance(_raw, str) else _raw)
    else:
        raise ValueError("placeholder or missing")
except Exception:
    _key = Fernet.generate_key()
    _fernet = Fernet(_key)
    logger.warning("ENCRYPTION_KEY not set or invalid — using ephemeral key. Set a valid key in .env for production.")


def encrypt(value: str) -> str:
    return _fernet.encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    try:
        return _fernet.decrypt(value.encode()).decode()
    except InvalidToken:
        raise ValueError("Failed to decrypt value — key mismatch or corrupted data.")
