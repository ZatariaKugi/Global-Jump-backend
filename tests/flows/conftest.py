"""The flow-test world: the real app, in memory, with the outside world faked.

Every test gets a fresh SQLite database, the app booted through its lifespan, an HTTP
client, three logged-in users (admin, seeker, advisor), Stripe keys saved the way an admin
saves them, and the fakes for Stripe, Zoom, the AI model, push and email already installed.

Run only these:  make test-flows   (or  uv run pytest tests/flows -q)
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

# --- environment, before any app import -------------------------------------------------
os.environ.setdefault("JWT_SECRET", "flows-test-secret-flows-test-secret")
os.environ.setdefault("ENVIRONMENT", "local")
os.environ.setdefault("ENCRYPTION_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
os.environ["UPLOAD_DIR"] = str(Path(os.environ.get("TEMP", "/tmp")) / "globlejump_flows")
os.environ["SMTP_HOST"] = ""
os.environ["S3_BUCKET_NAME"] = ""
os.environ["AWS_ACCESS_KEY_ID"] = ""
os.environ["AWS_SECRET_ACCESS_KEY"] = ""
os.environ["OPENAI_API_KEY"] = "sk-flows-fake"
os.environ["FRONTEND_URL"] = "http://frontend.test"
os.environ["ZOOM_CLIENT_ID"] = "zoom-client-flows"
os.environ["ZOOM_CLIENT_SECRET"] = "zoom-secret-flows"
os.environ["ZOOM_REDIRECT_URI"] = "http://test/api/v1/integrations/zoom/callback"
os.environ["FIREBASE_CREDENTIALS_FILE"] = ""

# The legacy tests/conftest.py (a parent directory, imported first) may already have built
# the cached settings with its own environment; rebuild them from the values above.
from app.core.config import get_settings as _get_settings  # noqa: E402

_get_settings.cache_clear()

from tests.flows.fakes.ai_fake import FakeAI  # noqa: E402
from tests.flows.fakes.push_fake import FakePush  # noqa: E402
from tests.flows.fakes.stripe_fake import (  # noqa: E402
    PUBLISHABLE_KEY,
    SECRET_KEY,
    WEBHOOK_SECRET,
    FakeStripe,
)
from tests.flows.fakes.zoom_fake import FakeZoom  # noqa: E402

PASSWORD = "Passw0rd!Strong"


@dataclass
class Mail:
    kind: str
    email: str
    token: str


@dataclass
class World:
    client: Any
    Session: Any
    settings: Any
    admin: dict[str, str]
    seeker: dict[str, str]
    advisor: dict[str, str]
    admin_id: uuid.UUID
    seeker_id: uuid.UUID
    advisor_id: uuid.UUID
    stripe: FakeStripe
    zoom: FakeZoom
    ai: FakeAI
    push: FakePush
    mailbox: list[Mail] = field(default_factory=list)

    async def login(self, email: str, password: str = PASSWORD) -> dict[str, str]:
        r = await self.client.post(
            "/api/v1/auth/login", data={"username": email, "password": password}
        )
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    def last_mail(self, kind: str, email: str) -> Mail:
        for m in reversed(self.mailbox):
            if m.kind == kind and m.email == email:
                return m
        raise AssertionError(f"no {kind} mail for {email}; mailbox={self.mailbox}")


def feature(key: str, value_type: str, value: str, *, order: int = 1):
    from app.schemas.pricing_plan import PlanFeatureInput

    return PlanFeatureInput(
        kind="enforceable",
        feature_key=key,
        label=key.replace("_", " ").title(),
        value_type=value_type,
        value=value,
        is_enabled=True,
        display_order=order,
    )


# What the flows need without buying anything. ``visa_journey`` is deliberately left out of
# the seeker free plan so the subscription flow has a feature that a paid plan unlocks.
SEEKER_FREE_FEATURES = (
    ("ai_assessments", "number", "unlimited"),
    ("assessment_history", "bool", "true"),
    ("matched_advisors", "bool", "true"),
    ("documents", "bool", "true"),
    ("chats", "bool", "true"),
    ("bookmarks", "bool", "true"),
)
ADVISOR_FREE_FEATURES = (
    ("profile_edits", "number", "unlimited"),
    ("leads", "number", "unlimited"),
    ("chats", "bool", "true"),
    ("support", "bool", "true"),
)


async def _default_free_plans(Session: Any) -> None:
    """Create the two default free plans through the real service (no Stripe price at $0)."""
    from decimal import Decimal

    from app.schemas.pricing_plan import PricingPlanCreate
    from app.services import pricing_plan_service

    for audience, features in (
        ("seeker", SEEKER_FREE_FEATURES),
        ("advisor", ADVISOR_FREE_FEATURES),
    ):
        body = PricingPlanCreate(
            audience=audience,
            name=f"Free {audience}",
            description=None,
            tagline=None,
            price_usd=Decimal("0"),
            billing_interval="month",
            is_highlighted=False,
            is_default=True,
            features=[feature(k, t, v, order=i + 1) for i, (k, t, v) in enumerate(features)],
        )
        async with Session() as s:
            await pricing_plan_service.create(s, body, uuid.uuid4())
            await s.commit()


@pytest.fixture
def fake_stripe(monkeypatch: pytest.MonkeyPatch) -> FakeStripe:
    f = FakeStripe()
    f.install(monkeypatch)
    return f


@pytest.fixture
def fake_zoom(monkeypatch: pytest.MonkeyPatch) -> FakeZoom:
    f = FakeZoom()
    f.install(monkeypatch)
    return f


@pytest.fixture
def fake_ai(monkeypatch: pytest.MonkeyPatch) -> FakeAI:
    f = FakeAI()
    f.install(monkeypatch)
    return f


@pytest.fixture
def fake_push(monkeypatch: pytest.MonkeyPatch) -> FakePush:
    f = FakePush()
    f.install(monkeypatch)
    return f


@pytest.fixture
def mailbox(monkeypatch: pytest.MonkeyPatch) -> list[Mail]:
    """Captures the e-mails the auth routes schedule, token included, at call time."""
    from app.api.v1 import auth as auth_module

    box: list[Mail] = []

    async def _noop() -> None:
        return None

    def capture(kind: str):
        def send(email: str, full_name: str, raw_token: str, settings: Any, *a: Any, **k: Any):
            box.append(Mail(kind=kind, email=email, token=raw_token))
            return _noop()

        return send

    monkeypatch.setattr(auth_module, "send_verification_email", capture("verify"))
    monkeypatch.setattr(auth_module, "send_password_reset_email", capture("reset"))
    return box


@pytest_asyncio.fixture
async def world(
    fake_stripe: FakeStripe,
    fake_zoom: FakeZoom,
    fake_ai: FakeAI,
    fake_push: FakePush,
    mailbox: list[Mail],
) -> AsyncIterator[World]:
    from asgi_lifespan import LifespanManager
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    import app.models  # noqa: F401
    from app.api.deps import get_settings
    from app.core.security import hash_password
    from app.db.base import Base
    from app.db.session import get_session
    from app.main import create_app
    from app.models.advisor_profile import AdvisorProfile
    from app.models.user import User, UserRole, VerificationStatus
    from app.schemas.payment_settings import StripeKeysUpdate
    from app.services import payment_config_service, stripe_config_service

    payment_config_service.invalidate_cache()
    stripe_config_service.invalidate_keys_cache()

    engine = create_async_engine(
        "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    admin_id, seeker_id, advisor_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    verified = datetime.now(UTC)
    async with Session() as s:
        s.add_all(
            [
                User(
                    id=admin_id,
                    email="admin@flows-world.com",
                    full_name="Flows Admin",
                    hashed_password=hash_password(PASSWORD),
                    role=UserRole.admin,
                    is_active=True,
                    email_verified_at=verified,
                ),
                User(
                    id=seeker_id,
                    email="seeker@flows-world.com",
                    full_name="Flows Seeker",
                    hashed_password=hash_password(PASSWORD),
                    role=UserRole.seeker,
                    is_active=True,
                    email_verified_at=verified,
                ),
                User(
                    id=advisor_id,
                    email="advisor@flows-world.com",
                    full_name="Flows Advisor",
                    hashed_password=hash_password(PASSWORD),
                    role=UserRole.advisor,
                    is_active=True,
                    email_verified_at=verified,
                    verification_status=VerificationStatus.approved,
                ),
                AdvisorProfile(
                    user_id=advisor_id,
                    title="Flows Immigration Advisor",
                    years_of_experience=5,
                    stripe_account_id="acct_flows_ready",
                    stripe_charges_enabled=True,
                    stripe_payouts_enabled=True,
                    stripe_details_submitted=True,
                    needs_stripe_connect=False,
                    needs_zoom_connect=False,
                ),
            ]
        )
        await s.commit()

    settings = get_settings()
    # Every audience has a default free plan in production; new users sit on it. Its
    # enforceable features are what the flows rely on without buying anything.
    await _default_free_plans(Session)
    # Stripe credentials come only from what an admin saved (no environment fallback).
    async with Session() as s:
        await stripe_config_service.save_keys(
            s,
            StripeKeysUpdate(
                secret_key=SECRET_KEY,
                publishable_key=PUBLISHABLE_KEY,
                webhook_secret=WEBHOOK_SECRET,
                mode="test",
            ),
            admin_id,
            settings,
        )
        await s.commit()
    stripe_config_service.invalidate_keys_cache()

    async def _override() -> AsyncIterator[AsyncSession]:
        async with Session() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app = create_app(settings)
    app.dependency_overrides[get_session] = _override
    async with (
        LifespanManager(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        w = World(
            client=client,
            Session=Session,
            settings=settings,
            admin={},
            seeker={},
            advisor={},
            admin_id=admin_id,
            seeker_id=seeker_id,
            advisor_id=advisor_id,
            stripe=fake_stripe,
            zoom=fake_zoom,
            ai=fake_ai,
            push=fake_push,
            mailbox=mailbox,
        )
        w.admin = await w.login("admin@flows-world.com")
        w.seeker = await w.login("seeker@flows-world.com")
        w.advisor = await w.login("advisor@flows-world.com")
        yield w
    await engine.dispose()
    stripe_config_service.invalidate_keys_cache()
    payment_config_service.invalidate_cache()
