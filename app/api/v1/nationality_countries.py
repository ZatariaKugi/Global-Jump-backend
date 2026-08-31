"""Nationality country reference data — no authentication required.

Returns all ISO 3166-1 countries from ``pycountry`` for the onboarding
nationality picker so the frontend doesn't have to hardcode the list.
"""

from __future__ import annotations

import pycountry
from fastapi import APIRouter

from app.api.deps import RequestIdDep
from app.schemas.country import CountryRead
from app.schemas.response import Meta, ResponseEnvelope

router = APIRouter(prefix="/nationality_countries", tags=["countries"])


@router.get("", response_model=ResponseEnvelope[list[CountryRead]])
async def list_nationality_countries(
    request_id: RequestIdDep,
) -> ResponseEnvelope[list[CountryRead]]:
    countries = [
        CountryRead(
            code=country.alpha_2,
            name=country.name,
            flag=country.flag,
        )
        for country in pycountry.countries
    ]
    countries.sort(key=lambda c: c.name)
    return ResponseEnvelope[list[CountryRead]](data=countries, meta=Meta(request_id=request_id))
