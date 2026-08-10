"""Seeker document portfolio + advisor/admin review tests (PRD §3.8)."""

from __future__ import annotations

import io
import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.models.seeker_document import DocumentCategory, SeekerDocument, SeekerDocumentStatus
from app.models.user import User, UserRole
from app.services import seeker_document_service
from tests.test_analytics import _seed_user
from tests.test_bookings import _bookable_advisor, _seeker, _slot_iso

BOOKINGS = "/api/v1/bookings"
DOCUMENTS = "/api/v1/users/me/documents"


async def _upload_document(
    client: AsyncClient,
    headers: dict,
    *,
    category: str,
    document_name: str,
    file_name: str = "doc.pdf",
) -> dict:
    upload = await client.post(
        "/api/v1/uploads",
        headers=headers,
        files={"file": (file_name, io.BytesIO(b"%PDF-1.4 fake"), "application/pdf")},
        data={"category": "seeker_document"},
    )
    assert upload.status_code == 201, upload.text
    file_info = upload.json()["data"]

    resp = await client.post(
        DOCUMENTS,
        json={
            "file_key": file_info["file_key"],
            "file_name": file_name,
            "file_size_bytes": file_info["file_size_bytes"],
            "content_type": "application/pdf",
            "category": category,
            "document_name": document_name,
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


async def _upload_seeker_document(client: AsyncClient, headers: dict) -> dict:
    return await _upload_document(
        client,
        headers,
        category="passport",
        document_name="Passport Copy",
        file_name="passport.pdf",
    )


def _checklist_item(summary, category: DocumentCategory):
    for item in summary.checklist:
        if item.category == category:
            return item
    raise AssertionError(f"checklist missing category {category!r}")


async def _seed_document(
    engine,
    seeker_id: uuid.UUID,
    *,
    category: DocumentCategory,
    status: SeekerDocumentStatus = SeekerDocumentStatus.under_review,
    document_name: str = "doc.pdf",
) -> uuid.UUID:
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        doc = SeekerDocument(
            seeker_id=seeker_id,
            category=category,
            document_name=document_name,
            file_url=f"/uploads/seeker_document/{seeker_id}/{document_name}",
            content_type="application/pdf",
            status=status,
            created_by=seeker_id,
        )
        session.add(doc)
        await session.commit()
        await session.refresh(doc)
        return doc.id


async def _portfolio_summary(engine, seeker_id: uuid.UUID):
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        return await seeker_document_service.portfolio_summary(session, seeker_id)


async def _booked_pair(client: AsyncClient, engine) -> tuple[str, dict, dict]:
    """Returns (advisor_id, advisor_headers, seeker_headers) with an existing booking."""
    advisor_id, advisor_headers, day = await _bookable_advisor(client, engine)
    _, seeker_headers = await _seeker(client)
    resp = await client.post(
        BOOKINGS,
        json={
            "advisor_id": advisor_id,
            "service_type": "immigration_specialist",
            "scheduled_start": _slot_iso(day, 10),
        },
        headers=seeker_headers,
    )
    assert resp.status_code == 201, resp.text
    return advisor_id, advisor_headers, seeker_headers


async def _user_id(client: AsyncClient, headers: dict) -> str:
    resp = await client.get("/api/v1/users/me", headers=headers)
    return str(resp.json()["data"]["id"])


# ── Seeker upload / list / comment ───────────────────────────────────────────


async def test_seeker_can_upload_list_and_comment(client: AsyncClient) -> None:
    _, seeker_headers = await _seeker(client)
    document = await _upload_seeker_document(client, seeker_headers)
    assert document["category"] == "passport"
    assert document["status"] == "under_review"

    resp = await client.get(DOCUMENTS, headers=seeker_headers)
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["data"]) == 1

    resp = await client.post(
        f"{DOCUMENTS}/{document['id']}/comments",
        json={"body": "Uploaded my passport, please review."},
        headers=seeker_headers,
    )
    assert resp.status_code == 201, resp.text

    resp = await client.get(f"{DOCUMENTS}/{document['id']}/comments", headers=seeker_headers)
    assert resp.status_code == 200
    assert len(resp.json()["data"]) == 1


async def test_seeker_cannot_comment_on_another_seekers_document(client: AsyncClient) -> None:
    _, seeker_headers = await _seeker(client, "owner@test.com")
    document = await _upload_seeker_document(client, seeker_headers)

    _, other_headers = await _seeker(client, "other-seeker@test.com")
    resp = await client.post(
        f"{DOCUMENTS}/{document['id']}/comments", json={"body": "hi"}, headers=other_headers
    )
    assert resp.status_code == 404


# ── Documents summary checklist ───────────────────────────────────────────────


async def test_portfolio_summary_includes_other_category_as_missing(engine) -> None:
    seeker_id = await _seed_user(engine, "summary-missing@test.com", "Seeker", UserRole.seeker)
    await _seed_document(engine, seeker_id, category=DocumentCategory.passport)

    summary = await _portfolio_summary(engine, seeker_id)

    assert len(summary.checklist) == 5
    other = _checklist_item(summary, DocumentCategory.other)
    assert other.label == "Other"
    assert other.status == "missing"
    assert other.document_id is None
    assert summary.missing == 4
    assert summary.progress_percent == 0


async def test_portfolio_summary_other_under_review(engine) -> None:
    seeker_id = await _seed_user(engine, "summary-other@test.com", "Seeker", UserRole.seeker)
    other_id = await _seed_document(
        engine,
        seeker_id,
        category=DocumentCategory.other,
        document_name="Misc Certificate",
    )

    summary = await _portfolio_summary(engine, seeker_id)
    other = _checklist_item(summary, DocumentCategory.other)
    assert other.status == "under_review"
    assert other.document_id == other_id


async def test_portfolio_summary_other_rollup_prefers_under_review_over_rejected(
    engine,
) -> None:
    seeker_id = await _seed_user(engine, "summary-rollup@test.com", "Seeker", UserRole.seeker)
    await _seed_document(
        engine,
        seeker_id,
        category=DocumentCategory.other,
        status=SeekerDocumentStatus.rejected,
        document_name="Rejected Misc",
    )
    reviewing_id = await _seed_document(
        engine,
        seeker_id,
        category=DocumentCategory.other,
        document_name="Replacement Misc",
    )

    summary = await _portfolio_summary(engine, seeker_id)
    other = _checklist_item(summary, DocumentCategory.other)
    assert other.status == "under_review"
    assert other.document_id == reviewing_id


async def test_portfolio_summary_progress_percent_counts_five_categories(engine) -> None:
    seeker_id = await _seed_user(engine, "summary-progress@test.com", "Seeker", UserRole.seeker)
    await _seed_document(
        engine,
        seeker_id,
        category=DocumentCategory.passport,
        status=SeekerDocumentStatus.under_review,
    )

    summary = await _portfolio_summary(engine, seeker_id)
    assert summary.progress_percent == 0

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        doc = (
            await session.execute(
                seeker_document_service.list_by_seeker_stmt(
                    seeker_id, category=DocumentCategory.passport
                )
            )
        ).scalar_one()
        doc.status = SeekerDocumentStatus.approved
        session.add(doc)
        await session.commit()

    summary = await _portfolio_summary(engine, seeker_id)
    assert summary.progress_percent == 20


# ── Comment metadata ──────────────────────────────────────────────────────────


async def test_build_comment_read_includes_author_role(engine) -> None:
    seeker_id = await _seed_user(engine, "comment-role-seeker@test.com", "Seeker", UserRole.seeker)
    advisor_id = await _seed_user(
        engine, "comment-role-advisor@test.com", "Advisor", UserRole.advisor
    )
    doc_id = await _seed_document(engine, seeker_id, category=DocumentCategory.passport)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        document = await session.get(SeekerDocument, doc_id)
        assert document is not None
        seeker_comment = await seeker_document_service.add_comment(
            session, document, seeker_id, "Seeker note"
        )
        advisor_comment = await seeker_document_service.add_comment(
            session, document, advisor_id, "Advisor note"
        )
        await session.commit()
        seeker = await session.get(User, seeker_id)
        advisor = await session.get(User, advisor_id)
        seeker_read = seeker_document_service.build_comment_read(seeker_comment, seeker)
        advisor_read = seeker_document_service.build_comment_read(advisor_comment, advisor)

    assert seeker_read.author_role == "seeker"
    assert advisor_read.author_role == "advisor"


async def test_build_reads_includes_comments_count(engine) -> None:
    settings = get_settings()
    seeker_id = await _seed_user(engine, "comment-count@test.com", "Seeker", UserRole.seeker)
    advisor_id = await _seed_user(engine, "comment-count-adv@test.com", "Advisor", UserRole.advisor)
    doc_id = await _seed_document(engine, seeker_id, category=DocumentCategory.passport)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        document = await session.get(SeekerDocument, doc_id)
        assert document is not None
        await seeker_document_service.add_comment(session, document, seeker_id, "one")
        await seeker_document_service.add_comment(session, document, advisor_id, "two")
        await session.commit()

        reads = await seeker_document_service.build_reads(session, [document], settings)
        assert len(reads) == 1
        assert reads[0].comments_count == 2

        counts = await seeker_document_service.comment_counts_for_documents(session, [doc_id])
        assert counts[doc_id] == 2


async def test_list_comments_oldest_first(engine) -> None:
    seeker_id = await _seed_user(engine, "comment-order@test.com", "Seeker", UserRole.seeker)
    doc_id = await _seed_document(engine, seeker_id, category=DocumentCategory.passport)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        document = await session.get(SeekerDocument, doc_id)
        assert document is not None
        first = await seeker_document_service.add_comment(session, document, seeker_id, "first")
        second = await seeker_document_service.add_comment(session, document, seeker_id, "second")
        await session.commit()

        stmt = seeker_document_service.list_comments_stmt(doc_id)
        comments = list((await session.execute(stmt)).scalars().all())

    assert [c.id for c in comments] == [first.id, second.id]
    assert [c.body for c in comments] == ["first", "second"]


# ── Advisor review ────────────────────────────────────────────────────────────


async def test_advisor_with_relationship_can_review_and_comment(
    client: AsyncClient, engine
) -> None:
    advisor_id, advisor_headers, seeker_headers = await _booked_pair(client, engine)
    seeker_id = await _user_id(client, seeker_headers)
    document = await _upload_seeker_document(client, seeker_headers)

    resp = await client.get(
        f"/api/v1/advisors/me/clients/{seeker_id}/documents", headers=advisor_headers
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["data"]) == 1

    resp = await client.patch(
        f"/api/v1/advisors/me/clients/{seeker_id}/documents/{document['id']}",
        json={"status": "approved"},
        headers=advisor_headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status"] == "approved"
    assert data["reviewed_by"] == advisor_id

    resp = await client.post(
        f"/api/v1/advisors/me/clients/{seeker_id}/documents/{document['id']}/comments",
        json={"body": "Looks good, thanks!"},
        headers=advisor_headers,
    )
    assert resp.status_code == 201, resp.text

    resp = await client.get(
        f"/api/v1/advisors/me/clients/{seeker_id}/documents/{document['id']}/comments",
        headers=advisor_headers,
    )
    assert resp.status_code == 200
    assert len(resp.json()["data"]) == 1


async def test_advisor_without_relationship_gets_404(client: AsyncClient, engine) -> None:
    _, _, seeker_headers = await _booked_pair(client, engine)
    seeker_id = await _user_id(client, seeker_headers)
    await _upload_seeker_document(client, seeker_headers)

    # A second advisor with no booking relationship to this seeker.
    _, unrelated_advisor_headers, _ = await _bookable_advisor(client, engine, "unrelated@test.com")

    resp = await client.get(
        f"/api/v1/advisors/me/clients/{seeker_id}/documents", headers=unrelated_advisor_headers
    )
    assert resp.status_code == 404


async def test_seeker_forbidden_from_advisor_document_endpoints(
    client: AsyncClient, engine
) -> None:
    _, _, seeker_headers = await _booked_pair(client, engine)
    seeker_id = await _user_id(client, seeker_headers)

    resp = await client.get(
        f"/api/v1/advisors/me/clients/{seeker_id}/documents", headers=seeker_headers
    )
    assert resp.status_code == 403


# ── Customer-documents aggregate list ─────────────────────────────────────────


CUSTOMER_DOCS = "/api/v1/advisors/me/customer-documents"


async def test_customer_documents_all_approved_is_completed(client: AsyncClient, engine) -> None:
    _, advisor_headers, seeker_headers = await _booked_pair(client, engine)
    seeker_id = await _user_id(client, seeker_headers)
    document = await _upload_seeker_document(client, seeker_headers)

    resp = await client.patch(
        f"/api/v1/advisors/me/clients/{seeker_id}/documents/{document['id']}",
        json={"status": "approved"},
        headers=advisor_headers,
    )
    assert resp.status_code == 200, resp.text

    resp = await client.get(CUSTOMER_DOCS, headers=advisor_headers)
    assert resp.status_code == 200, resp.text
    rows = resp.json()["data"]
    assert len(rows) == 1
    assert rows[0]["documents_status"] == "completed"
    assert rows[0]["documents_count"] == 1

    resp = await client.get(f"{CUSTOMER_DOCS}?documents_status=completed", headers=advisor_headers)
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["data"]) == 1

    resp = await client.get(f"{CUSTOMER_DOCS}?documents_status=rejected", headers=advisor_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == []


async def test_customer_documents_all_rejected_is_rejected(client: AsyncClient, engine) -> None:
    _, advisor_headers, seeker_headers = await _booked_pair(client, engine)
    seeker_id = await _user_id(client, seeker_headers)
    document = await _upload_seeker_document(client, seeker_headers)

    resp = await client.patch(
        f"/api/v1/advisors/me/clients/{seeker_id}/documents/{document['id']}",
        json={"status": "rejected"},
        headers=advisor_headers,
    )
    assert resp.status_code == 200, resp.text

    resp = await client.get(CUSTOMER_DOCS, headers=advisor_headers)
    assert resp.status_code == 200, resp.text
    rows = resp.json()["data"]
    assert len(rows) == 1
    assert rows[0]["documents_status"] == "rejected"
    assert rows[0]["documents_count"] == 1

    resp = await client.get(f"{CUSTOMER_DOCS}?documents_status=rejected", headers=advisor_headers)
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["data"]) == 1

    resp = await client.get(f"{CUSTOMER_DOCS}?documents_status=completed", headers=advisor_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == []

    resp = await client.get(f"{CUSTOMER_DOCS}?documents_status=pending", headers=advisor_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == []


# ── Admin access ──────────────────────────────────────────────────────────────


async def test_admin_can_review_any_seeker_document(
    client: AsyncClient, engine, admin_token: str
) -> None:
    _, seeker_headers = await _seeker(client)
    seeker_id = await _user_id(client, seeker_headers)
    document = await _upload_seeker_document(client, seeker_headers)

    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    resp = await client.get(f"/api/v1/admin/seekers/{seeker_id}/documents", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["data"]) == 1

    resp = await client.patch(
        f"/api/v1/admin/seekers/{seeker_id}/documents/{document['id']}",
        json={"status": "rejected"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "rejected"
