"""AI-matched customer leads tests — inverse of advisor matching (PRD §3.4.3)."""

from __future__ import annotations

from httpx import AsyncClient

from tests.test_advisor_search import _make_advisor
from tests.test_assessments import ASSESSMENTS, _opt, _seed_questions, _seeker_token

LEADS = "/api/v1/advisors/me/leads"


async def _completed_assessment(client: AsyncClient, admin_token: str, headers: dict) -> str:
    questions = await _seed_questions(client, admin_token)
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
    assert resp.status_code == 200, resp.text
    return assessment_id


async def test_completed_assessment_generates_lead_for_matching_advisor(
    client: AsyncClient, engine, admin_token: str
) -> None:
    gb_id, gb_token = await _make_advisor(
        client,
        engine,
        "gb-lead-advisor@test.com",
        "GB Expert",
        {"years_of_experience": 10, "visa_specializations": ["work"], "country_expertise": ["GB"]},
    )
    await _make_advisor(
        client,
        engine,
        "ca-lead-advisor@test.com",
        "CA Expert",
        {
            "years_of_experience": 10,
            "visa_specializations": ["family"],
            "country_expertise": ["CA"],
        },
    )

    seeker_token = await _seeker_token(client, "gb-lead-seeker@test.com")
    seeker_headers = {"Authorization": f"Bearer {seeker_token}"}
    await _completed_assessment(client, admin_token, seeker_headers)

    gb_headers = {"Authorization": f"Bearer {gb_token}"}
    resp = await client.get(LEADS, headers=gb_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert len(data) == 1
    assert data[0]["destination_country"] == "GB"
    assert data[0]["visa_type"] == "work"
    assert data[0]["match_score"] == 77.5  # 40 country + 30 visa + 7.5 experience (no rating)
    assert data[0]["status"] == "new"

    # The unrelated CA advisor never sees this lead.
    ca_id, ca_token = await _make_advisor(
        client,
        engine,
        "ca-lookup@test.com",
        "CA Lookup",
        {
            "years_of_experience": 5,
            "visa_specializations": ["student"],
            "country_expertise": ["FR"],
        },
    )
    resp = await client.get(LEADS, headers={"Authorization": f"Bearer {ca_token}"})
    assert resp.status_code == 200
    assert resp.json()["data"] == []


async def test_lead_detail_marks_viewed_and_contact_dismiss_flow(
    client: AsyncClient, engine, admin_token: str
) -> None:
    gb_id, gb_token = await _make_advisor(
        client,
        engine,
        "gb-lead-advisor2@test.com",
        "GB Expert Two",
        {"years_of_experience": 8, "visa_specializations": ["work"], "country_expertise": ["GB"]},
    )
    seeker_token = await _seeker_token(client, "gb-lead-seeker2@test.com")
    seeker_headers = {"Authorization": f"Bearer {seeker_token}"}
    await _completed_assessment(client, admin_token, seeker_headers)

    gb_headers = {"Authorization": f"Bearer {gb_token}"}
    resp = await client.get(LEADS, headers=gb_headers)
    lead_id = resp.json()["data"][0]["id"]
    assert resp.json()["data"][0]["status"] == "new"

    # Detail view marks it viewed and includes match_reasons.
    resp = await client.get(f"{LEADS}/{lead_id}", headers=gb_headers)
    assert resp.status_code == 200, resp.text
    detail = resp.json()["data"]
    assert detail["status"] == "viewed"
    assert detail["match_reasons"]

    # Contact is a status marker only — no conversation is created (PRD gates
    # in-app chat to an existing booking).
    resp = await client.post(f"{LEADS}/{lead_id}/contact", headers=gb_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "contacted"
    assert "conversation_id" not in resp.json()["data"]

    resp = await client.post(f"{LEADS}/{lead_id}/dismiss", headers=gb_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "dismissed"


async def test_advisor_cannot_see_or_act_on_another_advisors_lead(
    client: AsyncClient, engine, admin_token: str
) -> None:
    gb_id, gb_token = await _make_advisor(
        client,
        engine,
        "gb-lead-advisor3@test.com",
        "GB Expert Three",
        {"years_of_experience": 6, "visa_specializations": ["work"], "country_expertise": ["GB"]},
    )
    _, other_token = await _make_advisor(
        client,
        engine,
        "other-advisor@test.com",
        "Other Advisor",
        {"years_of_experience": 6, "visa_specializations": ["family"], "country_expertise": ["FR"]},
    )
    seeker_token = await _seeker_token(client, "gb-lead-seeker3@test.com")
    seeker_headers = {"Authorization": f"Bearer {seeker_token}"}
    await _completed_assessment(client, admin_token, seeker_headers)

    resp = await client.get(LEADS, headers={"Authorization": f"Bearer {gb_token}"})
    lead_id = resp.json()["data"][0]["id"]

    other_headers = {"Authorization": f"Bearer {other_token}"}
    resp = await client.get(f"{LEADS}/{lead_id}", headers=other_headers)
    assert resp.status_code == 404
    resp = await client.post(f"{LEADS}/{lead_id}/contact", headers=other_headers)
    assert resp.status_code == 404


async def test_seeker_forbidden_from_leads_endpoints(client: AsyncClient) -> None:
    seeker_token = await _seeker_token(client)
    headers = {"Authorization": f"Bearer {seeker_token}"}
    resp = await client.get(LEADS, headers=headers)
    assert resp.status_code == 403
