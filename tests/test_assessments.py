"""AI eligibility assessment engine tests (epic #6, PRD §3.4)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.advisor_lead import AdvisorLead, AdvisorLeadStatus
from app.models.user import User, UserRole

ASSESSMENTS = "/api/v1/assessments"
ADMIN_QUESTIONS = "/api/v1/admin/assessment-questions"
LOGIN = "/api/v1/auth/login"
REGISTER = "/api/v1/auth/register"
SEEKER_PASSWORD = "CustPass123!"


async def _mark_email_verified(engine, email: str) -> None:
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        user.email_verified_at = datetime.now(UTC)
        session.add(user)
        await session.commit()


async def _seeker_token(client: AsyncClient, email: str = "cust@test.com") -> str:
    resp = await client.post(
        REGISTER, json={"email": email, "password": SEEKER_PASSWORD, "full_name": "Cust"}
    )
    assert resp.status_code == 201, resp.text
    await _mark_email_verified(client._test_engine, email)  # type: ignore[attr-defined]
    resp = await client.post(LOGIN, data={"username": email, "password": SEEKER_PASSWORD})
    assert resp.status_code == 200, resp.text
    return str(resp.json()["access_token"])


def _q(text: str, category: str, weight: float, options: list[dict], **kwargs) -> dict:
    return {
        "text": text,
        "category": category,
        "weight": weight,
        "options": options,
        **kwargs,
    }


async def _seed_questions(client: AsyncClient, admin_token: str) -> list[dict]:
    """Create a 3-question global questionnaire via the admin API. Returns questions."""
    headers = {"Authorization": f"Bearer {admin_token}"}
    payloads = [
        _q(
            "Valid passport?",
            "nationality",
            2.0,
            [
                {"text": "Yes", "score": 100},
                {"text": "No", "score": 0, "improvement_tip": "Get a passport."},
            ],
        ),
        _q(
            "Sufficient funds?",
            "financial",
            1.0,
            [
                {"text": "Yes", "score": 100},
                {"text": "Borderline", "score": 50, "improvement_tip": "Save more."},
                {"text": "No", "score": 0, "improvement_tip": "Save much more."},
            ],
        ),
        _q(
            "Visa refusals?",
            "visa_refusals",
            1.0,
            [
                {"text": "Never", "score": 100},
                {"text": "Once", "score": 40, "improvement_tip": "Address the refusal."},
            ],
        ),
    ]
    created = []
    for p in payloads:
        resp = await client.post(ADMIN_QUESTIONS, json=p, headers=headers)
        assert resp.status_code == 201, resp.text
        created.append(resp.json()["data"])
    return created


def _opt(question: dict, text: str) -> str:
    return next(o["id"] for o in question["options"] if o["text"] == text)


async def test_admin_question_crud(client: AsyncClient, admin_token: str) -> None:
    headers = {"Authorization": f"Bearer {admin_token}"}
    questions = await _seed_questions(client, admin_token)
    qid = questions[0]["id"]

    # List
    resp = await client.get(ADMIN_QUESTIONS, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["meta"]["pagination"]["total"] == 3

    # Update weight + deactivate
    resp = await client.patch(
        f"{ADMIN_QUESTIONS}/{qid}", json={"weight": 3.0, "is_active": False}, headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["weight"] == 3.0
    assert resp.json()["data"]["is_active"] is False

    # Delete
    resp = await client.delete(f"{ADMIN_QUESTIONS}/{questions[2]['id']}", headers=headers)
    assert resp.status_code == 204
    resp = await client.get(ADMIN_QUESTIONS, headers=headers)
    assert resp.json()["meta"]["pagination"]["total"] == 2


async def test_question_crud_requires_admin(client: AsyncClient) -> None:
    token = await _seeker_token(client)
    resp = await client.post(
        ADMIN_QUESTIONS,
        json=_q("X?", "purpose", 1.0, [{"text": "A", "score": 100}, {"text": "B", "score": 0}]),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


async def test_bulk_status_updates_every_page(client: AsyncClient, admin_token: str) -> None:
    headers = {"Authorization": f"Bearer {admin_token}"}
    options = [{"text": "A", "score": 100}, {"text": "B", "score": 0}]
    for i in range(12):
        resp = await client.post(
            ADMIN_QUESTIONS,
            json=_q(f"Bulk Q{i}?", "purpose", 1.0, options, is_active=(i % 2 == 0)),
            headers=headers,
        )
        assert resp.status_code == 201, resp.text

    resp = await client.post(
        f"{ADMIN_QUESTIONS}/bulk-status", json={"is_active": False}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()["data"]
    assert payload["is_active"] is False
    assert payload["updated"] == 12

    listed: list[dict] = []
    for page in (1, 2):
        resp = await client.get(
            f"{ADMIN_QUESTIONS}?page={page}&page_size=10", headers=headers
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        listed.extend(body["data"])
        assert body["meta"]["pagination"]["total"] == 12
        assert all(row["is_active"] is False for row in body["data"])

    assert len(listed) == 12
    assert all(row["is_active"] is False for row in listed)

    token = await _seeker_token(client)
    resp = await client.post(
        f"{ADMIN_QUESTIONS}/bulk-status",
        json={"is_active": True},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403

    resp = await client.post(f"{ADMIN_QUESTIONS}/bulk-status", json={}, headers=headers)
    assert resp.status_code == 422


async def test_seeker_lists_applicable_questions(client: AsyncClient, admin_token: str) -> None:
    await _seed_questions(client, admin_token)
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    # Country-scoped question for CA only — must not appear for GB.
    resp = await client.post(
        ADMIN_QUESTIONS,
        json=_q(
            "CA only?",
            "purpose",
            1.0,
            [{"text": "A", "score": 100}, {"text": "B", "score": 0}],
            country_code="CA",
        ),
        headers=admin_headers,
    )
    assert resp.status_code == 201

    token = await _seeker_token(client)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.get(f"{ASSESSMENTS}/questions?country=GB&visa_type=work", headers=headers)
    assert resp.status_code == 200
    texts = [q["text"] for q in resp.json()["data"]]
    assert "CA only?" not in texts
    assert len(texts) == 3
    # Options must not leak scores to seekers.
    assert "score" not in resp.json()["data"][0]["options"][0]

    resp = await client.get(f"{ASSESSMENTS}/questions?country=CA&visa_type=work", headers=headers)
    assert len(resp.json()["data"]) == 4


async def test_full_assessment_flow_highly_eligible(client: AsyncClient, admin_token: str) -> None:
    questions = await _seed_questions(client, admin_token)
    token = await _seeker_token(client)
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post(
        ASSESSMENTS,
        json={"destination_country": "gb", "visa_type": "Work"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()["data"]
    assessment_id = body["id"]
    assert body["status"] == "in_progress"
    assert body["destination_country"] == "GB"
    assert body["visa_type"] == "work"

    answers = [
        {"question_id": questions[0]["id"], "option_id": _opt(questions[0], "Yes")},
        {"question_id": questions[1]["id"], "option_id": _opt(questions[1], "Yes")},
        {"question_id": questions[2]["id"], "option_id": _opt(questions[2], "Never")},
    ]
    resp = await client.post(
        f"{ASSESSMENTS}/{assessment_id}/answers", json={"answers": answers}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()["data"]
    assert result["status"] == "completed"
    assert result["score"] == 100.0
    assert result["tier"] == "highly_eligible"
    assert result["confidence"] == 1.0
    assert result["improvement_tips"] == []
    categories = {c["category"]: c["score"] for c in result["category_scores"]}
    assert categories == {"nationality": 100.0, "financial": 100.0, "visa_refusals": 100.0}


async def test_weighted_scoring_tiers_and_tips(client: AsyncClient, admin_token: str) -> None:
    questions = await _seed_questions(client, admin_token)
    token = await _seeker_token(client)
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post(
        ASSESSMENTS,
        json={"destination_country": "GB", "visa_type": "work"},
        headers=headers,
    )
    assessment_id = resp.json()["data"]["id"]

    # passport No (0 × w2), funds Borderline (50 × w1), refusals Once (40 × w1)
    # → (0 + 50 + 40) / 4 = 22.5 → low_eligibility
    answers = [
        {"question_id": questions[0]["id"], "option_id": _opt(questions[0], "No")},
        {"question_id": questions[1]["id"], "option_id": _opt(questions[1], "Borderline")},
        {"question_id": questions[2]["id"], "option_id": _opt(questions[2], "Once")},
    ]
    resp = await client.post(
        f"{ASSESSMENTS}/{assessment_id}/answers", json={"answers": answers}, headers=headers
    )
    result = resp.json()["data"]
    assert result["score"] == 22.5
    assert result["tier"] == "low_eligibility"
    assert sorted(result["improvement_tips"]) == [
        "Address the refusal.",
        "Get a passport.",
        "Save more.",
    ]

    # Cannot resubmit a completed assessment.
    resp = await client.post(
        f"{ASSESSMENTS}/{assessment_id}/answers", json={"answers": answers}, headers=headers
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "assessment_completed"


async def test_partial_answers_lower_confidence(client: AsyncClient, admin_token: str) -> None:
    questions = await _seed_questions(client, admin_token)
    token = await _seeker_token(client)
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post(
        ASSESSMENTS, json={"destination_country": "GB", "visa_type": "work"}, headers=headers
    )
    assessment_id = resp.json()["data"]["id"]

    answers = [
        {"question_id": questions[0]["id"], "option_id": _opt(questions[0], "Yes")},
        {"question_id": questions[1]["id"], "option_id": _opt(questions[1], "Yes")},
    ]
    resp = await client.post(
        f"{ASSESSMENTS}/{assessment_id}/answers", json={"answers": answers}, headers=headers
    )
    result = resp.json()["data"]
    assert result["confidence"] == 0.67  # 2 of 3 applicable questions answered
    # Unanswered questions contribute 0 so skipping cannot inflate the score:
    # (100×2 + 100×1 + 0×1) / 4 = 75.
    assert result["score"] == 75.0


async def test_adaptive_question_skipped_unless_triggered(
    client: AsyncClient, admin_token: str
) -> None:
    questions = await _seed_questions(client, admin_token)
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    # Follow-up only applicable when refusals answer was "Once".
    trigger_option = _opt(questions[2], "Once")
    resp = await client.post(
        ADMIN_QUESTIONS,
        json=_q(
            "Was the refusal overturned?",
            "visa_refusals",
            1.0,
            [{"text": "Yes", "score": 80}, {"text": "No", "score": 20}],
            depends_on_option_id=trigger_option,
        ),
        headers=admin_headers,
    )
    assert resp.status_code == 201

    token = await _seeker_token(client)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post(
        ASSESSMENTS, json={"destination_country": "GB", "visa_type": "work"}, headers=headers
    )
    assessment_id = resp.json()["data"]["id"]

    # "Never" selected → follow-up not applicable → full confidence with 3 answers.
    answers = [
        {"question_id": questions[0]["id"], "option_id": _opt(questions[0], "Yes")},
        {"question_id": questions[1]["id"], "option_id": _opt(questions[1], "Yes")},
        {"question_id": questions[2]["id"], "option_id": _opt(questions[2], "Never")},
    ]
    resp = await client.post(
        f"{ASSESSMENTS}/{assessment_id}/answers", json={"answers": answers}, headers=headers
    )
    assert resp.json()["data"]["confidence"] == 1.0


async def test_matched_advisors_on_result(client: AsyncClient, engine, admin_token: str) -> None:
    from tests.test_advisor_search import _make_advisor

    questions = await _seed_questions(client, admin_token)
    # GB/work specialist should outrank the CA specialist for a GB work assessment.
    gb_id, _ = await _make_advisor(
        client,
        engine,
        "gb-advisor@test.com",
        "GB Expert",
        {
            "years_of_experience": 10,
            "visa_specializations": ["work"],
            "country_expertise": ["GB"],
        },
    )
    await _make_advisor(
        client,
        engine,
        "ca-advisor@test.com",
        "CA Expert",
        {
            "years_of_experience": 10,
            "visa_specializations": ["family"],
            "country_expertise": ["CA"],
        },
    )

    token = await _seeker_token(client)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post(
        ASSESSMENTS, json={"destination_country": "GB", "visa_type": "work"}, headers=headers
    )
    assessment_id = resp.json()["data"]["id"]
    answers = [
        {"question_id": questions[0]["id"], "option_id": _opt(questions[0], "Yes")},
    ]
    resp = await client.post(
        f"{ASSESSMENTS}/{assessment_id}/answers", json={"answers": answers}, headers=headers
    )
    matches = resp.json()["data"]["matched_advisors"]
    assert matches, "expected at least one matched advisor"
    assert matches[0]["user_id"] == gb_id
    assert matches[0]["match_score"] == 77.5  # 40 country + 30 visa + 7.5 experience (no rating)


async def test_history_reads_persisted_leads_not_live_matcher(
    client: AsyncClient, engine, admin_token: str
) -> None:
    """Refreshing history / opening matches must not call OpenAI or the live matcher."""
    await _seed_questions(client, admin_token)
    token = await _seeker_token(client, "history-seeker@test.com")
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post(
        ASSESSMENTS, json={"destination_country": "GB", "visa_type": "work"}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    assessment_id = uuid.UUID(resp.json()["data"]["id"])

    questions = await client.get(
        f"{ASSESSMENTS}/questions?country=GB&visa_type=work", headers=headers
    )
    q0 = questions.json()["data"][0]
    opt0 = q0["options"][0]["id"]
    resp = await client.post(
        f"{ASSESSMENTS}/{assessment_id}/answers",
        json={"answers": [{"question_id": q0["id"], "option_id": opt0}]},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        seeker = (
            await session.execute(select(User).where(User.email == "history-seeker@test.com"))
        ).scalar_one()
        advisor = User(
            email="history-advisor@test.com",
            full_name="History Advisor",
            hashed_password="x",
            role=UserRole.advisor,
            is_active=True,
        )
        session.add(advisor)
        await session.flush()
        session.add(
            AdvisorLead(
                seeker_id=seeker.id,
                advisor_id=advisor.id,
                assessment_id=assessment_id,
                match_score=88.0,
                match_reasons="Persisted snapshot",
                status=AdvisorLeadStatus.new,
            )
        )
        await session.commit()
        advisor_id = advisor.id

    with patch(
        "app.services.advisor_matching_service.match",
        new_callable=AsyncMock,
        side_effect=AssertionError("history must not re-run the matcher"),
    ):
        hist = await client.get(
            f"{ASSESSMENTS}?page=1&page_size=10&status=completed&sort=newest",
            headers=headers,
        )
        assert hist.status_code == 200, hist.text
        rows = hist.json()["data"]
        assert len(rows) == 1
        assert rows[0]["matched_advisors_count"] >= 1

        panel = await client.get(
            f"{ASSESSMENTS}/{assessment_id}/matched-advisors?page=1&page_size=10",
            headers=headers,
        )
        assert panel.status_code == 200, panel.text
        by_id = {row["user_id"]: row for row in panel.json()["data"]}
        assert str(advisor_id) in by_id
        assert by_id[str(advisor_id)]["match_score"] == 88.0
        assert by_id[str(advisor_id)]["match_reasons"] == "Persisted snapshot"
        assert panel.json()["meta"]["pagination"]["total"] == rows[0]["matched_advisors_count"]

        detail = await client.get(f"{ASSESSMENTS}/{assessment_id}", headers=headers)
        assert detail.status_code == 200, detail.text
        detail_ids = {row["user_id"] for row in detail.json()["data"]["matched_advisors"]}
        assert str(advisor_id) in detail_ids


async def test_assessment_history(client: AsyncClient, admin_token: str) -> None:
    await _seed_questions(client, admin_token)
    token = await _seeker_token(client)
    headers = {"Authorization": f"Bearer {token}"}

    for country in ("GB", "CA"):
        await client.post(
            ASSESSMENTS, json={"destination_country": country, "visa_type": "work"}, headers=headers
        )

    resp = await client.get(ASSESSMENTS, headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["meta"]["pagination"]["total"] == 2
    assert {a["destination_country"] for a in body["data"]} == {"GB", "CA"}


async def test_assessment_isolated_per_user(client: AsyncClient, admin_token: str) -> None:
    await _seed_questions(client, admin_token)
    token_a = await _seeker_token(client, "a@test.com")
    token_b = await _seeker_token(client, "b@test.com")

    resp = await client.post(
        ASSESSMENTS,
        json={"destination_country": "GB", "visa_type": "work"},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assessment_id = resp.json()["data"]["id"]

    resp = await client.get(
        f"{ASSESSMENTS}/{assessment_id}", headers={"Authorization": f"Bearer {token_b}"}
    )
    assert resp.status_code == 404


async def test_advisor_cannot_start_assessment(client: AsyncClient, advisor_token: str) -> None:
    resp = await client.post(
        ASSESSMENTS,
        json={"destination_country": "GB", "visa_type": "work"},
        headers={"Authorization": f"Bearer {advisor_token}"},
    )
    assert resp.status_code == 403


async def test_invalid_answers_rejected(client: AsyncClient, admin_token: str) -> None:
    questions = await _seed_questions(client, admin_token)
    token = await _seeker_token(client)
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post(
        ASSESSMENTS, json={"destination_country": "GB", "visa_type": "work"}, headers=headers
    )
    assessment_id = resp.json()["data"]["id"]

    # Option from another question.
    resp = await client.post(
        f"{ASSESSMENTS}/{assessment_id}/answers",
        json={
            "answers": [{"question_id": questions[0]["id"], "option_id": _opt(questions[1], "Yes")}]
        },
        headers=headers,
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_answer"

    # Unknown question id.
    resp = await client.post(
        f"{ASSESSMENTS}/{assessment_id}/answers",
        json={"answers": [{"question_id": str(uuid.uuid4()), "option_id": str(uuid.uuid4())}]},
        headers=headers,
    )
    assert resp.status_code == 400
