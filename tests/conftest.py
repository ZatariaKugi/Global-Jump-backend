"""Test fixtures: an in-memory SQLite DB and an async HTTP client.

The app's ``get_session`` dependency is overridden to use a shared in-memory SQLite
engine, so the suite runs without a PostgreSQL instance while still exercising the real
ORM models, auth flow, and routers.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from unittest import mock

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

# Ensure settings are valid before any app import.
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("ENVIRONMENT", "local")
# 32 zero-bytes base64url-encoded — valid AES-256 key for tests only.
os.environ.setdefault("ENCRYPTION_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
os.environ.setdefault("UPLOAD_DIR", "/tmp/globlejump_test_uploads")
# Force the no-SMTP dev fallback (log-only) so the suite never sends real email via
# whatever SMTP_HOST happens to be configured in the local .env.
os.environ["SMTP_HOST"] = ""
# Force the local-disk file storage fallback so the suite never uploads to whatever
# real S3 bucket happens to be configured in the local .env — file_storage._s3_enabled()
# requires all three of these to be set, so clearing any one disables S3 entirely.
os.environ["S3_BUCKET_NAME"] = ""
os.environ["AWS_ACCESS_KEY_ID"] = ""
os.environ["AWS_SECRET_ACCESS_KEY"] = ""
# Force the no-OpenAI fallback so the suite can never hit the real OpenAI API via
# whatever OPENAI_API_KEY happens to be configured in the local .env.
os.environ["OPENAI_API_KEY"] = ""

import app.models  # noqa: E402,F401  (register models on Base.metadata)
from app.api.deps import get_settings  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.session import get_session  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models.user import User, UserRole  # noqa: E402


@pytest.fixture
async def engine() -> AsyncIterator:
    eng = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture(autouse=True)
def _block_real_openai() -> Iterator[None]:
    """Suite-wide guard: constructing a real OpenAI client in a test is always a bug.

    Tests that need a client mock re-patch ``ai_insight_service.AsyncOpenAI``
    themselves (see test_ai_insights.py); that inner patch takes precedence.
    If a test sets an API key but forgets to patch, construction raises and
    generate_insights degrades to None — the SDK can never dial api.openai.com.
    """
    with mock.patch(
        "app.services.ai_insight_service.AsyncOpenAI",
        side_effect=AssertionError(
            "Test tried to construct a real AsyncOpenAI client — patch it with a mock."
        ),
    ):
        yield


@pytest.fixture
async def client(engine) -> AsyncIterator[AsyncClient]:
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _override_get_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app = create_app(get_settings())
    app.dependency_overrides[get_session] = _override_get_session

    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


@pytest.fixture
async def admin_token(client: AsyncClient, engine) -> str:
    """Create an admin user directly (bypassing the public API) and return its JWT."""
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        user = User(
            email="admin@test.com",
            hashed_password=hash_password("adminpass1"),
            role=UserRole.admin,
        )
        session.add(user)
        await session.commit()

    resp = await client.post(
        "/api/v1/auth/login",
        data={"username": "admin@test.com", "password": "adminpass1"},
    )
    assert resp.status_code == 200, resp.text
    return str(resp.json()["access_token"])


@pytest.fixture
async def advisor_token(client: AsyncClient, engine) -> str:
    """Create an approved advisor directly and return its JWT."""
    from app.models.user import VerificationStatus

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        user = User(
            email="advisor@test.com",
            full_name="Test Advisor",
            hashed_password=hash_password("advisorpass1"),
            role=UserRole.advisor,
            is_active=True,
            verification_status=VerificationStatus.approved,
        )
        session.add(user)
        await session.commit()

    resp = await client.post(
        "/api/v1/auth/login",
        data={"username": "advisor@test.com", "password": "advisorpass1"},
    )
    assert resp.status_code == 200, resp.text
    return str(resp.json()["access_token"])
