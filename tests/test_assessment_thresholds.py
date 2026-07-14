"""Assessment threshold settings tests (PRD §3.4 AI Engine Management)."""

from __future__ import annotations

from httpx import AsyncClient

from tests.test_assessments import ASSESSMENTS, _opt, _seed_questions, _seeker_token

THRESHOLDS = "/api/v1/admin/assessment-thresholds"


async def test_get_threshold_returns_null_when_not_configured(
    client: AsyncClient, admin_token: str
) -> None:
    headers = {"Authorization": f"Bearer {admin_token}"}
    resp = await client.get(f"{THRESHOLDS}?country=GB&visa_type=work", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] is None


async def test_put_upserts_threshold_for_scope(client: AsyncClient, admin_token: str) -> None:
    headers = {"Authorization": f"Bearer {admin_token}"}

    resp = await client.put(
        THRESHOLDS,
        json={
            "country_code": "gb",
            "visa_type": "Work",
            "highly_eligible_min": 90,
            "likely_eligible_min": 70,
            "borderline_min": 50,
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["country_code"] == "GB"
    assert data["visa_type"] == "work"
    assert data["highly_eligible_min"] == 90

    # Re-PUTting the same scope updates the existing row rather than creating a new one.
    resp = await client.put(
        THRESHOLDS,
        json={
            "country_code": "gb",
            "visa_type": "work",
            "highly_eligible_min": 95,
            "likely_eligible_min": 70,
            "borderline_min": 50,
        },
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["highly_eligible_min"] == 95

    resp = await client.get(f"{THRESHOLDS}?country=GB&visa_type=work", headers=headers)
    assert resp.json()["data"]["highly_eligible_min"] == 95


async def test_thresholds_must_be_in_descending_order(
    client: AsyncClient, admin_token: str
) -> None:
    headers = {"Authorization": f"Bearer {admin_token}"}
    resp = await client.put(
        THRESHOLDS,
        json={
            "highly_eligible_min": 50,
            "likely_eligible_min": 60,  # not descending — rejected
            "borderline_min": 40,
        },
        headers=headers,
    )
    assert resp.status_code == 422


async def test_custom_threshold_changes_tier_resolution(
    client: AsyncClient, admin_token: str
) -> None:
    headers = {"Authorization": f"Bearer {admin_token}"}

    # Lower the borderline cutoff for GB/work so a 22.5 score (which is
    # low_eligibility under the 40-point default) now qualifies as borderline.
    resp = await client.put(
        THRESHOLDS,
        json={
            "country_code": "GB",
            "visa_type": "work",
            "highly_eligible_min": 80,
            "likely_eligible_min": 60,
            "borderline_min": 20,
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    questions = await _seed_questions(client, admin_token)
    token = await _seeker_token(client)
    seeker_headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post(
        ASSESSMENTS,
        json={"destination_country": "GB", "visa_type": "work"},
        headers=seeker_headers,
    )
    assessment_id = resp.json()["data"]["id"]

    # Same combo as the default-threshold test: score == 22.5.
    answers = [
        {"question_id": questions[0]["id"], "option_id": _opt(questions[0], "No")},
        {"question_id": questions[1]["id"], "option_id": _opt(questions[1], "Borderline")},
        {"question_id": questions[2]["id"], "option_id": _opt(questions[2], "Once")},
    ]
    resp = await client.post(
        f"{ASSESSMENTS}/{assessment_id}/answers", json={"answers": answers}, headers=seeker_headers
    )
    result = resp.json()["data"]
    assert result["score"] == 22.5
    assert result["tier"] == "borderline"  # was low_eligibility under the 40-point default


async def test_non_admin_forbidden_from_thresholds(client: AsyncClient) -> None:
    token = await _seeker_token(client)
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.get(THRESHOLDS, headers=headers)
    assert resp.status_code == 403

    resp = await client.put(
        THRESHOLDS,
        json={"highly_eligible_min": 80, "likely_eligible_min": 60, "borderline_min": 40},
        headers=headers,
    )
    assert resp.status_code == 403
