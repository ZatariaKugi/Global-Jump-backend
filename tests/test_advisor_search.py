"""Advisor discovery & search endpoint tests (epic #7, PRD §3.5)."""

from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.security import hash_password
from app.models.user import User, UserRole, VerificationStatus

ADVISORS = "/api/v1/advisors"
LOGIN = "/api/v1/auth/login"


async def _make_advisor(
    client: AsyncClient,
    engine,
    email: str,
    full_name: str,
    profile: dict | None = None,
) -> tuple[str, str]:
    """Create an approved advisor and optionally set its profile. Returns (user_id, token)."""
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        user = User(
            email=email,
            full_name=full_name,
            hashed_password=hash_password("advisorpass1"),
            role=UserRole.advisor,
            is_active=True,
            verification_status=VerificationStatus.approved,
        )
        session.add(user)
        await session.commit()

    resp = await client.post(LOGIN, data={"username": email, "password": "advisorpass1"})
    assert resp.status_code == 200, resp.text
    token = str(resp.json()["access_token"])
    headers = {"Authorization": f"Bearer {token}"}

    if profile is not None:
        resp = await client.patch(f"{ADVISORS}/me/profile", json=profile, headers=headers)
        assert resp.status_code == 200, resp.text

    me = await client.get(f"{ADVISORS}/me/profile", headers=headers)
    return str(me.json()["data"]["user_id"]), token


async def _seed_two_advisors(client: AsyncClient, engine) -> tuple[str, str, str]:
    """Two distinct advisors; returns (alice_id, bob_id, alice_token)."""
    alice_id, alice_token = await _make_advisor(
        client,
        engine,
        "alice@test.com",
        "Alice Andersson",
        {
            "title": "UK Immigration Lawyer",
            "bio": "Specialist in skilled worker routes",
            "years_of_experience": 12,
            "visa_specializations": ["work", "student"],
            "country_expertise": ["GB", "IE"],
            "languages": [{"language": "English", "proficiency": "native"}],
            "services": [
                {"service_type": "consultation_60", "duration_minutes": 60, "price_usd": 150.0}
            ],
        },
    )
    bob_id, _ = await _make_advisor(
        client,
        engine,
        "bob@test.com",
        "Bob Baker",
        {
            "title": "Canada PR Consultant",
            "bio": "Express entry and family sponsorship",
            "years_of_experience": 5,
            "visa_specializations": ["pr", "family"],
            "country_expertise": ["CA"],
            "languages": [{"language": "French", "proficiency": "fluent"}],
            "services": [
                {
                    "service_type": "immigration_specialist",
                    "duration_minutes": 30,
                    "price_usd": 60.0,
                }
            ],
        },
    )
    return alice_id, bob_id, alice_token


def _ids(body: dict) -> list[str]:
    return [card["user_id"] for card in body["data"]]


