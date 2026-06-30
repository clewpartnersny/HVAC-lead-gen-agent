"""Google Maps data source.

Two interchangeable backends — pick one with ``MAPS_PROVIDER`` (or just set the
key for whichever you have; the factory auto-detects):

- **Serper** (``SERPER_API_KEY``) — serper.dev ``/maps`` endpoint, paginated.
- **Outscraper** (``OUTSCRAPER_API_KEY``) — outscraper.com Google Maps search.

Both normalize their raw rows into :class:`Place` and de-dupe by place id.
"""

from __future__ import annotations

import logging
import math
import re

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import CONFIG
from .models import Place

log = logging.getLogger(__name__)

_STATE_ZIP = re.compile(r",\s*([A-Za-z]{2})\s+(\d{5})(?:-\d{4})?", re.ASCII)


def _first(d: dict, *keys: str, default=""):
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


def _parse_us_address(address: str) -> tuple[str, str, str]:
    """Best-effort (city, state, zip) from a full US address string."""
    if not address:
        return "", "", ""
    m = _STATE_ZIP.search(address)
    state, zipc = (m.group(1).upper(), m.group(2)) if m else ("", "")
    city = ""
    if m:
        before = address[: m.start()].rstrip().rstrip(",")
        # city is the last comma-separated chunk before ", ST ZIP"
        city = before.split(",")[-1].strip()
    return city, state, zipc


# --- Serper backend ----------------------------------------------------------
class SerperMapsClient:
    URL = "https://google.serper.dev/maps"
    PER_PAGE = 20

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or CONFIG.serper_api_key

    @retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=2, min=2, max=30))
    def _page(self, query: str, page: int) -> list[dict]:
        resp = requests.post(
            self.URL,
            headers={"X-API-KEY": self.api_key, "Content-Type": "application/json"},
            json={"q": query, "page": page},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("places", []) or []

    def _to_place(self, rec: dict, msa: str) -> Place:
        address = _first(rec, "address")
        city, state, zipc = _parse_us_address(address)
        cid = _first(rec, "cid")
        category = _first(rec, "category", "type")
        types = rec.get("types") or []
        return Place(
            name=_first(rec, "title"),
            place_id=_first(rec, "placeId", "cid", "fid"),
            address=address,
            city=city,
            state=state,
            postal_code=zipc,
            phone=_first(rec, "phoneNumber"),
            website=_first(rec, "website"),
            category=category,
            subtypes=", ".join(types) if isinstance(types, list) else str(types),
            rating=_num(rec, "rating"),
            reviews=int(_num(rec, "ratingCount") or 0) or None,
            latitude=_num(rec, "latitude"),
            longitude=_num(rec, "longitude"),
            google_url=f"https://www.google.com/maps?cid={cid}" if cid else "",
            msa=msa,
        )

    def search_msa(self, msa: str) -> list[Place]:
        seen: dict[str, Place] = {}
        max_pages = max(1, math.ceil(CONFIG.results_per_query / self.PER_PAGE))
        for term in CONFIG.search_terms:
            query = f"{term} in {msa}"
            log.info("Maps query: %s", query)
            for page in range(1, max_pages + 1):
                try:
                    rows = self._page(query, page)
                except Exception as exc:  # noqa: BLE001 — skip a bad page/query
                    log.warning("Query failed (%s p%d): %s", query, page, exc)
                    break
                if not rows:
                    break
                for rec in rows:
                    place = self._to_place(rec, msa)
                    if not place.name:
                        continue
                    key = place.dedupe_key()
                    if key not in seen or (place.website and not seen[key].website):
                        seen[key] = place
        places = list(seen.values())
        log.info("MSA %s: %d unique places", msa, len(places))
        return places


# --- Outscraper backend ------------------------------------------------------
class OutscraperMapsClient:
    def __init__(self, api_key: str | None = None):
        from outscraper import ApiClient  # imported lazily so Serper users don't need it

        self.client = ApiClient(api_key=api_key or CONFIG.outscraper_api_key)

    @retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=2, min=2, max=30))
    def _search(self, query: str, limit: int) -> list[dict]:
        results = self.client.google_maps_search(
            query, limit=limit, language="en", region="US", async_request=False
        )
        if not results:
            return []
        return results[0] if isinstance(results[0], list) else results

    @staticmethod
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

    def search_msa(self, msa: str) -> list[Place]:
        seen: dict[str, Place] = {}
        for term in CONFIG.search_terms:
            query = f"{term} in {msa}"
            log.info("Maps query: %s", query)
            try:
                rows = self._search(query, CONFIG.results_per_query)
            except Exception as exc:  # noqa: BLE001
                log.warning("Query failed (%s): %s", query, exc)
                continue
            for rec in rows:
                place = self._to_place(rec, msa)
                if not place.name:
                    continue
                key = place.dedupe_key()
                if key not in seen or (place.website and not seen[key].website):
                    seen[key] = place
        places = list(seen.values())
        log.info("MSA %s: %d unique places", msa, len(places))
        return places


def get_maps_client():
    """Return the configured Maps backend (Serper preferred, else Outscraper)."""
    provider = CONFIG.maps_provider
    if provider == "serper" or (provider == "auto" and CONFIG.serper_api_key):
        log.info("Maps provider: Serper")
        return SerperMapsClient()
    if provider == "outscraper" or (provider == "auto" and CONFIG.outscraper_api_key):
        log.info("Maps provider: Outscraper")
        return OutscraperMapsClient()
    raise RuntimeError(
        "No Maps provider configured. Set SERPER_API_KEY (or OUTSCRAPER_API_KEY)."
    )
