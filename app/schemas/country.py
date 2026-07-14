"""Schema for public country reference data."""

from __future__ import annotations

from pydantic import BaseModel


class CountryRead(BaseModel):
    code: str
    name: str
