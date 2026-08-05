"""Official government immigration-source registry (Country Rules & Policies §5).

Maps each supported country to the authoritative government starting URL and the
domain(s) we expect the AI to retrieve from. This keeps AI drafting pointed at
registered government sources rather than arbitrary sites, and lets us decide
whether a draft is "grounded" (the retrieved URL belonged to an expected domain).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse


@dataclass(frozen=True)
class OfficialSource:
    country_code: str
    label: str  
    start_url: str  
    expected_domains: tuple[str, ...] = field(default_factory=tuple)  


_OFFICIAL_SOURCES: dict[str, OfficialSource] = {
    "CA": OfficialSource(
        "CA",
        "IRCC / Canada.ca",
        "https://www.canada.ca/en/immigration-refugees-citizenship.html",
        ("canada.ca",),
    ),
    "GB": OfficialSource(
        "GB",
        "GOV.UK / UKVI",
        "https://www.gov.uk/browse/visas-immigration",
        ("gov.uk",),
    ),
    "AU": OfficialSource(
        "AU",
        "Department of Home Affairs",
        "https://immi.homeaffairs.gov.au/visas",
        ("homeaffairs.gov.au", "immi.homeaffairs.gov.au"),
    ),
    "NZ": OfficialSource(
        "NZ",
        "Immigration New Zealand",
        "https://www.immigration.govt.nz/new-zealand-visas",
        ("immigration.govt.nz", "govt.nz"),
    ),
    "DE": OfficialSource(
        "DE",
        "Make it in Germany",
        "https://www.make-it-in-germany.com/en/visa-residence",
        ("make-it-in-germany.com",),
    ),
    "IE": OfficialSource(
        "IE",
        "Immigration Service Delivery",
        "https://www.irishimmigration.ie/",
        ("irishimmigration.ie",),
    ),
    "MX": OfficialSource(
        "MX",
        "INM / gob.mx",
        "https://www.gob.mx/inm",
        ("gob.mx",),
    ),
    "ES": OfficialSource(
        "ES",
        "Ministerio de Inclusión",
        "https://www.inclusion.gob.es/web/migraciones/extranjeria",
        ("inclusion.gob.es", "gob.es"),
    ),
    "PT": OfficialSource(
        "PT",
        "AIMA",
        "https://aima.gov.pt/en",
        ("aima.gov.pt", "gov.pt"),
    ),
    "JP": OfficialSource(
        "JP",
        "Japan MOFA / ISA",
        "https://www.mofa.go.jp/j_info/visit/visa/",
        ("mofa.go.jp", "isa.go.jp", "moj.go.jp"),
    ),
}


def official_source(country_code: str | None) -> OfficialSource | None:
    """Registered official source for a supported country, or ``None``."""
    if not country_code:
        return None
    return _OFFICIAL_SOURCES.get(country_code.upper())


def is_expected_domain(url: str | None, country_code: str | None) -> bool:
    """True if ``url``'s host is (a subdomain of) an expected official domain.

    Used to compute the ``grounded`` flag: a retrieved URL is grounded only when
    it belongs to the country's registered government domain.
    """
    source = official_source(country_code)
    if source is None or not url:
        return False
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    return any(host == dom or host.endswith(f".{dom}") for dom in source.expected_domains)
