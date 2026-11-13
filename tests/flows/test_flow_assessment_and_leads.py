"""Phase 2 (#185) · flows 5 and 6: assessment → result → history → matched advisors → advisor leads;
Find Advisor's readiness gate, profile match and bookmarks."""

from __future__ import annotations

import pytest

from tests.flows import helpers as h
from tests.flows.conftest import World

pytestmark = pytest.mark.asyncio

QUESTIONS = [
    (
        "What is your highest completed education?",
        "education",
        [("High school", 40), ("Bachelor's degree", 80), ("Master's degree", 100)],
    ),
    (
        "How many years of relevant work experience do you have?",
        "employment",
        [("None", 20), ("1-3 years", 60), ("More than 3 years", 100)],
    ),
    (
        "What is your English test score band?",
        "language",
        [("None yet", 10), ("6.0-6.5", 70), ("7.0 or above", 100)],
    ),
]


async def _admin_writes_the_questionnaire(world: World) -> list[dict]:
    rows = []
    for i, (text, category, options) in enumerate(QUESTIONS):
        rows.append(
            h.body(
                await world.client.post(
                    "/api/v1/admin/assessment-questions",
                    json={
                        "text": text,
                        "category": category,
                        "country_code": "AU",
                        "visa_type": "student",
                        "weightage_pct": 30,
                        "display_order": i,
                        "options": [
                            {"text": t, "score": s, "display_order": n}
                            for n, (t, s) in enumerate(options)
                        ],
                    },
                    headers=world.admin,
                )
            )
        )
    return rows


async def _advisor_matches_au_student(world: World) -> None:
    h.body(
        await world.client.patch(
            "/api/v1/advisors/me/profile",
            json={
                "country_expertise": ["AU"],
                "visa_specializations": ["student"],
                "country_of_residence": "AU",
            },
            headers=world.advisor,
        )
    )
    await h.connect_zoom(world, world.advisor)


async def test_seeker_completes_an_assessment_and_the_advisor_gets_the_lead(world: World) -> None:
    written = await _admin_writes_the_questionnaire(world)
    await _advisor_matches_au_student(world)

    questions = h.body(
        await world.client.get(
            "/api/v1/assessments/questions",
            params={"country": "AU", "visa_type": "student"},
            headers=world.seeker,
        )
    )
    assert len(questions) == 3

    started = h.body(
        await world.client.post(
            "/api/v1/assessments",
            json={"destination_country": "AU", "visa_type": "student"},
            headers=world.seeker,
        )
    )
    # seekers never see option scores; the admin's rows carry them
    best_option = {
        row["id"]: max(row["options"], key=lambda o: o["score"])["id"] for row in written
    }
    best = [{"question_id": q["id"], "option_id": best_option[q["id"]]} for q in questions]
    result = h.body(
        await world.client.post(
            f"/api/v1/assessments/{started['id']}/answers",
            json={"answers": best, "complete": True},
            headers=world.seeker,
        )
    )
    assert result["status"] == "completed" and float(result["score"]) == 100 and result["tier"]
    assert result["ai_summary"] == world.ai.insights["summary"], (
        "the narrative comes from the model (faked)"
    )
    assert result["strengths"] and result["missing_requirements"]

    history = h.body(await world.client.get("/api/v1/assessments", headers=world.seeker))
    assert [a["id"] for a in history] == [started["id"]]

    matched = h.body(
        await world.client.get(
            f"/api/v1/assessments/{started['id']}/matched-advisors", headers=world.seeker
        )
    )
    assert [m["user_id"] for m in matched][:1] == [str(world.advisor_id)]
    assert world.ai.calls, "the AI ranking was asked"

    leads = h.body(await world.client.get("/api/v1/advisors/me/leads", headers=world.advisor))
    assert len(leads) == 1 and leads[0]["seeker_id"] == str(world.seeker_id)
    assert float(leads[0]["match_score"]) >= 50


async def test_find_advisor_lists_only_ready_advisors_with_a_profile_match_and_bookmarks_work(
    world: World,
) -> None:
    await _advisor_matches_au_student(world)
    h.body(
        await world.client.patch(
            "/api/v1/users/me/profile",
            json={"intended_destination": "AU", "intended_visa_type": "student"},
            headers=world.seeker,
        )
    )
    rows = h.body(
        await world.client.get("/api/v1/advisors", params={"page_size": 50}, headers=world.seeker)
    )
    mine = next(r for r in rows if r["user_id"] == str(world.advisor_id))
    assert mine["match_percentage"] is not None and mine["match_percentage"] > 0

    # a second advisor without Zoom is not listed, whatever the profile says
    created = await h.register_advisor(world, "unready@flows-world.com", "Unready Advisor")
    await h.verify_email(world, "unready@flows-world.com")
    await h.approve_advisor(world, created["id"])
    rows = h.body(
        await world.client.get("/api/v1/advisors", params={"page_size": 50}, headers=world.seeker)
    )
    assert created["id"] not in [r["user_id"] for r in rows]

    h.body(
        await world.client.post(
            "/api/v1/bookmarks", json={"advisor_id": str(world.advisor_id)}, headers=world.seeker
        )
    )
    bookmarks = h.body(await world.client.get("/api/v1/bookmarks", headers=world.seeker))
    assert [b["advisor_id"] for b in bookmarks] == [str(world.advisor_id)]
    assert bookmarks[0]["match_percentage"] == mine["match_percentage"], (
        "the same number everywhere"
    )
    r = await world.client.delete(f"/api/v1/bookmarks/{world.advisor_id}", headers=world.seeker)
    assert r.status_code == 204
    assert h.body(await world.client.get("/api/v1/bookmarks", headers=world.seeker)) == []
