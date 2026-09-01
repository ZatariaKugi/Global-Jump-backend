"""Language reference data — no authentication required.

Returns ISO 639 languages from ``pycountry`` for language pickers,
with optional search and pagination.
"""

from __future__ import annotations

import pycountry
from fastapi import APIRouter, Query

from app.api.deps import RequestIdDep
from app.api.pagination import PaginationDep, page_meta
from app.schemas.country import LanguageRead
from app.schemas.response import ResponseEnvelope

router = APIRouter(prefix="/languages", tags=["languages"])

_POPULAR_ALPHA2 = [
    "en",  # English
    "es",  # Spanish
    "zh",  # Chinese
    "hi",  # Hindi
    "ar",  # Arabic
    "fr",  # French
    "pt",  # Portuguese
    "ru",  # Russian
    "ur",  # Urdu
    "de",  # German
    "ja",  # Japanese
    "ko",  # Korean
    "it",  # Italian
    "tr",  # Turkish
    "fa",  # Persian
]

_POPULAR_LANGUAGES: list[LanguageRead] = []
for _code in _POPULAR_ALPHA2:
    _lang = pycountry.languages.get(alpha_2=_code)
    if _lang is not None:
        _POPULAR_LANGUAGES.append(
            LanguageRead(code=_lang.alpha_3, name=_lang.name)
        )


@router.get("", response_model=ResponseEnvelope[list[LanguageRead]])
async def list_languages(
    request_id: RequestIdDep,
    pagination: PaginationDep,
    q: str | None = Query(default=None, description="Search by language name or code"),
) -> ResponseEnvelope[list[LanguageRead]]:
    results = _POPULAR_LANGUAGES

    if q:
        term = q.lower()
        results = [
            lang for lang in results
            if term in lang.name.lower() or term in lang.code.lower()
        ]

    total = len(results)
    start = pagination.offset
    end = start + pagination.limit
    page_items = results[start:end]

    return ResponseEnvelope[list[LanguageRead]](
        data=page_items,
        meta=page_meta(pagination, total, request_id=request_id),
    )
