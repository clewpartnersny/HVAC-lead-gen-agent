"""Typed data structures passed between pipeline stages."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field


@dataclass
class Place:
    """A raw business record as returned by the Google Maps scrape."""

    name: str
    place_id: str = ""
    address: str = ""
    city: str = ""
    state: str = ""
    postal_code: str = ""
    phone: str = ""
    website: str = ""
    domain: str = ""
    category: str = ""
    subtypes: str = ""
    rating: float | None = None
    reviews: int | None = None
    latitude: float | None = None
    longitude: float | None = None
    google_url: str = ""
    email: str = ""
    msa: str = ""

    def dedupe_key(self) -> str:
        """Stable identity for de-duplication across runs and MSAs."""
        if self.place_id:
            return f"place:{self.place_id}".lower()
        if self.domain:
            return f"domain:{self.domain}".lower()
        return f"name:{self.name}|{self.address}".lower().strip()


# --- Structured output schema returned by Claude during qualification --------
# Pydantic model => used directly with client.messages.parse(). Keep it flat and
# free of unsupported JSON-schema constraints (no min/max), per the API rules.

Verdict = Literal["qualified", "reject", "review"]
RevenueBand = Literal[
    "under_1m", "1m_5m", "5m_10m", "10m_25m", "25m_50m", "50m_plus", "unknown"
]
Ownership = Literal[
    "independent",
    "pe_owned",
    "subsidiary",
    "franchise",
    "public",
    "unknown",
]


class Qualification(BaseModel):
    """Claude's structured assessment of a single company."""

    verdict: Verdict = Field(description="Overall fit decision for the lead list.")
    confidence: float = Field(description="0.0-1.0 confidence in the verdict.")

    revenue_band: RevenueBand = Field(description="Estimated annual revenue band (USD).")
    revenue_rationale: str = Field(description="Why this band — employee count, fleet, footprint, etc.")

    ownership: Ownership = Field(description="Independence / ownership status.")
    ownership_rationale: str = Field(description="Evidence for the ownership classification.")
    parent_or_acquirer: str = Field(
        default="", description="Name of PE firm / parent / acquirer if not independent, else empty."
    )

    service_share: int = Field(
        description="Estimated percent of work that is service/repair/maintenance (0-100)."
    )
    construction_share: int = Field(
        description="Estimated percent of work that is new-construction (0-100)."
    )
    serves_residential: bool = Field(description="Does meaningful residential work.")
    serves_commercial: bool = Field(description="Does meaningful commercial work.")

    summary: str = Field(description="2-3 sentence summary of the company for the analyst.")
    sources: list[str] = Field(
        default_factory=list, description="URLs that support the assessment."
    )


@dataclass
class Lead:
    """A Place plus its qualification, ready to be written to a sheet."""

    place: Place
    qual: Qualification
    sources_text: str = ""

    def as_field_map(self) -> dict[str, str]:
        """Flatten into a {logical_field: value} map for the sheet writer.

        Keys here are matched (fuzzily) against the Research Template headers
        in ``sheets.py`` — see FIELD_ALIASES there.
        """
        p, q = self.place, self.qual
        return {
            "company_name": p.name,
            "website": p.website,
            "phone": p.phone,
            "email": p.email,
            "address": p.address,
            "city": p.city,
            "state": p.state,
            "zip": p.postal_code,
            "msa": p.msa,
            "google_maps_url": p.google_url,
            "rating": "" if p.rating is None else str(p.rating),
            "review_count": "" if p.reviews is None else str(p.reviews),
            "category": p.category,
            "revenue_band": q.revenue_band,
            "revenue_rationale": q.revenue_rationale,
            "ownership": q.ownership,
            "ownership_rationale": q.ownership_rationale,
            "parent_or_acquirer": q.parent_or_acquirer,
            "service_pct": str(q.service_share),
            "construction_pct": str(q.construction_share),
            "residential": "Yes" if q.serves_residential else "No",
            "commercial": "Yes" if q.serves_commercial else "No",
            "summary": q.summary,
            "confidence": f"{q.confidence:.2f}",
            "verdict": q.verdict,
            "sources": self.sources_text,
        }
