"""Admin-configurable advisor matching weights (AI Engine Management).

Singleton row — sliders for destination / visa / language / services /
experience / rating. Values should sum to 100.
"""

from __future__ import annotations

from sqlalchemy import Float
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_model import BaseModel


class AdvisorMatchingWeights(BaseModel):
    """Global matching weight config (one active row expected)."""

    __tablename__ = "advisor_matching_weights"

    country_weight: Mapped[float] = mapped_column(Float, nullable=False, default=25.0)
    visa_weight: Mapped[float] = mapped_column(Float, nullable=False, default=25.0)
    language_weight: Mapped[float] = mapped_column(Float, nullable=False, default=15.0)
    services_weight: Mapped[float] = mapped_column(Float, nullable=False, default=15.0)
    experience_weight: Mapped[float] = mapped_column(Float, nullable=False, default=10.0)
    rating_weight: Mapped[float] = mapped_column(Float, nullable=False, default=10.0)