async def test_keyword_search_by_name(client: AsyncClient, engine) -> None:
    alice_id, bob_id, token = await _seed_two_advisors(client, engine)
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.get(f"{ADVISORS}?q=alice", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert _ids(body) == [alice_id]


async def test_keyword_search_matches_bio(client: AsyncClient, engine) -> None:
    alice_id, bob_id, token = await _seed_two_advisors(client, engine)
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.get(f"{ADVISORS}?q=express entry", headers=headers)
    assert _ids(resp.json()) == [bob_id]


async def test_filter_by_country(client: AsyncClient, engine) -> None:
    alice_id, bob_id, token = await _seed_two_advisors(client, engine)
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.get(f"{ADVISORS}?country=gb", headers=headers)
    assert _ids(resp.json()) == [alice_id]
    resp = await client.get(f"{ADVISORS}?country=CA", headers=headers)
    assert _ids(resp.json()) == [bob_id]


async def test_filter_by_visa_type(client: AsyncClient, engine) -> None:
    alice_id, bob_id, token = await _seed_two_advisors(client, engine)
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.get(f"{ADVISORS}?visa_type=work", headers=headers)
    assert _ids(resp.json()) == [alice_id]
    resp = await client.get(f"{ADVISORS}?visa_type=family", headers=headers)
    assert _ids(resp.json()) == [bob_id]


async def test_filter_by_language(client: AsyncClient, engine) -> None:
    alice_id, bob_id, token = await _seed_two_advisors(client, engine)
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.get(f"{ADVISORS}?language=french", headers=headers)
    assert _ids(resp.json()) == [bob_id]


async def test_filter_by_price_range(client: AsyncClient, engine) -> None:
    alice_id, bob_id, token = await _seed_two_advisors(client, engine)
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.get(f"{ADVISORS}?max_price=100", headers=headers)
    assert _ids(resp.json()) == [bob_id]
    resp = await client.get(f"{ADVISORS}?min_price=100", headers=headers)
    assert _ids(resp.json()) == [alice_id]


async def test_sort_by_price(client: AsyncClient, engine) -> None:
    alice_id, bob_id, token = await _seed_two_advisors(client, engine)
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.get(f"{ADVISORS}?sort=price_asc", headers=headers)
    assert _ids(resp.json()) == [bob_id, alice_id]
    resp = await client.get(f"{ADVISORS}?sort=price_desc", headers=headers)
    assert _ids(resp.json()) == [alice_id, bob_id]


async def test_sort_by_experience(client: AsyncClient, engine) -> None:
    alice_id, bob_id, token = await _seed_two_advisors(client, engine)
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.get(f"{ADVISORS}?sort=experience", headers=headers)
    assert _ids(resp.json()) == [alice_id, bob_id]


async def test_listing_card_shape(client: AsyncClient, engine) -> None:
    alice_id, _, token = await _seed_two_advisors(client, engine)
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.get(f"{ADVISORS}?q=alice", headers=headers)
    card = resp.json()["data"][0]
    assert card["full_name"] == "Alice Andersson"
    assert card["starting_price_usd"] == 150.0
    assert card["languages"] == ["English"]
    assert card["visa_specializations"] == ["work", "student"]
    assert card["public_profile_slug"]


async def test_slug_lookup(client: AsyncClient, engine) -> None:
    alice_id, _, token = await _seed_two_advisors(client, engine)
    headers = {"Authorization": f"Bearer {token}"}

    list_resp = await client.get(f"{ADVISORS}?q=alice", headers=headers)
    slug = list_resp.json()["data"][0]["public_profile_slug"]
    assert slug.startswith("alice-andersson")

    resp = await client.get(f"{ADVISORS}/slug/{slug}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["data"]["user_id"] == alice_id

    resp = await client.get(f"{ADVISORS}/slug/no-such-slug", headers=headers)
    assert resp.status_code == 404


async def test_featured_flow(client: AsyncClient, engine, admin_token: str) -> None:
    alice_id, bob_id, token = await _seed_two_advisors(client, engine)
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    headers = {"Authorization": f"Bearer {token}"}

    # Initially nobody is featured.
    resp = await client.get(f"{ADVISORS}/featured", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["data"] == []

    # Admin features Alice.
    resp = await client.patch(
        f"/api/v1/admin/advisors/{alice_id}/feature",
        json={"is_featured": True},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["is_featured"] is True

    resp = await client.get(f"{ADVISORS}/featured", headers=headers)
    assert _ids(resp.json()) == [alice_id]

    # Un-feature again.
    resp = await client.patch(
        f"/api/v1/admin/advisors/{alice_id}/feature",
        json={"is_featured": False},
        headers=admin_headers,
    )
    assert resp.json()["data"]["is_featured"] is False


async def test_feature_requires_admin(client: AsyncClient, engine) -> None:
    alice_id, _, token = await _seed_two_advisors(client, engine)
    resp = await client.patch(
        f"/api/v1/admin/advisors/{alice_id}/feature",
        json={"is_featured": True},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "forbidden"


async def test_feature_unknown_advisor_404(client: AsyncClient, admin_token: str) -> None:
    resp = await client.patch(
        f"/api/v1/admin/advisors/{uuid.uuid4()}/feature",
        json={"is_featured": True},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 404
