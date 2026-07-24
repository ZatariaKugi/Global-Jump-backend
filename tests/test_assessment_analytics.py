"""AI Analytics endpoint tests (PRD §3.4 AI Engine Management)."""

from __future__ import annotations

from datetime import UTC, datetime

from httpx import AsyncClient

from tests.test_assessments import ASSESSMENTS, _opt, _seed_questions, _seeker_token

ANALYTICS = "/api/v1/admin/assessment-analytics"


async def _complete_assessment(
    client: AsyncClient, questions: list[dict], headers: dict, choice: str
) -> dict:
    """Answer question 0 with ``choice`` and the rest with their top option."""
    resp = await client.post(
        ASSESSMENTS, json={"destination_country": "GB", "visa_type": "work"}, headers=headers
    )
    assessment_id = resp.json()["data"]["id"]
    answers = [
        {"question_id": questions[0]["id"], "option_id": _opt(questions[0], choice)},
        {"question_id": questions[1]["id"], "option_id": _opt(questions[1], "Yes")},
        {"question_id": questions[2]["id"], "option_id": _opt(questions[2], "Never")},
    ]
    resp = await client.post(
        f"{ASSESSMENTS}/{assessment_id}/answers", json={"answers": answers}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


async def test_analytics_volume_and_shape(client: AsyncClient, admin_token: str) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    questions = await _seed_questions(client, admin_token)

    token1 = await _seeker_token(client, "s1@test.com")
    token2 = await _seeker_token(client, "s2@test.com")
    await _complete_assessment(client, questions, {"Authorization": f"Bearer {token1}"}, "Yes")
    await _complete_assessment(client, questions, {"Authorization": f"Bearer {token2}"}, "Yes")

    resp = await client.get(f"{ANALYTICS}?country=GB&visa_type=work", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert set(data.keys()) == {
        "window_days",
        "pass_rate",
        "fail_rate",
        "assessment_volume",
        "drop_off_points",
    }
    assert sum(p["value"] for p in data["assessment_volume"]) == 2
    assert data["assessment_volume"][0]["month"] == datetime.now(UTC).strftime("%b")


async def test_analytics_pass_and_fail_rate(client: AsyncClient, admin_token: str) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    questions = await _seed_questions(client, admin_token)

    # Passing: Yes/Yes/Never -> score 100 -> highly_eligible.
    pass_token = await _seeker_token(client, "pass@test.com")
    await _complete_assessment(client, questions, {"Authorization": f"Bearer {pass_token}"}, "Yes")

    # Failing: No/Yes/Never -> (0*2 + 100 + 100)/4 = 50 -> borderline (fail bucket).
    fail_token = await _seeker_token(client, "fail@test.com")
    await _complete_assessment(client, questions, {"Authorization": f"Bearer {fail_token}"}, "No")

    resp = await client.get(f"{ANALYTICS}?country=GB&visa_type=work", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["pass_rate"] == 50.0
    assert data["fail_rate"] == 50.0


async def test_analytics_drop_off(client: AsyncClient, admin_token: str) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    await _seed_questions(client, admin_token)

    # Started but never answered -> drop-off at Q1 (100% of started).
    token = await _seeker_token(client, "dropoff@test.com")
    resp = await client.post(
        ASSESSMENTS,
        json={"destination_country": "GB", "visa_type": "work"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text

    resp = await client.get(f"{ANALYTICS}?country=GB&visa_type=work", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["drop_off_points"]
    assert data["drop_off_points"][0]["stage"] == "Q1"
    assert data["drop_off_points"][0]["value"] == 100.0


async def test_non_admin_forbidden_from_analytics(client: AsyncClient) -> None:
    token = await _seeker_token(client)
    resp = await client.get(ANALYTICS, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403
