"""Admin-configurable advisor matching weights (AI Engine Management).

Singleton row — UI sliders for country / language / availability / setting.
Language and availability contribute when profile data exists; otherwise 0.
``setting_weight`` maps to visa-type specialization match (UI "Setting").
"""

from __future__ import annotations

from sqlalchemy import Float
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_model import BaseModel


class AdvisorMatchingWeights(BaseModel):
    """Global matching weight config (one active row expected)."""

    __tablename__ = "advisor_matching_weights"

    country_weight: Mapped[float] = mapped_column(Float, nullable=False, default=40.0)
    language_weight: Mapped[float] = mapped_column(Float, nullable=False, default=20.0)
    availability_weight: Mapped[float] = mapped_column(Float, nullable=False, default=20.0)
    setting_weight: Mapped[float] = mapped_column(Float, nullable=False, default=20.0)
