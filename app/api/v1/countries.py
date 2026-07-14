"""Public country reference data — no authentication required.

Used by frontend country pickers (nationality, destination, travel history,
etc.) so the client doesn't have to hardcode the ISO 3166-1 list.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import RequestIdDep
from app.core.countries import COUNTRY_NAMES
from app.schemas.country import CountryRead
from app.schemas.response import Meta, ResponseEnvelope

router = APIRouter(prefix="/countries", tags=["countries"])


@router.get("", response_model=ResponseEnvelope[list[CountryRead]])
async def list_countries(request_id: RequestIdDep) -> ResponseEnvelope[list[CountryRead]]:
    countries = [
        CountryRead(code=code, name=name)
        for code, name in sorted(COUNTRY_NAMES.items(), key=lambda item: item[1])
    ]
    return ResponseEnvelope[list[CountryRead]](data=countries, meta=Meta(request_id=request_id))
