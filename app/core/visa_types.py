"""VisaType helpers — display labels and (legacy) input normalisation.

Canonical values live on ``app.models.visa_type.VisaType``. New API write fields
should type as ``VisaType`` directly. Read schemas may use ``OptionalVisaType`` /
``RequiredVisaType`` so legacy DB aliases still serialise after remap.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BeforeValidator

from app.models.visa_type import VisaType

# Display label for each canonical enum member (admin charts / UI copy).
VISA_TYPE_LABELS: dict[VisaType, str] = {
    VisaType.student: "Study Visa",
    VisaType.work: "Work Visa",
    VisaType.tourist: "Tourist Visa",
    VisaType.pr: "Permanent Residency",
    VisaType.family: "Family Sponsorship",
    VisaType.investment: "Investment Visa",
    VisaType.asylum: "Asylum",
}

# Legacy / UI aliases → canonical enum. Used to normalise old DB rows.
_ALIASES: dict[str, VisaType] = {
    "tourist": VisaType.tourist,
    "tourist visa": VisaType.tourist,
    "tourist_visa": VisaType.tourist,
    "work": VisaType.work,
    "work visa": VisaType.work,
    "work_visa": VisaType.work,
    "student": VisaType.student,
    "student visa": VisaType.student,
    "study": VisaType.student,
    "study visa": VisaType.student,
    "study_visa": VisaType.student,
    "pr": VisaType.pr,
    "permanent residency": VisaType.pr,
    "permanent_residency": VisaType.pr,
    "family": VisaType.family,
    "family sponsorship": VisaType.family,
    "family_sponsorship": VisaType.family,
    "investment": VisaType.investment,
    "investment visa": VisaType.investment,
    "asylum": VisaType.asylum,
}


def parse_visa_type(value: str | VisaType | None) -> VisaType | None:
    """Resolve a string / enum to ``VisaType``, or ``None`` if unknown/empty."""
    if value is None:
        return None
    if isinstance(value, VisaType):
        return value
    normalized = value.strip().casefold()
    if not normalized:
        return None
    return _ALIASES.get(normalized)


def visa_type_name(code: str | VisaType | None) -> str | None:
    """Map a visa-type value to its display label, or ``None`` if unknown."""
    parsed = parse_visa_type(code)
    if parsed is None:
        return None
    return VISA_TYPE_LABELS[parsed]


def visa_type_code(value: str | VisaType | None) -> str | None:
    """Normalise input to the canonical stored slug (``VisaType`` value)."""
    parsed = parse_visa_type(value)
    return parsed.value if parsed is not None else None


def humanize_slug(value: str | None) -> str | None:
    """Generic ``snake_case`` → ``Title Case`` for non-visa slugs (e.g. service types)."""
    if value is None:
        return None
    text = value.strip().replace("_", " ")
    return text.title() if text else None


def _coerce_optional_visa_type(value: object) -> VisaType | None:
    if value is None or value == "":
        return None
    if isinstance(value, VisaType):
        return value
    return parse_visa_type(str(value))


def _coerce_required_visa_type(value: object) -> VisaType:
    parsed = _coerce_optional_visa_type(value)
    if parsed is None:
        raise ValueError(f"Invalid visa type: {value!r}")
    return parsed


# Use on read schemas so legacy aliases (e.g. ``study``) still serialise.
OptionalVisaType = Annotated[VisaType | None, BeforeValidator(_coerce_optional_visa_type)]
RequiredVisaType = Annotated[VisaType, BeforeValidator(_coerce_required_visa_type)]
