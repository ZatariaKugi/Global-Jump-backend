"""Admin analytics dashboard tests (Overview / Advisor / Finance / AI / Engagement)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.security import hash_password
from app.models.assessment import Assessment, AssessmentStatus, EligibilityTier
from app.models.booking import Booking, BookingStatus
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.payout_request import PayoutMethod, PayoutRequest, PayoutStatus
from app.models.review import Review
from app.models.transaction import Transaction, TransactionStatus
from app.models.user import SignupSource, User, UserRole, VerificationStatus

ANALYTICS = "/api/v1/admin/analytics"


async def _seeker_token(client: AsyncClient, email: str = "seeker@test.com") -> str:
    await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "custpass123", "full_name": "Test Seeker"},
    )
    resp = await client.post(
        "/api/v1/auth/login", data={"username": email, "password": "custpass123"}
    )
    assert resp.status_code == 200, resp.text
    return str(resp.json()["access_token"])


async def _seed_user(
    engine,
    email: str,
    full_name: str,
    role: UserRole,
    created_at: datetime | None = None,
    **kwargs,
) -> uuid.UUID:
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        user = User(
            email=email,
            full_name=full_name,
            hashed_password=hash_password("password123"),
            role=role,
            **kwargs,
        )
        if created_at is not None:
            user.created_at = created_at
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user.id


async def _seed_booking(
    engine,
    seeker_id: uuid.UUID,
    advisor_id: uuid.UUID,
    status: BookingStatus,
    scheduled_start: datetime,
    duration_minutes: int = 60,
    price_usd: float = 100.0,
) -> uuid.UUID:
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        booking = Booking(
            seeker_id=seeker_id,
            advisor_id=advisor_id,
            service_type="consultation_60",
            duration_minutes=duration_minutes,
            price_usd=price_usd,
            scheduled_start=scheduled_start,
            scheduled_end=scheduled_start + timedelta(minutes=duration_minutes),
            status=status,
        )
        session.add(booking)
        await session.commit()
        await session.refresh(booking)
        return booking.id


async def _seed_review(
    engine, booking_id: uuid.UUID, seeker_id: uuid.UUID, advisor_id: uuid.UUID, rating: int
) -> None:
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        session.add(
            Review(
                booking_id=booking_id,
                seeker_id=seeker_id,
                advisor_id=advisor_id,
                rating_expertise=rating,
                rating_communication=rating,
                rating_professionalism=rating,
                rating_value=rating,
                rating_overall=float(rating),
            )
        )
        await session.commit()


async def _seed_transaction(
    engine,
    booking_id: uuid.UUID,
    amount_usd: float,
    status: TransactionStatus = TransactionStatus.succeeded,
    advisor_payout_usd: float = 0.0,
    refunded_at: datetime | None = None,
    refunded_amount_usd: float | None = None,
    created_at: datetime | None = None,
) -> uuid.UUID:
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        txn = Transaction(
            booking_id=booking_id,
            stripe_checkout_session_id=f"cs_test_{uuid.uuid4().hex[:8]}",
            amount_usd=amount_usd,
            commission_rate=0.15,
            commission_usd=round(amount_usd * 0.15, 2),
            tax_rate=0.08,
            tax_usd=round(amount_usd * 0.08, 2),
            advisor_payout_usd=advisor_payout_usd,
            status=status,
            refunded_at=refunded_at,
            refunded_amount_usd=refunded_amount_usd,
            created_at=created_at or datetime.now(UTC),
        )
        session.add(txn)
        await session.commit()
        await session.refresh(txn)
        return txn.id


async def _seed_payout(
    engine, advisor_id: uuid.UUID, amount_usd: float, processed_at: datetime
) -> None:
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        session.add(
            PayoutRequest(
                advisor_id=advisor_id,
                amount_usd=amount_usd,
                method=PayoutMethod.bank_transfer,
                processing_fee_usd=0.0,
                net_amount_usd=amount_usd,
                status=PayoutStatus.completed,
                processed_at=processed_at,
            )
        )
        await session.commit()


async def _seed_activity(engine, user_id: uuid.UUID, occurred_on) -> None:
    from app.models.activity_log import ActivityLog

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        session.add(ActivityLog(user_id=user_id, occurred_on=occurred_on))
        await session.commit()


# ── Shape / empty-state ──────────────────────────────────────────────────────


async def test_all_tabs_shape_on_empty_data(client: AsyncClient, admin_token: str) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    for tab in ("overview", "advisors", "finance", "ai", "engagement"):
        resp = await client.get(f"{ANALYTICS}/{tab}", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["window_days"] == 30


async def test_non_admin_forbidden_from_all_tabs(client: AsyncClient, admin_token: str) -> None:
    token = await _seeker_token(client)
    headers = {"Authorization": f"Bearer {token}"}
    for tab in ("overview", "advisors", "finance", "ai", "engagement"):
        resp = await client.get(f"{ANALYTICS}/{tab}", headers=headers)
        assert resp.status_code == 403


# ── Overview ─────────────────────────────────────────────────────────────────


async def test_overview_acquisition_sources_and_country(
    client: AsyncClient, admin_token: str
) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "paid@test.com",
            "password": "custpass123",
            "full_name": "Paid Seeker",
            "signup_source": "paid_ads",
        },
    )
    assert resp.status_code == 201, resp.text

    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": "organic@test.com", "password": "custpass123", "full_name": "Organic"},
    )
    assert resp.status_code == 201, resp.text
    login = await client.post(
        "/api/v1/auth/login", data={"username": "organic@test.com", "password": "custpass123"}
    )
    organic_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    resp = await client.patch(
        "/api/v1/users/me/profile",
        json={"country_of_residence": "GB"},
        headers=organic_headers,
    )
    assert resp.status_code == 200, resp.text

    resp = await client.get(f"{ANALYTICS}/overview", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]

    sources = {p["label"]: p["count"] for p in data["acquisition_sources"]}
    assert sources.get("paid_ads", 0) >= 1
    assert sources.get("organic", 0) >= 1

    countries = {p["label"]: p["count"] for p in data["users_by_country"]}
    assert countries.get("GB", 0) >= 1


async def test_overview_retention(client: AsyncClient, admin_token: str, engine) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    today = datetime.now(UTC)
    registered_at = today - timedelta(days=2)
    user_id = await _seed_user(
        engine, "retained@test.com", "Retained Seeker", UserRole.seeker, created_at=registered_at
    )
    await _seed_activity(engine, user_id, (registered_at + timedelta(days=1)).date())

    resp = await client.get(f"{ANALYTICS}/overview", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    retention = {p["day"]: p["retention_pct"] for p in resp.json()["data"]["retention"]}
    assert retention[1] == 100.0


# ── Advisor Analytics ────────────────────────────────────────────────────────


async def test_advisor_analytics_top_rated_and_completion(
    client: AsyncClient, admin_token: str, engine
) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    seeker_id = await _seed_user(engine, "seeker-adv@test.com", "Seeker", UserRole.seeker)
    advisor_a = await _seed_user(
        engine,
        "advisor-a@test.com",
        "Advisor A",
        UserRole.advisor,
        is_active=True,
        verification_status=VerificationStatus.approved,
    )
    advisor_b = await _seed_user(
        engine,
        "advisor-b@test.com",
        "Advisor B",
        UserRole.advisor,
        is_active=True,
        verification_status=VerificationStatus.approved,
    )
    now = datetime.now(UTC)

    booking_a = await _seed_booking(engine, seeker_id, advisor_a, BookingStatus.completed, now)
    await _seed_review(engine, booking_a, seeker_id, advisor_a, rating=5)
    booking_b = await _seed_booking(engine, seeker_id, advisor_b, BookingStatus.cancelled, now)
    await _seed_review(engine, booking_b, seeker_id, advisor_b, rating=3)

    resp = await client.get(f"{ANALYTICS}/advisors", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]

    top = data["top_rated_advisors"]
    assert top[0]["avg_rating"] >= top[-1]["avg_rating"]
    ratings_by_user = {t["user_id"]: t["avg_rating"] for t in top}
    assert ratings_by_user[str(advisor_a)] == 5.0
    assert ratings_by_user[str(advisor_b)] == 3.0

    assert data["session_completed_pct"] == 50.0


# ── Finance Analytics ────────────────────────────────────────────────────────


async def test_finance_analytics_revenue_and_refunds(
    client: AsyncClient, admin_token: str, engine
) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    seeker_id = await _seed_user(engine, "seeker-fin@test.com", "Seeker", UserRole.seeker)
    advisor_id = await _seed_user(
        engine,
        "advisor-fin@test.com",
        "Advisor",
        UserRole.advisor,
        is_active=True,
        verification_status=VerificationStatus.approved,
    )
    now = datetime.now(UTC)

    booking1 = await _seed_booking(engine, seeker_id, advisor_id, BookingStatus.completed, now)
    await _seed_transaction(
        engine,
        booking1,
        amount_usd=100.0,
        status=TransactionStatus.succeeded,
        advisor_payout_usd=80.0,
    )

    booking2 = await _seed_booking(engine, seeker_id, advisor_id, BookingStatus.completed, now)
    await _seed_transaction(
        engine,
        booking2,
        amount_usd=50.0,
        status=TransactionStatus.partially_refunded,
        advisor_payout_usd=35.0,
        refunded_at=now,
        refunded_amount_usd=20.0,
    )

    await _seed_payout(engine, advisor_id, amount_usd=80.0, processed_at=now)

    resp = await client.get(f"{ANALYTICS}/finance", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]

    assert data["gross_revenue_usd"] == 150.0
    assert data["refunds_usd"] == 20.0
    assert data["net_revenue_usd"] == 130.0
    assert data["advisor_payout_usd"] == 80.0


# ── AI Analytics ─────────────────────────────────────────────────────────────


async def test_ai_analytics_pass_fail_volume_and_drop_off(
    client: AsyncClient, admin_token: str, engine
) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    seeker_id = await _seed_user(engine, "seeker-ai@test.com", "Seeker", UserRole.seeker)
    now = datetime.now(UTC)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        session.add_all(
            [
                Assessment(
                    user_id=seeker_id,
                    destination_country="GB",
                    visa_type="work",
                    status=AssessmentStatus.completed,
                    score=90.0,
                    tier=EligibilityTier.highly_eligible,
                    confidence=0.9,
                    completed_at=now,
                    created_at=now,
                ),
                Assessment(
                    user_id=seeker_id,
                    destination_country="GB",
                    visa_type="work",
                    status=AssessmentStatus.completed,
                    score=40.0,
                    tier=EligibilityTier.low_eligibility,
                    confidence=0.8,
                    completed_at=now,
                    created_at=now,
                ),
                Assessment(
                    user_id=seeker_id,
                    destination_country="GB",
                    visa_type="work",
                    status=AssessmentStatus.in_progress,
                    created_at=now,
                ),
            ]
        )
        await session.commit()

    resp = await client.get(f"{ANALYTICS}/ai", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]

    assert set(data.keys()) >= {
        "window_days",
        "pass_rate",
        "fail_rate",
        "assessment_volume",
        "drop_off_points",
    }
    assert "recommendation_effectiveness" not in data
    assert "match_score_distribution" not in data
    assert "session_duration_distribution" not in data
    assert "eligibility_assessments_trend" not in data

    assert data["pass_rate"] == 50.0
    assert data["fail_rate"] == 50.0
    assert len(data["assessment_volume"]) == 1
    assert data["assessment_volume"][0]["month"] == now.strftime("%b")
    assert data["assessment_volume"][0]["value"] == 3
    # Abandoned with no questions → Before Q1; value is % of started (1/3 ≈ 33.3)
    assert data["drop_off_points"]
    assert data["drop_off_points"][0]["stage"] == "Before Q1"
    assert data["drop_off_points"][0]["value"] == 33.3


# ── Engagement Analytics ─────────────────────────────────────────────────────


async def test_engagement_response_time_and_call_hours(
    client: AsyncClient, admin_token: str, engine
) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    seeker_id = await _seed_user(engine, "seeker-eng@test.com", "Seeker", UserRole.seeker)
    advisor_id = await _seed_user(
        engine,
        "advisor-eng@test.com",
        "Advisor",
        UserRole.advisor,
        is_active=True,
        verification_status=VerificationStatus.approved,
    )
    now = datetime.now(UTC)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        conversation = Conversation(seeker_id=seeker_id, advisor_id=advisor_id)
        session.add(conversation)
        await session.flush()
        session.add_all(
            [
                Message(
                    conversation_id=conversation.id,
                    sender_id=seeker_id,
                    body="hi",
                    created_at=now,
                ),
                Message(
                    conversation_id=conversation.id,
                    sender_id=advisor_id,
                    body="hello",
                    created_at=now + timedelta(hours=2),
                ),
                Message(
                    conversation_id=conversation.id,
                    sender_id=seeker_id,
                    body="thanks",
                    created_at=now + timedelta(hours=3),
                ),
            ]
        )
        await session.commit()

    await _seed_booking(
        engine, seeker_id, advisor_id, BookingStatus.completed, now, duration_minutes=90
    )
    await _seed_booking(
        engine, seeker_id, advisor_id, BookingStatus.completed, now, duration_minutes=30
    )

    resp = await client.get(f"{ANALYTICS}/engagement", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]

    assert data["avg_response_time_hours"] == 1.5
    assert data["messages_sent"] == 3
    assert data["session_completed"] == 2

    month = now.strftime("%Y-%m")
    video_by_month = {p["month"]: p["amount_usd"] for p in data["video_call_hours_trend"]}
    duration_by_month = {p["month"]: p["amount_usd"] for p in data["session_duration_trend"]}
    assert video_by_month[month] == 2.0
    assert duration_by_month[month] == 1.0


# ── Signup source default ────────────────────────────────────────────────────


async def test_signup_source_defaults_to_organic(engine) -> None:
    user_id = await _seed_user(engine, "default-source@test.com", "Default", UserRole.seeker)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        user = await session.get(User, user_id)
        assert user is not None
        assert user.signup_source == SignupSource.organic
