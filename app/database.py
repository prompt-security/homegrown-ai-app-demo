import json
import logging
import os
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

logger = logging.getLogger(__name__)

# Override file: persists a new DATABASE_URL across restarts.
# In Docker: mounted via app_data volume at /app/data/
# In local dev: app/data/ relative to this file's directory
_DB_OVERRIDE_FILE = Path(os.getenv(
    "DB_OVERRIDE_FILE",
    str(Path(__file__).parent / "data" / "db_config_override.json"),
))


def _load_override_url() -> str | None:
    try:
        if _DB_OVERRIDE_FILE.exists():
            data = json.loads(_DB_OVERRIDE_FILE.read_text())
            url = data.get("database_url")
            if url:
                logger.info("Using DATABASE_URL from override file: %s", _DB_OVERRIDE_FILE)
                return url
    except Exception as exc:
        logger.warning("Failed to read DB override file: %s", exc)
    return None


DATABASE_URL: str = _load_override_url() or os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://hgapp:hgapp_dev@db:5432/hgapp",
)

engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


def write_db_override(new_url: str) -> None:
    """Persist a new DATABASE_URL to the override file so restarts use the new password."""
    _DB_OVERRIDE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _DB_OVERRIDE_FILE.write_text(json.dumps({"database_url": new_url}))


async def rebuild_engine(new_url: str) -> None:
    """Hot-swap the SQLAlchemy engine in-process to use new_url. Safe to call while requests are in flight."""
    global DATABASE_URL, engine, AsyncSessionLocal
    old_engine = engine
    DATABASE_URL = new_url
    engine = create_async_engine(new_url, echo=False, pool_pre_ping=True)
    AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await old_engine.dispose()
    logger.info("Database engine hot-swapped to new credentials")
