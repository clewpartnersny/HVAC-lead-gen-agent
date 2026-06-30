"""Typed data structures passed between pipeline stages."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field


@dataclass
class MsaJob:
    """One metro to process.

    ``label`` is the exact name from the Organized MSAs sheet (what goes in the
    output MSA column); ``query`` appends the state for a precise Maps search.
    """

    label: str
    state: str = ""

    @property
    def query(self) -> str:
        return f"{self.label}, {self.state}" if self.state else self.label

    @property
    def key(self) -> str:
        return self.query.lower()


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

    # --- ownership / independence ---
    ownership: Ownership = Field(description="Independence / ownership status.")
    ownership_rationale: str = Field(description="Evidence for the ownership classification.")
    parent_or_acquirer: str = Field(
        default="", description="Name of PE firm / parent / acquirer if not independent, else empty."
    )

    # --- revenue ---
    revenue_band: RevenueBand = Field(description="Estimated annual revenue band (USD).")
    revenue_estimate: str = Field(
        default="", description="Human-readable revenue estimate, e.g. '$8M' or '$5-10M'. Empty if unknown."
    )
    revenue_rationale: str = Field(description="Why — employee count, fleet, footprint, stated figures.")

    # --- service vs construction / end markets ---
    service_share: int = Field(description="Estimated percent of work that is service/repair/maintenance (0-100).")
    construction_share: int = Field(description="Estimated percent of work that is new-construction (0-100).")
    serves_residential: bool = Field(description="Does meaningful residential work.")
    serves_commercial: bool = Field(description="Does meaningful commercial work.")

    # --- firmographics (leave empty if not found — do not guess) ---
    employees: str = Field(default="", description="Employee count or range, e.g. '40' or '25-50'.")
    locations: str = Field(default="", description="Number of locations/branches, e.g. '2'.")
    year_founded: str = Field(default="", description="Year founded, e.g. '1998'.")
    ppp_loan: str = Field(default="", description="PPP loan amount + year if found, e.g. '$350K (2021)'.")

    # --- owner / contact (only if actually found; never fabricate) ---
    owner_first_name: str = Field(default="", description="Owner/principal first name.")
    owner_last_name: str = Field(default="", description="Owner/principal last name.")
    owner_title: str = Field(default="", description="Owner/principal title, e.g. 'Owner', 'President'.")
    owner_linkedin: str = Field(default="", description="LinkedIn URL of the owner/principal.")
    owner_age: str = Field(default="", description="Approximate owner age if discoverable.")
    contact_email: str = Field(default="", description="Best contact email if found.")

    summary: str = Field(description="2-3 sentence summary of the company for the analyst.")
    sources: list[str] = Field(default_factory=list, description="URLs that support the assessment.")


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

        if q.serves_residential and q.serves_commercial:
            customer = "Both"
        elif q.serves_residential:
            customer = "Residential"
        elif q.serves_commercial:
            customer = "Commercial"
        else:
            customer = ""

        if p.rating is not None and p.reviews:
            reviews = f"{p.rating} ({p.reviews})"
        elif p.reviews:
            reviews = str(p.reviews)
        else:
            reviews = ""

        return {
            "company_name": p.name,
            "first_name": q.owner_first_name,
            "last_name": q.owner_last_name,
            "position": q.owner_title,
            "contact_email": q.contact_email or p.email,
            "phone": p.phone,
            "linkedin": q.owner_linkedin,
            "owner_age": q.owner_age,
            "domain": p.domain or _domain(p.website),
            "industry": p.category or "HVAC",
            "customer_type": customer,
            "city": p.city,
            "state": p.state,
            "msa": p.msa,
            "google_reviews": reviews,
            "ppp_loan": q.ppp_loan,
            "est_revenue": q.revenue_estimate or _BAND_LABELS.get(q.revenue_band, ""),
            "employees": q.employees,
            "locations": q.locations,
            "year_founded": q.year_founded,
            "notes": self._notes(),
        }

    def _notes(self) -> str:
        # Only what isn't already in a dedicated column: reasoning + sources.
        q = self.qual
        parts = [q.summary]
        indep = f"Independence: {q.ownership}"
        if q.ownership_rationale:
            indep += f" — {q.ownership_rationale}"
        parts.append(indep)
        if q.service_share or q.construction_share:
            parts.append(f"Work mix: ~{q.service_share}% service / ~{q.construction_share}% construction")
        if q.revenue_rationale:
            parts.append(f"Revenue basis: {q.revenue_rationale}")
        if q.parent_or_acquirer:
            parts.append(f"Parent/acquirer: {q.parent_or_acquirer}")
        parts.append(f"Confidence: {q.confidence:.2f}")
        if self.sources_text:
            parts.append("Sources:\n" + self.sources_text)
        return "\n".join(parts)


_BAND_LABELS = {
    "under_1m": "<$1M",
    "1m_5m": "$1-5M",
    "5m_10m": "$5-10M",
    "10m_25m": "$10-25M",
    "25m_50m": "$25-50M",
    "50m_plus": "$50M+",
    "unknown": "",
}


def _domain(website: str) -> str:
    if not website:
        return ""
    w = website.lower().strip()
    for p in ("https://", "http://"):
        if w.startswith(p):
            w = w[len(p):]
    if w.startswith("www."):
        w = w[4:]
    return w.split("/")[0].strip()
