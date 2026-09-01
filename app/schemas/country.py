"""Schema for public country and language reference data."""

from __future__ import annotations

from pydantic import BaseModel


class CountryRead(BaseModel):
    code: str
    name: str
    flag: str | None = None


class LanguageRead(BaseModel):
    code: str
    name: str
