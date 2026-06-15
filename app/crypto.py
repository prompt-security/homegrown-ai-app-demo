import json
import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

_CRYPTO_OVERRIDE_FILE = Path(os.getenv(
    "CRYPTO_OVERRIDE_FILE",
    str(Path(__file__).parent / "data" / "crypto_config_override.json"),
))


def _load_key_from_override() -> str:
    try:
        if _CRYPTO_OVERRIDE_FILE.exists():
            data = json.loads(_CRYPTO_OVERRIDE_FILE.read_text())
            key = data.get("encryption_key", "")
            if key:
                logger.info("Encryption key loaded from override file: %s", _CRYPTO_OVERRIDE_FILE)
                return key
    except Exception as exc:
        logger.warning("Failed to read crypto override file: %s", exc)
    return ""


_raw = _load_key_from_override() or os.getenv("ENCRYPTION_KEY", "")
try:
    if _raw and not _raw.startswith("change_me"):
        _fernet = Fernet(_raw.encode() if isinstance(_raw, str) else _raw)
    else:
        raise ValueError("placeholder or missing")
except Exception:
    _key = Fernet.generate_key()
    _fernet = Fernet(_key)
    logger.warning("ENCRYPTION_KEY not set or invalid — using ephemeral key. Set ENCRYPTION_KEY in docker-compose.yml (or as an environment variable) for production.")


def encrypt(value: str) -> str:
    return _fernet.encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    try:
        return _fernet.decrypt(value.encode()).decode()
    except InvalidToken:
        raise ValueError("Failed to decrypt value — key mismatch or corrupted data.")


def validate_fernet_key(key: str) -> bool:
    """Return True if key is a valid Fernet key (URL-safe base64, decodes to 32 bytes)."""
    try:
        Fernet(key.encode() if isinstance(key, str) else key)
        return True
    except Exception:
        return False


def set_encryption_key(key: str) -> None:
    """Hot-swap the Fernet encryption key in-process. Previously encrypted values will no longer be readable."""
    global _fernet
    _fernet = Fernet(key.encode() if isinstance(key, str) else key)


def write_encryption_key_override(key: str) -> None:
    """Persist the encryption key to the override file so restarts use the new key."""
    _CRYPTO_OVERRIDE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _CRYPTO_OVERRIDE_FILE.write_text(json.dumps({"encryption_key": key}))


def clear_encryption_key_override() -> None:
    """Delete the override file and reset _fernet to the env-var key (or a new ephemeral key)."""
    global _fernet
    try:
        if _CRYPTO_OVERRIDE_FILE.exists():
            _CRYPTO_OVERRIDE_FILE.unlink()
    except Exception as exc:
        logger.warning("Failed to remove crypto override file: %s", exc)
    raw = os.getenv("ENCRYPTION_KEY", "")
    try:
        if raw and not raw.startswith("change_me"):
            _fernet = Fernet(raw.encode() if isinstance(raw, str) else raw)
        else:
            raise ValueError("placeholder or missing")
    except Exception:
        _key = Fernet.generate_key()
        _fernet = Fernet(_key)
        logger.warning("ENCRYPTION_KEY not set after override clear — using new ephemeral key.")


def encryption_key_overridden() -> bool:
    """True if an override file exists with a valid key."""
    try:
        if _CRYPTO_OVERRIDE_FILE.exists():
            data = json.loads(_CRYPTO_OVERRIDE_FILE.read_text())
            return bool(data.get("encryption_key"))
    except Exception:
        pass
    return False
