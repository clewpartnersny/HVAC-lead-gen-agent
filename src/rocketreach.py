"""RocketReach contact lookup — find a verified owner email.

Used after Claude identifies the owner/principal. Two modes:

- If we already have an owner name → look them up at the company directly.
- If not → search the company for an owner-level title, take the top hit, then
  enrich it.

RocketReach lookups can be asynchronous (``status: searching``), so we poll
briefly until the profile is ``complete``. Every method is defensive: any error
or miss returns an empty dict so the pipeline never breaks over a contact lookup.

Field names follow the RocketReach API v2. If your account returns slightly
different keys, adjust ``_parse`` — nothing else needs to change.
"""

from __future__ import annotations

import logging
import time

import requests

from .config import CONFIG

log = logging.getLogger(__name__)

BASE = "https://api.rocketreach.co/api/v2"
OWNER_TITLES = ["Owner", "President", "CEO", "Founder", "Principal", "Partner", "General Manager"]


class RocketReachClient:
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or CONFIG.rocketreach_api_key
        self._cache: dict[str, dict] = {}

    @property
    def _headers(self) -> dict:
        return {"Api-Key": self.api_key, "Content-Type": "application/json"}

    def _lookup(self, params: dict) -> dict:
        r = requests.get(f"{BASE}/person/lookup", headers={"Api-Key": self.api_key}, params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def _search(self, payload: dict) -> dict:
        r = requests.post(f"{BASE}/person/search", headers=self._headers, json=payload, timeout=30)
        r.raise_for_status()
        return r.json()

    def _poll(self, profile_id, tries: int = 5, delay: int = 4) -> dict:
        data: dict = {}
        for _ in range(tries):
            data = self._lookup({"id": profile_id})
            if str(data.get("status", "")).lower() in ("complete", "failed", ""):
                return data
            time.sleep(delay)
        return data

    def find(self, company: str, domain: str = "", first: str = "", last: str = "", linkedin: str = "") -> dict:
        """Return {email, phone, linkedin, first, last, title} (any may be empty).

        Strategy: LinkedIn URL lookup if we have one (most accurate); otherwise
        SEARCH (fuzzy, no credit cost) to resolve a profile id, then look that
        id up. Avoids the brittle name+employer GET lookup that 404s easily.
        """
        if not self.api_key or not company:
            return {}
        cache_key = f"{first}|{last}|{linkedin}|{domain or company}".lower()
        if cache_key in self._cache:
            return self._cache[cache_key]

        result: dict = {}
        try:
            data = None
            if linkedin:
                try:
                    data = self._lookup({"linkedin_url": linkedin})
                except Exception:  # noqa: BLE001 — fall back to search on a miss
                    data = None

            if data is None:
                if first and last:
                    query = {"name": [f"{first} {last}"], "current_employer": [company]}
                else:
                    query = {"current_employer": [company], "current_title": OWNER_TITLES}
                res = self._search({"query": query, "page_size": 5})
                profiles = res.get("profiles") or []
                if not profiles:
                    self._cache[cache_key] = {}
                    return {}
                data = self._lookup({"id": profiles[0].get("id")})

            if str(data.get("status", "")).lower() not in ("complete", ""):
                data = self._poll(data.get("id"))
            result = self._parse(data)
        except Exception as exc:  # noqa: BLE001 — never break the run over a lookup
            log.warning("RocketReach lookup failed for %s: %s", company, exc)
            result = {}

        self._cache[cache_key] = result
        return result

    @staticmethod
    def _parse(data: dict) -> dict:
        if not data:
            return {}
        email = data.get("recommended_email") or data.get("current_work_email") or ""
        if not email:
            emails = data.get("emails") or []
            # Prefer professional, then by grade (A best). Grade may be absent.
            def rank(e):
                grade = (e.get("grade") or "Z").upper()
                is_pro = 0 if (e.get("type") == "professional") else 1
                return (is_pro, grade)
            for e in sorted(emails, key=rank):
                if e.get("email"):
                    email = e["email"]
                    break
        phones = data.get("phones") or []
        phone = ""
        for p in phones:
            phone = p.get("number") or p.get("phone") or ""
            if phone:
                break
        return {
            "email": email or "",
            "phone": phone or "",
            "linkedin": data.get("linkedin_url", "") or "",
            "first": data.get("first_name", "") or "",
            "last": data.get("last_name", "") or "",
            "title": data.get("current_title", "") or "",
        }
