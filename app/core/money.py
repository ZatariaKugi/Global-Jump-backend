"""Money helpers — coerce ORM Numeric/Decimal values to float for math & APIs."""

from __future__ import annotations

from decimal import Decimal
from typing import Any


def as_float(value: Any | None) -> float:
    """Convert ``None`` / ``Decimal`` / numeric to ``float`` (safe for sum/compare)."""
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def money_sum(values: Any, *, start: float = 0.0) -> float:
    """``sum()`` that always returns float even when values are Decimal."""
    total = start
    for value in values:
        total += as_float(value)
    return total
