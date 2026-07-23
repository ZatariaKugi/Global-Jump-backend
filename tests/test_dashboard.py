"""Admin dashboard home screen tests (stat cards, charts, activity feed)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.advisor_credential import AdvisorCredential, CredentialStatus, DocumentType
from app.models.booking import Booking, BookingStatus
from app.models.review import ModerationStatus, Review
from app.models.seeker_document import DocumentCategory, SeekerDocument, SeekerDocumentStatus
from app.models.seeker_profile import SeekerProfile
from app.models.user import UserRole, VerificationStatus
from tests.test_analytics import _seed_booking, _seed_transaction, _seed_user

DASHBOARD = "/api/v1/admin/dashboard"
ACTIVITIES = "/api/v1/admin/activities"


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


async def _seed_seeker_document(engine, seeker_id: uuid.UUID, created_at: datetime) -> None:
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        doc = SeekerDocument(
            seeker_id=seeker_id,
            category=DocumentCategory.passport,
            document_name="passport.pdf",
            file_url="/uploads/seeker_document/x/passport.pdf",
            content_type="application/pdf",
            status=SeekerDocumentStatus.under_review,
        )
        session.add(doc)
        await session.commit()
        await session.refresh(doc)
        doc.created_at = created_at
        session.add(doc)
        await session.commit()


async def _seed_advisor_credential(engine, user_id: uuid.UUID, created_at: datetime) -> None:
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        cred = AdvisorCredential(
            user_id=user_id,
            document_type=DocumentType.certification,
            document_name="cert.pdf",
            file_url="/uploads/advisor_credential/x/cert.pdf",
            status=CredentialStatus.pending,
        )
        session.add(cred)
        await session.commit()
        await session.refresh(cred)
        cred.created_at = created_at
        session.add(cred)
        await session.commit()


async def _seed_completed_booking(
    engine, seeker_id: uuid.UUID, advisor_id: uuid.UUID, updated_at: datetime
) -> uuid.UUID:
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        booking = Booking(
            seeker_id=seeker_id,
            advisor_id=advisor_id,
            service_type="consultation_60",
            duration_minutes=60,
            price_usd=100.0,
            scheduled_start=updated_at,
            scheduled_end=updated_at + timedelta(hours=1),
            status=BookingStatus.completed,
        )
        session.add(booking)
        await session.commit()
        await session.refresh(booking)
        booking.updated_at = updated_at
        session.add(booking)
        await session.commit()
        return booking.id


async def _seed_flagged_review(
    engine, booking_id: uuid.UUID, seeker_id: uuid.UUID, advisor_id: uuid.UUID, updated_at: datetime
) -> None:
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        review = Review(
            booking_id=booking_id,
            seeker_id=seeker_id,
            advisor_id=advisor_id,
            rating_expertise=3,
            rating_communication=3,
            rating_professionalism=3,
            rating_value=3,
            rating_overall=3.0,
            moderation_status=ModerationStatus.flagged,
        )
        session.add(review)
        await session.commit()
        await session.refresh(review)
        review.updated_at = updated_at
        session.add(review)
        await session.commit()


async def _seed_seeker_profile(engine, user_id: uuid.UUID, country_of_residence: str) -> None:
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        session.add(SeekerProfile(user_id=user_id, country_of_residence=country_of_residence))
        await session.commit()


# ── Shape / empty-state ──────────────────────────────────────────────────────


async def test_dashboard_shape_on_empty_data(client: AsyncClient, admin_token: str) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    resp = await client.get(DASHBOARD, headers=admin_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["window_days"] == 180
    assert data["total_users"] == 1  # the admin fixture itself
    assert data["total_advisors"] == 0
    assert data["active_advisors"] == 0
    assert data["revenue_today_usd"] == 0.0
    this_month = datetime.now(UTC).strftime("%Y-%m")
    assert data["user_registration_trend"] == [{"month": this_month, "count": 1}]
    assert data["ai_assessment_volume"] == []
    assert data["revenue_breakdown"] == []
    assert data["recent_activities"] == []


async def test_activities_shape_on_empty_data(client: AsyncClient, admin_token: str) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    resp = await client.get(ACTIVITIES, headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == []
    assert resp.json()["meta"]["pagination"]["total"] == 0


async def test_non_admin_forbidden(client: AsyncClient, admin_token: str) -> None:
    token = await _seeker_token(client)
    headers = {"Authorization": f"Bearer {token}"}
    assert (await client.get(DASHBOARD, headers=headers)).status_code == 403
    assert (await client.get(ACTIVITIES, headers=headers)).status_code == 403


# ── Stat cards ────────────────────────────────────────────────────────────────


async def test_dashboard_stat_cards_exact_counts(
    client: AsyncClient, admin_token: str, engine
) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    for i in range(3):
        await _seed_user(engine, f"seeker{i}@test.com", f"Seeker {i}", UserRole.seeker)
    await _seed_user(
        engine,
        "active-advisor@test.com",
        "Active Advisor",
        UserRole.advisor,
        is_active=True,
        verification_status=VerificationStatus.approved,
    )
    await _seed_user(
        engine,
        "pending-advisor@test.com",
        "Pending Advisor",
        UserRole.advisor,
        is_active=False,
        verification_status=VerificationStatus.pending,
    )

    resp = await client.get(DASHBOARD, headers=admin_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["total_users"] == 6  # 3 seekers + 2 advisors + 1 admin fixture
    assert data["total_advisors"] == 2
    assert data["active_advisors"] == 1


async def test_dashboard_revenue_today(client: AsyncClient, admin_token: str, engine) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    seeker_id = await _seed_user(engine, "seeker-rt@test.com", "Seeker", UserRole.seeker)
    advisor_id = await _seed_user(
        engine,
        "advisor-rt@test.com",
        "Advisor",
        UserRole.advisor,
        is_active=True,
        verification_status=VerificationStatus.approved,
    )
    now = datetime.now(UTC)
    yesterday = now - timedelta(days=1)

    booking_today = await _seed_booking(engine, seeker_id, advisor_id, BookingStatus.completed, now)
    await _seed_transaction(engine, booking_today, amount_usd=100.0, created_at=now)

    booking_yesterday = await _seed_booking(
        engine, seeker_id, advisor_id, BookingStatus.completed, yesterday
    )
    await _seed_transaction(engine, booking_yesterday, amount_usd=50.0, created_at=yesterday)

    resp = await client.get(DASHBOARD, headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["revenue_today_usd"] == 100.0


# ── Revenue breakdown ─────────────────────────────────────────────────────────


async def test_revenue_breakdown_bucketing(client: AsyncClient, admin_token: str, engine) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    seeker_id = await _seed_user(engine, "seeker-rb@test.com", "Seeker", UserRole.seeker)
    advisor_id = await _seed_user(
        engine,
        "advisor-rb@test.com",
        "Advisor",
        UserRole.advisor,
        is_active=True,
        verification_status=VerificationStatus.approved,
    )
    now = datetime.now(UTC)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        consult_booking = Booking(
            seeker_id=seeker_id,
            advisor_id=advisor_id,
            service_type="immigration_specialist",
            duration_minutes=30,
            price_usd=100.0,
            scheduled_start=now,
            scheduled_end=now + timedelta(minutes=30),
            status=BookingStatus.completed,
        )
        review_booking = Booking(
            seeker_id=seeker_id,
            advisor_id=advisor_id,
            service_type="document_review",
            duration_minutes=30,
            price_usd=50.0,
            scheduled_start=now,
            scheduled_end=now + timedelta(minutes=30),
            status=BookingStatus.completed,
        )
        session.add_all([consult_booking, review_booking])
        await session.commit()
        await session.refresh(consult_booking)
        await session.refresh(review_booking)
        consult_booking_id, review_booking_id = consult_booking.id, review_booking.id

    await _seed_transaction(engine, consult_booking_id, amount_usd=100.0, created_at=now)
    await _seed_transaction(engine, review_booking_id, amount_usd=50.0, created_at=now)

    resp = await client.get(DASHBOARD, headers=admin_headers)
    assert resp.status_code == 200, resp.text
    breakdown = {s["label"]: s for s in resp.json()["data"]["revenue_breakdown"]}
    assert "Others" not in breakdown
    assert breakdown["Consultant"]["amount_usd"] == 100.0
    assert breakdown["Consultant"]["pct"] == 66.67
    assert breakdown["Document Review"]["amount_usd"] == 50.0
    assert breakdown["Document Review"]["pct"] == 33.33


# ── Activity feed ─────────────────────────────────────────────────────────────


async def test_activity_feed_merge_and_sort(client: AsyncClient, admin_token: str, engine) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    now = datetime.now(UTC)

    t_review_flagged = now - timedelta(days=6)
    t_document_uploaded = now - timedelta(days=5)
    t_refund_request = now - timedelta(days=4)
    t_session_completed = now - timedelta(days=3)
    t_advisor_application = now - timedelta(days=2)
    t_new_user = now - timedelta(days=1)

    # Registered well outside the default 180-day activities window so these
    # helper accounts' own signup/application events don't pollute the feed.
    outside_window = now - timedelta(days=200)
    seeker_id = await _seed_user(
        engine, "flow-seeker@test.com", "Flow Seeker", UserRole.seeker, created_at=outside_window
    )
    advisor_id = await _seed_user(
        engine,
        "flow-advisor@test.com",
        "Flow Advisor",
        UserRole.advisor,
        is_active=True,
        verification_status=VerificationStatus.approved,
        created_at=outside_window,
    )

    # 1. New Review Flagged (oldest) — booking left `confirmed` (not `completed`) so it
    # doesn't also register as a spurious Session Completed event.
    flagged_booking = await _seed_booking(
        engine, seeker_id, advisor_id, BookingStatus.confirmed, t_review_flagged
    )
    await _seed_flagged_review(engine, flagged_booking, seeker_id, advisor_id, t_review_flagged)

    # 2. Document Uploaded
    await _seed_seeker_document(engine, seeker_id, t_document_uploaded)

    # 3. Refund Request — same reasoning, kept `confirmed`.
    refund_booking = await _seed_booking(
        engine, seeker_id, advisor_id, BookingStatus.confirmed, t_refund_request
    )
    await _seed_transaction(
        engine,
        refund_booking,
        amount_usd=50.0,
        refunded_at=t_refund_request,
        refunded_amount_usd=50.0,
        created_at=t_refund_request,
    )

    # 4. Session Completed
    await _seed_completed_booking(engine, seeker_id, advisor_id, t_session_completed)

    # 5. Advisor Application Submitted
    await _seed_user(
        engine,
        "new-advisor@test.com",
        "New Advisor",
        UserRole.advisor,
        is_active=False,
        verification_status=VerificationStatus.pending,
        created_at=t_advisor_application,
    )

    # 6. New User Register (newest)
    await _seed_user(
        engine, "new-seeker@test.com", "New Seeker", UserRole.seeker, created_at=t_new_user
    )

    resp = await client.get(f"{ACTIVITIES}?page_size=20", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    items = resp.json()["data"]

    expected_order = [
        "new_user_registered",
        "advisor_application_submitted",
        "session_completed",
        "refund_request",
        "document_uploaded",
        "review_flagged",
    ]
    assert [item["event_type"] for item in items] == expected_order
    assert items[0]["title"] == "New User Register"
    assert items[2]["title"] == "Session Completed"


async def test_activities_country_suffix(client: AsyncClient, admin_token: str, engine) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    with_country = await _seed_user(engine, "with-country@test.com", "Has Country", UserRole.seeker)
    await _seed_seeker_profile(engine, with_country, "PK")
    await _seed_user(engine, "no-country@test.com", "No Country", UserRole.seeker)

    resp = await client.get(f"{ACTIVITIES}?page_size=20", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    descriptions = {
        item["description"]
        for item in resp.json()["data"]
        if item["event_type"] == "new_user_registered"
    }
    assert any("Has Country (PK)" in d for d in descriptions)
    assert any(d == "No Country just signed up" for d in descriptions)


async def test_activities_pagination(client: AsyncClient, admin_token: str, engine) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    now = datetime.now(UTC)
    for i in range(25):
        await _seed_user(
            engine,
            f"page-seeker-{i}@test.com",
            f"Page Seeker {i}",
            UserRole.seeker,
            created_at=now - timedelta(minutes=i),
        )

    page1 = await client.get(f"{ACTIVITIES}?page=1&page_size=10", headers=admin_headers)
    page2 = await client.get(f"{ACTIVITIES}?page=2&page_size=10", headers=admin_headers)
    assert page1.status_code == 200, page1.text
    assert page2.status_code == 200, page2.text

    page1_data = page1.json()
    page2_data = page2.json()
    assert page1_data["meta"]["pagination"]["total"] == 25
    assert page1_data["meta"]["pagination"]["pages"] == 3
    assert len(page1_data["data"]) == 10
    assert len(page2_data["data"]) == 10

    page1_times = [item["occurred_at"] for item in page1_data["data"]]
    page2_times = [item["occurred_at"] for item in page2_data["data"]]
    assert set(page1_times).isdisjoint(set(page2_times))
    assert page1_times == sorted(page1_times, reverse=True)
