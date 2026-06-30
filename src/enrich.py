"""Qualify a company with Claude.

Two-step design (deliberately not one call):

1. **Research** — Claude runs the ``web_search`` server tool to gather facts
   about the company (size, ownership, service vs. construction mix) and writes
   a plain-text dossier. Web search attaches citations, which are incompatible
   with structured outputs, so we keep this step tool-only.
2. **Extract** — a second, tool-free call uses ``messages.parse`` to turn that
   dossier into a validated :class:`Qualification` object.

This keeps each call doing one thing and avoids the citations/structured-output
conflict, while still grounding the judgment in live web data.
"""

from __future__ import annotations

import logging

import anthropic
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .config import CONFIG
from .models import Place, Qualification

log = logging.getLogger(__name__)

RESEARCH_SYSTEM = """\
You are a research analyst for a firm that builds acquisition target lists of
HVAC companies for private-equity clients. You investigate one company at a time
using web search and report what you find — factually, with sources.

The client cares about four things:
1. INDEPENDENCE. Is the company independently owned, or has it been acquired by
   a private-equity firm, rolled into a platform/holding company, made a
   subsidiary of a larger corporation, or is it a franchise? PE-owned, acquired,
   subsidiary, public, and franchise companies are NOT of interest. Look hard for
   acquisition press releases, "a [Platform] company" footers, PE portfolio
   pages, and parent-company branding.
2. SIZE. Roughly what is annual revenue? Infer from employee count, fleet/truck
   count, number of locations, service-area breadth, and any stated figures.
   The client wants $5M+ in revenue; very small shops are not of interest.
3. SERVICE vs. CONSTRUCTION. Is the business primarily service, repair, and
   maintenance, or primarily new-construction installation? Service-focused is
   wanted. Some construction exposure is fine, but construction must NOT be the
   majority of the work.
4. END MARKETS. Residential, commercial, or both.

Be skeptical and specific. Cite the URLs you relied on."""

RESEARCH_PROMPT = """\
Research this HVAC company and write a concise dossier. Cover, with sources:

1. Ownership / independence — and any acquirer, parent, platform, or franchise.
2. Estimated annual revenue (give a figure or tight range) with your reasoning.
3. Work mix — rough % service/maintenance vs % new-construction.
4. End markets — residential, commercial, or both.
5. Firmographics — employee count, number of locations/branches, year founded.
6. Owner / principal — first and last name, title (Owner/President/etc.),
   LinkedIn URL, and approximate age IF you can find them.
7. Best contact email for the business or owner, if published.
8. PPP loan — check public PPP databases (e.g. FederalPay / ProPublica); if a
   loan is on record, note the amount and year.

IMPORTANT: Only report owner names, emails, LinkedIn URLs, or ages that you
actually find in a source. Never guess or fabricate contact details — if you
can't find something, say it's unknown. End with the source URLs you used.

Company: {name}
Location: {location}
Website: {website}
Phone: {phone}
Google category: {category}
Google rating / reviews: {rating} / {reviews}
"""

EXTRACT_SYSTEM = """\
You convert a research dossier about an HVAC company into a structured
assessment. Apply the client's rules strictly:

- verdict "reject" if the company is PE-owned, acquired, a subsidiary, public,
  or a franchise; or if estimated revenue is clearly under $5M; or if
  new-construction is the majority of the work.
- verdict "qualified" only if it is independently owned, estimated revenue is
  ~$5M or more, and service/maintenance is the majority of the work (some
  construction exposure is acceptable).
- verdict "review" when the dossier is genuinely inconclusive on independence or
  revenue — do not guess "qualified" to be generous.

service_share + construction_share should sum to roughly 100.

Fill the firmographic and owner fields (employees, locations, year_founded,
ppp_loan, owner name/title/LinkedIn/age, contact_email) ONLY from facts present
in the dossier. Leave any field empty if the dossier doesn't establish it — never
invent an owner name, email, LinkedIn URL, or age."""


class Enricher:
    def __init__(self):
        self.client = anthropic.Anthropic(api_key=CONFIG.anthropic_api_key)
        self.model = CONFIG.anthropic_model

    @retry(
        retry=retry_if_exception_type(
            (anthropic.RateLimitError, anthropic.InternalServerError, anthropic.APIConnectionError)
        ),
        stop=stop_after_attempt(2),  # bounded — a stalled research call should skip, not spin
        wait=wait_exponential(multiplier=2, min=4, max=30),
    )
    def _research(self, place: Place) -> str:
        location = ", ".join(p for p in [place.city, place.state] if p) or place.address or place.msa
        prompt = RESEARCH_PROMPT.format(
            name=place.name,
            location=location,
            website=place.website or "(unknown)",
            phone=place.phone or "(unknown)",
            category=place.category or "(unknown)",
            rating=place.rating if place.rating is not None else "n/a",
            reviews=place.reviews if place.reviews is not None else "n/a",
        )
        # Stream to stay under HTTP timeouts; cap effort/searches so each company
        # takes ~1-2 min instead of many. A hard timeout skips a stalled lookup.
        client = self.client.with_options(timeout=180.0)
        with client.messages.stream(
            model=self.model,
            max_tokens=4000,
            thinking={"type": "adaptive"},
            output_config={"effort": "low"},
            system=RESEARCH_SYSTEM,
            tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 4}],
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            msg = stream.get_final_message()
        return "".join(b.text for b in msg.content if b.type == "text").strip()

    @retry(
        retry=retry_if_exception_type(
            (anthropic.RateLimitError, anthropic.InternalServerError, anthropic.APIConnectionError)
        ),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=2, min=4, max=60),
    )
    def _extract(self, dossier: str, place: Place) -> Qualification:
        resp = self.client.messages.parse(
            model=self.model,
            max_tokens=2500,
            system=EXTRACT_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": f"Company: {place.name}\n\nDossier:\n{dossier}",
                }
            ],
            output_format=Qualification,
        )
        if resp.parsed_output is None:
            raise RuntimeError(f"Could not parse qualification for {place.name}")
        return resp.parsed_output

    def qualify(self, place: Place) -> tuple[Qualification, str]:
        """Return (qualification, dossier_text) for one company."""
        log.info("Qualifying: %s (%s)", place.name, place.msa)
        dossier = self._research(place)
        qual = self._extract(dossier, place)
        return qual, dossier
