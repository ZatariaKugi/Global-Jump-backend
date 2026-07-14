"""Admin "Visa Seeker Management" tests: list/filter, counts, add-seeker flow."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.assessment import Assessment, AssessmentStatus
from app.models.booking import BookingStatus
from app.models.seeker_profile import SeekerProfile
from app.models.token import TokenPurpose, UserToken
from app.models.user import User, UserRole
from tests.test_analytics import _seed_booking, _seed_user

SEEKERS = "/api/v1/admin/seekers"


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


async def _seed_seeker_profile(
    engine,
    user_id: uuid.UUID,
    country_of_residence: str | None = None,
    intended_visa_type: str | None = None,
) -> None:
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        session.add(
            SeekerProfile(
                user_id=user_id,
                country_of_residence=country_of_residence,
                intended_visa_type=intended_visa_type,
            )
        )
        await session.commit()


async def _seed_assessment(engine, user_id: uuid.UUID) -> None:
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        session.add(
            Assessment(
                user_id=user_id,
                destination_country="GB",
                visa_type="work",
                status=AssessmentStatus.in_progress,
            )
        )
        await session.commit()


# ── List / filter ─────────────────────────────────────────────────────────────


async def test_list_seekers_shape_on_empty_data(client: AsyncClient, admin_token: str) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    resp = await client.get(SEEKERS, headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == []
    assert resp.json()["meta"]["pagination"]["total"] == 0


async def test_study_visa_filter(client: AsyncClient, admin_token: str, engine) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    work_id = await _seed_user(engine, "work-seeker@test.com", "Work Seeker", UserRole.seeker)
    await _seed_seeker_profile(engine, work_id, intended_visa_type="work")
    study_id = await _seed_user(engine, "study-seeker@test.com", "Study Seeker", UserRole.seeker)
    await _seed_seeker_profile(engine, study_id, intended_visa_type="study")

    resp = await client.get(f"{SEEKERS}?study_visa=study", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert len(data) == 1
    assert data[0]["full_name"] == "Study Seeker"


async def test_status_filter(client: AsyncClient, admin_token: str, engine) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    await _seed_user(
        engine, "suspended-seeker@test.com", "Suspended Seeker", UserRole.seeker, is_active=False
    )
    await _seed_user(engine, "unverified-seeker@test.com", "Unverified Seeker", UserRole.seeker)

    resp = await client.get(f"{SEEKERS}?status=suspended", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert len(data) == 1
    assert data[0]["full_name"] == "Suspended Seeker"
    assert data[0]["status"] == "suspended"

    resp = await client.get(f"{SEEKERS}?status=unverified", headers=admin_headers)
    data = resp.json()["data"]
    assert len(data) == 1
    assert data[0]["full_name"] == "Unverified Seeker"


async def test_ai_assessment_and_booking_counts(
    client: AsyncClient, admin_token: str, engine
) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    seeker_id = await _seed_user(
        engine, "counted-seeker@test.com", "Counted Seeker", UserRole.seeker
    )
    advisor_id = await _seed_user(
        engine, "counted-advisor@test.com", "Counted Advisor", UserRole.advisor, is_active=True
    )
    await _seed_assessment(engine, seeker_id)
    await _seed_assessment(engine, seeker_id)
    now = datetime.now(UTC)
    for _ in range(3):
        await _seed_booking(engine, seeker_id, advisor_id, BookingStatus.completed, now)

    resp = await client.get(SEEKERS, headers=admin_headers)
    assert resp.status_code == 200, resp.text
    row = next(d for d in resp.json()["data"] if d["full_name"] == "Counted Seeker")
    assert row["ai_assessment_count"] == 2
    assert row["total_bookings"] == 3

    resp = await client.get(f"{SEEKERS}/{seeker_id}", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    detail = resp.json()["data"]
    assert detail["ai_assessment_count"] == 2
    assert detail["total_bookings"] == 3


# ── Add Visa Seeker ──────────────────────────────────────────────────────────


async def test_create_seeker_happy_path(client: AsyncClient, admin_token: str, engine) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    resp = await client.post(
        SEEKERS,
        json={
            "email": "invited@test.com",
            "full_name": "Invited Seeker",
            "country_of_residence": "PK",
            "intended_visa_type": "work",
        },
        headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["full_name"] == "Invited Seeker"
    assert data["country_of_residence"] == "PK"
    assert data["status"] == "unverified"

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        user = (
            await session.execute(select(User).where(User.email == "invited@test.com"))
        ).scalar_one()
        assert user.role == UserRole.seeker

        token_row = (
            await session.execute(
                select(UserToken).where(
                    UserToken.user_id == user.id, UserToken.purpose == TokenPurpose.password_reset
                )
            )
        ).scalar_one_or_none()
        assert token_row is not None

    # The random placeholder password is never usable — login must fail.
    login = await client.post(
        "/api/v1/auth/login",
        data={"username": "invited@test.com", "password": "guessableguess1"},
    )
    assert login.status_code == 401


async def test_create_seeker_duplicate_email(client: AsyncClient, admin_token: str) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    body = {"email": "dupe@test.com", "full_name": "Dupe Seeker"}
    first = await client.post(SEEKERS, json=body, headers=admin_headers)
    assert first.status_code == 201, first.text
    second = await client.post(SEEKERS, json=body, headers=admin_headers)
    assert second.status_code == 409


# ── Auth ──────────────────────────────────────────────────────────────────────


async def test_non_admin_forbidden(client: AsyncClient, admin_token: str) -> None:
    token = await _seeker_token(client)
    headers = {"Authorization": f"Bearer {token}"}
    assert (await client.get(SEEKERS, headers=headers)).status_code == 403
    assert (
        await client.post(SEEKERS, json={"email": "x@test.com", "full_name": "X"}, headers=headers)
    ).status_code == 403
