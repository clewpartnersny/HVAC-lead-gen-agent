"""Google Sheets I/O: read MSAs, read the Research Template, write leads.

Design choices:
- The output sheet's header row IS the schema. We read it at runtime and map our
  logical fields onto whatever columns exist, so the template can change without
  code edits. Unknown logical fields are dropped; unmatched columns stay blank.
- De-duplication reads existing rows once at startup and keys on website domain
  and company-name+state, so we never write the same company twice.
- Progress is checkpointed in a dedicated state tab (one row per processed MSA),
  so a run resumes where the last one stopped — important for GitHub Actions,
  which caps job length.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

import gspread
from google.oauth2.service_account import Credentials
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import CONFIG
from .models import Lead

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

# Logical field -> candidate header names (lowercased, alnum-only when matched).
# The writer picks the first template column whose normalized header matches any
# alias for a logical field.
FIELD_ALIASES: dict[str, list[str]] = {
    "company_name": ["company", "companyname", "name", "business", "businessname"],
    "website": ["website", "url", "web", "site", "domain"],
    "phone": ["phone", "phonenumber", "telephone", "tel"],
    "email": ["email", "emailaddress"],
    "address": ["address", "streetaddress", "fulladdress"],
    "city": ["city", "town"],
    "state": ["state", "province"],
    "zip": ["zip", "zipcode", "postalcode", "postal"],
    "msa": ["msa", "metro", "metroarea", "market", "region"],
    "google_maps_url": ["googlemapsurl", "mapsurl", "googleurl", "maplink", "googlemaps"],
    "rating": ["rating", "googlerating", "stars"],
    "review_count": ["reviews", "reviewcount", "numreviews"],
    "category": ["category", "type", "googlecategory"],
    "revenue_band": ["revenue", "revenueband", "estimatedrevenue", "revenueestimate", "size"],
    "revenue_rationale": ["revenuerationale", "revenuenotes", "revenuereasoning"],
    "ownership": ["ownership", "ownershipstatus", "independence", "independent", "owner"],
    "ownership_rationale": ["ownershiprationale", "ownershipnotes", "ownershipreasoning"],
    "parent_or_acquirer": ["parent", "acquirer", "parentcompany", "owner", "pefirm", "platform"],
    "service_pct": ["servicepct", "servicepercent", "serviceshare", "service"],
    "construction_pct": ["constructionpct", "constructionpercent", "constructionshare", "construction"],
    "residential": ["residential", "res"],
    "commercial": ["commercial", "comm"],
    "summary": ["summary", "notes", "description", "overview", "comments"],
    "confidence": ["confidence", "confidencescore"],
    "verdict": ["verdict", "status", "decision", "fit"],
    "sources": ["sources", "source", "citations", "links", "references"],
}


def _norm(header: str) -> str:
    return re.sub(r"[^a-z0-9]", "", header.lower())


class SheetsClient:
    def __init__(self):
        self.gc = self._authorize()

    @staticmethod
    def _authorize() -> gspread.Client:
        if CONFIG.google_sa_json:
            info = json.loads(CONFIG.google_sa_json)
            creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        else:
            creds = Credentials.from_service_account_file(CONFIG.google_sa_file, scopes=SCOPES)
        return gspread.authorize(creds)

    # --- MSA list -------------------------------------------------------------
    @retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=2, min=2, max=20))
    def read_msas(self) -> list[str]:
        """Return ordered MSA names from the configured MSA sheet/tab."""
        sh = self.gc.open_by_key(CONFIG.msa_sheet_id)
        ws = sh.worksheet(CONFIG.msa_worksheet)
        records = ws.get_all_records()  # list of dicts keyed by header row
        if not records:
            return []
        headers = list(records[0].keys())
        col = self._pick_msa_column(headers)
        log.info("Reading MSAs from column %r", col)
        seen, msas = set(), []
        for row in records:
            value = str(row.get(col, "")).strip()
            if value and value.lower() not in seen:
                seen.add(value.lower())
                msas.append(value)
        return msas

    @staticmethod
    def _pick_msa_column(headers: list[str]) -> str:
        if CONFIG.msa_name_column:
            return CONFIG.msa_name_column
        wanted = {"msa", "metro", "metroarea", "market", "cbsa", "name", "region", "city"}
        for h in headers:
            if _norm(h) in wanted:
                return h
        return headers[0]  # fall back to the first column

    # --- Output template ------------------------------------------------------
    def _output_ws(self) -> gspread.Worksheet:
        sh = self.gc.open_by_key(CONFIG.output_sheet_id)
        return sh.worksheet(CONFIG.output_worksheet)

    @retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=2, min=2, max=20))
    def _headers(self, ws: gspread.Worksheet) -> list[str]:
        headers = ws.row_values(1)
        if not headers:
            raise RuntimeError(
                f"Output worksheet '{ws.title}' has no header row. "
                "Add the Research Template column headers to row 1."
            )
        return headers

    def _build_column_map(self, headers: list[str]) -> dict[int, str]:
        """Map column index -> logical field name (best match)."""
        col_map: dict[int, str] = {}
        used_fields: set[str] = set()
        for idx, header in enumerate(headers):
            nh = _norm(header)
            for field, aliases in FIELD_ALIASES.items():
                if field in used_fields:
                    continue
                if nh in {_norm(a) for a in aliases} or nh == field:
                    col_map[idx] = field
                    used_fields.add(field)
                    break
        return col_map

    # --- De-duplication -------------------------------------------------------
    @retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=2, min=2, max=20))
    def existing_keys(self) -> set[str]:
        """Keys for companies already in the output sheet."""
        ws = self._output_ws()
        rows = ws.get_all_records()
        headers = self._headers(ws)
        col_map = self._build_column_map(headers)
        # invert: logical field -> header name
        field_to_header = {field: headers[idx] for idx, field in col_map.items()}
        keys: set[str] = set()
        for row in rows:
            name = str(row.get(field_to_header.get("company_name", ""), "")).strip()
            website = str(row.get(field_to_header.get("website", ""), "")).strip()
            state = str(row.get(field_to_header.get("state", ""), "")).strip()
            for k in _company_keys(name, website, state):
                keys.add(k)
        log.info("Loaded %d existing dedupe keys from output sheet", len(keys))
        return keys

    # --- Writing --------------------------------------------------------------
    @retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=2, min=2, max=20))
    def append_leads(self, leads: list[Lead], *, worksheet: str | None = None) -> None:
        if not leads:
            return
        sh = self.gc.open_by_key(CONFIG.output_sheet_id)
        ws = sh.worksheet(worksheet or CONFIG.output_worksheet)
        headers = self._headers(ws)
        col_map = self._build_column_map(headers)
        rows = []
        for lead in leads:
            fields = lead.as_field_map()
            row = [""] * len(headers)
            for idx, field in col_map.items():
                row[idx] = fields.get(field, "")
            rows.append(row)
        ws.append_rows(rows, value_input_option="USER_ENTERED")
        log.info("Appended %d rows to '%s'", len(rows), ws.title)

    # --- Progress state -------------------------------------------------------
    def _state_ws(self) -> gspread.Worksheet:
        sh = self.gc.open_by_key(CONFIG.output_sheet_id)
        try:
            return sh.worksheet(CONFIG.state_worksheet)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=CONFIG.state_worksheet, rows=2000, cols=4)
            ws.update("A1:D1", [["msa", "status", "leads_added", "completed_at"]])
            return ws

    @retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=2, min=2, max=20))
    def completed_msas(self) -> set[str]:
        ws = self._state_ws()
        records = ws.get_all_records()
        return {
            str(r.get("msa", "")).strip().lower()
            for r in records
            if str(r.get("status", "")).strip().lower() == "done"
        }

    @retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=2, min=2, max=20))
    def mark_msa_done(self, msa: str, leads_added: int) -> None:
        ws = self._state_ws()
        ws.append_row(
            [msa, "done", str(leads_added), datetime.now(timezone.utc).isoformat()],
            value_input_option="USER_ENTERED",
        )

    def ensure_review_ws(self) -> str:
        """Make sure the review tab exists and mirrors the output headers."""
        sh = self.gc.open_by_key(CONFIG.output_sheet_id)
        try:
            sh.worksheet(CONFIG.review_worksheet)
        except gspread.WorksheetNotFound:
            out = self._output_ws()
            headers = self._headers(out)
            ws = sh.add_worksheet(title=CONFIG.review_worksheet, rows=2000, cols=len(headers))
            ws.update("A1", [headers])
        return CONFIG.review_worksheet


def _company_keys(name: str, website: str, state: str) -> list[str]:
    keys = []
    domain = _domain(website)
    if domain:
        keys.append(f"domain:{domain}")
    if name:
        keys.append(f"name:{_norm(name)}|{_norm(state)}")
    return keys


def _domain(website: str) -> str:
    if not website:
        return ""
    w = website.lower().strip()
    w = re.sub(r"^https?://", "", w)
    w = re.sub(r"^www\.", "", w)
    return w.split("/")[0].strip()


def place_dedupe_keys(place) -> list[str]:
    """Keys used to test a scraped place against the existing set."""
    return _company_keys(place.name, place.website or place.domain, place.state)
