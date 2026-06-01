"""Shared test fixtures for the humble-demo test suite."""

import os
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch

# Point at an in-memory SQLite DB before any app modules are imported
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["SECRET_KEY"] = "test-secret-key-for-unit-tests"
os.environ["ENCRYPTION_KEY"] = ""  # not needed for most tests
os.environ["ADMIN_EMAIL"] = "admin@test.com"
os.environ["ADMIN_PASSWORD"] = "testpass"
os.environ["LITELLM_BASE_URL"] = "http://litellm:4000"
os.environ["LITELLM_MASTER_KEY"] = ""

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import Base, get_db
from auth import create_access_token, hash_password
from models import User, PSTenant


# ── Async DB engine for tests (SQLite in-memory) ────────────────────────────
test_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSession = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def db():
    """Create tables, yield a session, then drop."""
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with TestSession() as session:
        yield session
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def test_user(db: AsyncSession):
    """Create a basic test user and return it."""
    user = User(
        email="user@test.com",
        hashed_password=hash_password("password"),
        role="se",
        is_active=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@pytest_asyncio.fixture
async def test_tenant(db: AsyncSession):
    """Create a PS tenant for testing."""
    tenant = PSTenant(
        name="TestTenant",
        base_url="https://test.prompt.security",
        gateway_url="https://test.prompt.security/v1",
    )
    db.add(tenant)
    await db.commit()
    await db.refresh(tenant)
    return tenant


@pytest_asyncio.fixture
def auth_token(test_user: User):
    """JWT token for the test user."""
    return create_access_token({"sub": str(test_user.id)})


@pytest_asyncio.fixture
async def client(db: AsyncSession):
    """FastAPI test client with DB override."""
    from main import app

    async def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()
