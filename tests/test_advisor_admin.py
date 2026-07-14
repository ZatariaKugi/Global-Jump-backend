"""Admin "Advisor Management" + "Verification Queue" tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.advisor_credential import AdvisorCredential, CredentialStatus, DocumentType
from app.models.booking import BookingStatus
from app.models.payout_request import PayoutMethod, PayoutRequest, PayoutStatus
from app.models.review import ModerationStatus, Review
from app.models.user import UserRole, VerificationStatus
from tests.test_analytics import _seed_booking, _seed_payout, _seed_transaction, _seed_user

ADVISORS = "/api/v1/admin/advisors"
VERIFICATION_QUEUE = "/api/v1/admin/verification-queue"


async def _seed_credential(
    engine,
    advisor_id: uuid.UUID,
    status: CredentialStatus = CredentialStatus.pending,
    document_name: str = "License",
    file_url: str = "/uploads/credentials/x/y.pdf",
    created_at: datetime | None = None,
) -> uuid.UUID:
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        cred = AdvisorCredential(
            user_id=advisor_id,
            document_type=DocumentType.immigration_license,
            document_name=document_name,
            file_url=file_url,
            status=status,
        )
        if created_at is not None:
            cred.created_at = created_at
        session.add(cred)
        await session.commit()
        await session.refresh(cred)
        return cred.id


async def _seed_payout_pending(engine, advisor_id: uuid.UUID, amount_usd: float) -> None:
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        session.add(
            PayoutRequest(
                advisor_id=advisor_id,
                amount_usd=amount_usd,
                method=PayoutMethod.bank_transfer,
                processing_fee_usd=0.0,
                net_amount_usd=amount_usd,
                status=PayoutStatus.pending,
            )
        )
        await session.commit()


async def _seed_review_with_status(
    engine,
    booking_id: uuid.UUID,
    seeker_id: uuid.UUID,
    advisor_id: uuid.UUID,
    moderation_status: ModerationStatus,
) -> None:
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        session.add(
            Review(
                booking_id=booking_id,
                seeker_id=seeker_id,
                advisor_id=advisor_id,
                rating_expertise=4,
                rating_communication=4,
                rating_professionalism=4,
                rating_value=4,
                rating_overall=4.0,
                moderation_status=moderation_status,
            )
        )
        await session.commit()


# ── Advisor Management list ──────────────────────────────────────────────────


async def test_advisor_list_shape_and_columns(
    client: AsyncClient, admin_token: str, engine
) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    advisor_id = await _seed_user(
        engine,
        "advisor1@test.com",
        "Advisor One",
        UserRole.advisor,
        is_active=True,
        verification_status=VerificationStatus.approved,
    )
    seeker_id = await _seed_user(engine, "seeker1@test.com", "Seeker One", UserRole.seeker)
    now = datetime.now(UTC)
    booking_id = await _seed_booking(
        engine, seeker_id, advisor_id, BookingStatus.completed, now - timedelta(days=1)
    )
    await _seed_booking(engine, seeker_id, advisor_id, BookingStatus.confirmed, now)
    await _seed_review_with_status(
        engine, booking_id, seeker_id, advisor_id, ModerationStatus.visible
    )

    resp = await client.get(ADVISORS, headers=admin_headers)
    assert resp.status_code == 200, resp.text
    row = next(d for d in resp.json()["data"] if d["full_name"] == "Advisor One")
    assert row["session_count"] == 2
    assert row["avg_rating"] == 4.0
    assert row["review_count"] == 1
    assert row["verification_status"] == "approved"
    assert row["expertise"] == []
    assert row["created_at"] is not None


async def test_advisor_list_search_and_status_filter(
    client: AsyncClient, admin_token: str, engine
) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    await _seed_user(
        engine,
        "approved@test.com",
        "Approved Advisor",
        UserRole.advisor,
        is_active=True,
        verification_status=VerificationStatus.approved,
    )
    await _seed_user(
        engine,
        "pending@test.com",
        "Pending Advisor",
        UserRole.advisor,
        verification_status=VerificationStatus.pending,
    )

    resp = await client.get(f"{ADVISORS}?status=pending", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert len(data) == 1
    assert data[0]["full_name"] == "Pending Advisor"

    resp = await client.get(f"{ADVISORS}?search=Approved", headers=admin_headers)
    data = resp.json()["data"]
    assert len(data) == 1
    assert data[0]["full_name"] == "Approved Advisor"


# ── Advisor detail — Overview tab ────────────────────────────────────────────


async def test_advisor_detail_overview_tab(client: AsyncClient, admin_token: str, engine) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    advisor_id = await _seed_user(
        engine, "detail-advisor@test.com", "Detail Advisor", UserRole.advisor
    )
    seeker_id = await _seed_user(engine, "detail-seeker@test.com", "Detail Seeker", UserRole.seeker)
    now = datetime.now(UTC)
    await _seed_booking(engine, seeker_id, advisor_id, BookingStatus.completed, now)
    await _seed_booking(engine, seeker_id, advisor_id, BookingStatus.completed, now)
    await _seed_booking(engine, seeker_id, advisor_id, BookingStatus.confirmed, now)
    await _seed_credential(engine, advisor_id, status=CredentialStatus.pending)
    await _seed_credential(engine, advisor_id, status=CredentialStatus.pending)
    await _seed_credential(engine, advisor_id, status=CredentialStatus.verified)

    resp = await client.get(f"{ADVISORS}/{advisor_id}", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["session_count"] == 3
    assert data["completed_sessions"] == 2
    assert data["credentials_pending_count"] == 2
    assert data["credentials_verified_count"] == 1


async def test_advisor_detail_404_for_non_advisor(
    client: AsyncClient, admin_token: str, engine
) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    seeker_id = await _seed_user(engine, "not-advisor@test.com", "Not Advisor", UserRole.seeker)
    resp = await client.get(f"{ADVISORS}/{seeker_id}", headers=admin_headers)
    assert resp.status_code == 404


# ── Session History tab ──────────────────────────────────────────────────────


async def test_session_history_tab_paginated(client: AsyncClient, admin_token: str, engine) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    advisor_id = await _seed_user(
        engine, "sessions-advisor@test.com", "Sessions Advisor", UserRole.advisor
    )
    seeker_id = await _seed_user(
        engine, "sessions-seeker@test.com", "Sessions Seeker", UserRole.seeker
    )
    now = datetime.now(UTC)
    for i in range(3):
        await _seed_booking(
            engine, seeker_id, advisor_id, BookingStatus.completed, now + timedelta(hours=i)
        )

    resp = await client.get(f"{ADVISORS}/{advisor_id}/sessions", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["meta"]["pagination"]["total"] == 3
    assert body["data"][0]["seeker_name"] == "Sessions Seeker"


# ── Earnings tab ──────────────────────────────────────────────────────────────


async def test_earnings_tab_summary_and_sublists(
    client: AsyncClient, admin_token: str, engine
) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    advisor_id = await _seed_user(
        engine, "earnings-advisor@test.com", "Earnings Advisor", UserRole.advisor
    )
    seeker_id = await _seed_user(
        engine, "earnings-seeker@test.com", "Earnings Seeker", UserRole.seeker
    )
    booking_id = await _seed_booking(
        engine, seeker_id, advisor_id, BookingStatus.completed, datetime.now(UTC)
    )
    await _seed_transaction(engine, booking_id, amount_usd=100.0, advisor_payout_usd=80.0)
    await _seed_payout(engine, advisor_id, 50.0, datetime.now(UTC))
    await _seed_payout_pending(engine, advisor_id, 10.0)

    resp = await client.get(f"{ADVISORS}/{advisor_id}/earnings", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["total_earned_usd"] == 80.0
    assert data["total_payouts_usd"] == 50.0
    assert data["pending_payout_usd"] == 10.0
    assert data["available_balance_usd"] == 20.0  # 80 earned - (50 completed + 10 pending)
    assert data["transaction_count"] == 1

    resp = await client.get(f"{ADVISORS}/{advisor_id}/earnings/transactions", headers=admin_headers)
    assert resp.json()["meta"]["pagination"]["total"] == 1

    resp = await client.get(f"{ADVISORS}/{advisor_id}/earnings/payouts", headers=admin_headers)
    assert resp.json()["meta"]["pagination"]["total"] == 2


# ── Reviews tab ───────────────────────────────────────────────────────────────


async def test_reviews_tab_only_public_statuses(
    client: AsyncClient, admin_token: str, engine
) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    advisor_id = await _seed_user(
        engine, "reviews-advisor@test.com", "Reviews Advisor", UserRole.advisor
    )
    seeker_id = await _seed_user(
        engine, "reviews-seeker@test.com", "Reviews Seeker", UserRole.seeker
    )
    now = datetime.now(UTC)
    visible_booking = await _seed_booking(
        engine, seeker_id, advisor_id, BookingStatus.completed, now
    )
    removed_booking = await _seed_booking(
        engine, seeker_id, advisor_id, BookingStatus.completed, now + timedelta(hours=1)
    )
    await _seed_review_with_status(
        engine, visible_booking, seeker_id, advisor_id, ModerationStatus.visible
    )
    await _seed_review_with_status(
        engine, removed_booking, seeker_id, advisor_id, ModerationStatus.removed
    )

    resp = await client.get(f"{ADVISORS}/{advisor_id}/reviews", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["meta"]["pagination"]["total"] == 1
    assert body["data"][0]["booking_id"] == str(visible_booking)


# ── Verify / Suspend regression guard ────────────────────────────────────────


async def test_verify_and_suspend_reuse_existing_endpoints_unaffected(
    client: AsyncClient, admin_token: str, engine
) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    advisor_id = await _seed_user(
        engine, "verify-advisor@test.com", "Verify Advisor", UserRole.advisor
    )

    resp = await client.patch(
        f"{ADVISORS}/{advisor_id}/verification", json={"status": "approved"}, headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["verification_status"] == "approved"

    resp = await client.post(f"/api/v1/admin/users/{advisor_id}/suspend", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["is_active"] is False


# ── Verification Queue ───────────────────────────────────────────────────────


async def test_verification_queue_groups_by_advisor_not_by_credential(
    client: AsyncClient, admin_token: str, engine
) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    advisor_id = await _seed_user(
        engine, "queue-advisor@test.com", "Queue Advisor", UserRole.advisor
    )
    for _ in range(3):
        await _seed_credential(engine, advisor_id, status=CredentialStatus.pending)

    resp = await client.get(VERIFICATION_QUEUE, headers=admin_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert len(data) == 1
    assert data[0]["advisor_id"] == str(advisor_id)
    assert data[0]["pending_document_count"] == 3


async def test_verification_queue_excludes_fully_resolved_advisors(
    client: AsyncClient, admin_token: str, engine
) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    advisor_id = await _seed_user(
        engine, "resolved-advisor@test.com", "Resolved Advisor", UserRole.advisor
    )
    await _seed_credential(engine, advisor_id, status=CredentialStatus.verified)

    resp = await client.get(VERIFICATION_QUEUE, headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == []


async def test_verification_queue_earliest_latest_dates(
    client: AsyncClient, admin_token: str, engine
) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    advisor_id = await _seed_user(
        engine, "dates-advisor@test.com", "Dates Advisor", UserRole.advisor
    )
    earlier = datetime.now(UTC) - timedelta(days=5)
    later = datetime.now(UTC) - timedelta(days=1)
    await _seed_credential(engine, advisor_id, status=CredentialStatus.pending, created_at=earlier)
    await _seed_credential(engine, advisor_id, status=CredentialStatus.pending, created_at=later)

    resp = await client.get(VERIFICATION_QUEUE, headers=admin_headers)
    assert resp.status_code == 200, resp.text
    row = resp.json()["data"][0]
    assert row["earliest_submitted_at"][:10] == earlier.date().isoformat()
    assert row["latest_submitted_at"][:10] == later.date().isoformat()


# ── Documents panel ───────────────────────────────────────────────────────────


async def test_documents_panel_returns_only_pending_and_format(
    client: AsyncClient, admin_token: str, engine
) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    advisor_id = await _seed_user(engine, "docs-advisor@test.com", "Docs Advisor", UserRole.advisor)
    await _seed_credential(
        engine, advisor_id, status=CredentialStatus.pending, file_url="/uploads/credentials/x/a.pdf"
    )
    await _seed_credential(
        engine,
        advisor_id,
        status=CredentialStatus.rejected,
        file_url="/uploads/credentials/x/b.png",
    )

    resp = await client.get(
        f"{ADVISORS}/{advisor_id}/credentials?status=pending", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert len(data) == 1
    assert data[0]["document_format"] == "PDF"


async def test_per_document_approve_reject(client: AsyncClient, admin_token: str, engine) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    advisor_id = await _seed_user(
        engine, "single-advisor@test.com", "Single Advisor", UserRole.advisor
    )
    credential_id = await _seed_credential(engine, advisor_id, status=CredentialStatus.pending)

    resp = await client.patch(
        f"{ADVISORS}/{advisor_id}/credentials/{credential_id}",
        json={"status": "verified"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "verified"


async def test_bulk_approve_all_pending(client: AsyncClient, admin_token: str, engine) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    advisor_id = await _seed_user(engine, "bulk-approve@test.com", "Bulk Approve", UserRole.advisor)
    for _ in range(3):
        await _seed_credential(engine, advisor_id, status=CredentialStatus.pending)

    resp = await client.post(
        f"{ADVISORS}/{advisor_id}/credentials/bulk-review",
        json={"action": "approve"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert len(data) == 3
    assert all(c["status"] == "verified" for c in data)
    assert all(c["verified_at"] is not None for c in data)


async def test_bulk_reject_all_pending(client: AsyncClient, admin_token: str, engine) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    advisor_id = await _seed_user(engine, "bulk-reject@test.com", "Bulk Reject", UserRole.advisor)
    for _ in range(2):
        await _seed_credential(engine, advisor_id, status=CredentialStatus.pending)

    resp = await client.post(
        f"{ADVISORS}/{advisor_id}/credentials/bulk-review",
        json={"action": "reject"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert len(data) == 2
    assert all(c["status"] == "rejected" for c in data)


async def test_bulk_review_404_when_no_pending_credentials(
    client: AsyncClient, admin_token: str, engine
) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    advisor_id = await _seed_user(engine, "no-pending@test.com", "No Pending", UserRole.advisor)

    resp = await client.post(
        f"{ADVISORS}/{advisor_id}/credentials/bulk-review",
        json={"action": "approve"},
        headers=admin_headers,
    )
    assert resp.status_code == 404


# ── Auth ──────────────────────────────────────────────────────────────────────


async def test_non_admin_403(client: AsyncClient, admin_token: str, advisor_token: str) -> None:
    headers = {"Authorization": f"Bearer {advisor_token}"}
    assert (await client.get(ADVISORS, headers=headers)).status_code == 403
    assert (await client.get(VERIFICATION_QUEUE, headers=headers)).status_code == 403
