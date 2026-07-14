"""AI narrative insight tests — OpenAI is always mocked; the real API is never hit
(conftest force-blanks OPENAI_API_KEY, and key-present tests mutate the cached
Settings singleton + patch the AsyncOpenAI client class)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient

from app.core.config import get_settings
from app.models.assessment import Assessment, AssessmentCategoryScore
from app.services.ai_insight_service import _filter_strengths, _StrengthItem
from tests.test_assessments import _opt, _seed_questions, _seeker_token

ASSESSMENTS = "/api/v1/assessments"

PAYLOAD = {
    # Strengths carry their source category; the service drops any whose
    # deterministic category score is below threshold (all seeded answers score
    # 100, so these survive) and returns plain text to callers.
    "strengths": [
        {"category": "nationality", "text": "Strong passport profile"},
        {"category": "financial", "text": "Stable financial history"},
    ],
    # Leaked "[category]" prefixes must be stripped before persisting.
    "weaknesses": ["[financial] Updated bank statement needed"],
    "missing_requirements": ["Language test certificate"],
    "summary": "Your profile demonstrates strong potential; address the identified gaps.",
}
EXPECTED_STRENGTHS = ["Strong passport profile", "Stable financial history"]
EXPECTED_WEAKNESSES = ["Updated bank statement needed"]


def _mock_completion(content: str) -> SimpleNamespace:
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def _mock_openai_client(content: str) -> MagicMock:
    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=_mock_completion(content))
    return client


async def _complete_assessment(client: AsyncClient, admin_token: str) -> dict:
    """Seed questions, start an assessment, submit a full answer set; return the result."""
    questions = await _seed_questions(client, admin_token)
    token = await _seeker_token(client)
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post(
        ASSESSMENTS,
        json={"destination_country": "GB", "visa_type": "work"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    assessment_id = resp.json()["data"]["id"]

    answers = [
        {"question_id": questions[0]["id"], "option_id": _opt(questions[0], "Yes")},
        {"question_id": questions[1]["id"], "option_id": _opt(questions[1], "Yes")},
        {"question_id": questions[2]["id"], "option_id": _opt(questions[2], "Never")},
    ]
    resp = await client.post(
        f"{ASSESSMENTS}/{assessment_id}/answers", json={"answers": answers}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    result: dict = resp.json()["data"]

    # Round-trip via GET to prove persistence, not just the in-memory response.
    resp = await client.get(f"{ASSESSMENTS}/{assessment_id}", headers=headers)
    assert resp.status_code == 200, resp.text
    fetched: dict = resp.json()["data"]
    assert fetched["strengths"] == result["strengths"]
    assert fetched["weaknesses"] == result["weaknesses"]
    assert fetched["missing_requirements"] == result["missing_requirements"]
    assert fetched["ai_summary"] == result["ai_summary"]
    return result


async def test_insights_generated_and_persisted(
    client: AsyncClient, admin_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "OPENAI_API_KEY", "sk-test")
    with patch(
        "app.services.ai_insight_service.AsyncOpenAI",
        return_value=_mock_openai_client(json.dumps(PAYLOAD)),
    ):
        result = await _complete_assessment(client, admin_token)

    assert result["strengths"] == EXPECTED_STRENGTHS
    assert result["weaknesses"] == EXPECTED_WEAKNESSES
    assert result["missing_requirements"] == PAYLOAD["missing_requirements"]
    assert result["ai_summary"] == PAYLOAD["summary"]
    # Deterministic result is untouched by the AI layer.
    assert result["score"] == 100.0
    assert result["tier"] == "highly_eligible"
    assert result["improvement_tips"] == []


async def test_openai_failure_never_blocks_completion(
    client: AsyncClient, admin_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "OPENAI_API_KEY", "sk-test")
    failing_client = MagicMock()
    failing_client.chat.completions.create = AsyncMock(side_effect=RuntimeError("boom"))
    with patch("app.services.ai_insight_service.AsyncOpenAI", return_value=failing_client):
        result = await _complete_assessment(client, admin_token)

    assert result["status"] == "completed"
    assert result["score"] == 100.0
    assert result["strengths"] == []
    assert result["weaknesses"] == []
    assert result["missing_requirements"] == []
    assert result["ai_summary"] is None


async def test_no_api_key_skips_openai_entirely(client: AsyncClient, admin_token: str) -> None:
    # conftest blanks OPENAI_API_KEY — the client class must never be constructed.
    with patch("app.services.ai_insight_service.AsyncOpenAI") as client_cls:
        result = await _complete_assessment(client, admin_token)

    client_cls.assert_not_called()
    assert result["status"] == "completed"
    assert result["strengths"] == []
    assert result["ai_summary"] is None


def test_strength_filter_drops_weak_and_unknown_categories() -> None:
    import uuid

    assessment = Assessment(user_id=uuid.uuid4(), destination_country="GB", visa_type="work")
    assessment.id = uuid.uuid4()
    assessment.category_scores = [
        AssessmentCategoryScore(assessment_id=assessment.id, category="financial", score=100.0),
        AssessmentCategoryScore(assessment_id=assessment.id, category="education", score=40.0),
    ]
    items = [
        _StrengthItem(category="financial", text="Ample funds"),
        # Models sometimes echo the prompt's decoration around the category name.
        _StrengthItem(category="[financial] (category score 100/100)", text="Decorated tag"),
        _StrengthItem(category="education", text="Below the 70 threshold"),
        _StrengthItem(category="astrology", text="Unknown category"),
    ]

    assert _filter_strengths(items, assessment) == ["Ample funds", "Decorated tag"]


async def test_malformed_response_falls_back(
    client: AsyncClient, admin_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "OPENAI_API_KEY", "sk-test")
    with patch(
        "app.services.ai_insight_service.AsyncOpenAI",
        return_value=_mock_openai_client("not json at all"),
    ):
        result = await _complete_assessment(client, admin_token)

    assert result["status"] == "completed"
    assert result["strengths"] == []
    assert result["ai_summary"] is None
