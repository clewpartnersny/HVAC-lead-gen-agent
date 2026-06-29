"""Google Maps data source, backed by the Outscraper API.

We query each MSA with several search terms and merge the results, de-duping by
Outscraper's ``place_id``. Outscraper returns a list (one element per query) of
lists of place dicts; field names are normalized here into :class:`Place`.
"""

from __future__ import annotations

import logging

from outscraper import ApiClient
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import CONFIG
from .models import Place

log = logging.getLogger(__name__)


def _first(d: dict, *keys: str, default="") -> str:
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return default


def _num(d: dict, *keys: str):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
    return None


def _to_place(rec: dict, msa: str) -> Place:
    return Place(
        name=_first(rec, "name", "title"),
        place_id=_first(rec, "place_id", "google_id"),
        address=_first(rec, "full_address", "address"),
        city=_first(rec, "city"),
        state=_first(rec, "state", "us_state"),
        postal_code=_first(rec, "postal_code", "zip"),
        phone=_first(rec, "phone", "phone_1"),
        website=_first(rec, "site", "website"),
        domain=_first(rec, "domain"),
        category=_first(rec, "type", "category"),
        subtypes=_first(rec, "subtypes", "categories"),
        rating=_num(rec, "rating"),
        reviews=int(_num(rec, "reviews", "reviews_count") or 0) or None,
        latitude=_num(rec, "latitude"),
        longitude=_num(rec, "longitude"),
        google_url=_first(rec, "location_link", "google_maps_url"),
        email=_first(rec, "email_1", "email"),
        msa=msa,
    )


class MapsClient:
    def __init__(self, api_key: str | None = None):
        self.client = ApiClient(api_key=api_key or CONFIG.outscraper_api_key)

    @retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=2, min=2, max=30))
    def _search(self, query: str, limit: int) -> list[dict]:
        # async=False makes Outscraper block until results are ready (simplest
        # for a batch job). Returns list-of-lists; we want the first query's rows.
        results = self.client.google_maps_search(
            query, limit=limit, language="en", region="US", async_request=False
        )
        if not results:
            return []
        return results[0] if isinstance(results[0], list) else results

    def search_msa(self, msa: str) -> list[Place]:
        """Return de-duplicated HVAC places for one MSA across all search terms."""
        seen: dict[str, Place] = {}
        for term in CONFIG.search_terms:
            query = f"{term} in {msa}"
            log.info("Maps query: %s", query)
            try:
                rows = self._search(query, CONFIG.results_per_query)
            except Exception as exc:  # noqa: BLE001 — keep going on a bad query
                log.warning("Query failed (%s): %s", query, exc)
                continue
            for rec in rows:
                place = _to_place(rec, msa)
                if not place.name:
                    continue
                key = place.dedupe_key()
                # Prefer the record that carries a website/email.
                if key not in seen or (place.website and not seen[key].website):
                    seen[key] = place
        places = list(seen.values())
        log.info("MSA %s: %d unique places", msa, len(places))
        return places
