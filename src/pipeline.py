"""End-to-end run: pick MSAs, scrape, qualify, write.

Flow per run:
  1. Load MSAs and skip ones already marked done (resume across runs).
  2. For each MSA (up to MAX_MSAS_PER_RUN):
       a. scrape Google Maps for HVAC businesses,
       b. drop companies already in the output sheet,
       c. qualify each with Claude,
       d. route: qualified -> Leads tab, review -> Needs Review tab, reject -> drop,
       e. checkpoint the MSA as done.
"""

from __future__ import annotations

import logging

from .config import CONFIG
from .enrich import Enricher
from .maps import get_maps_client
from .models import Lead, _domain
from .rocketreach import RocketReachClient
from .sheets import SheetsClient, place_dedupe_keys

log = logging.getLogger(__name__)


def _filter_obvious_noise(places):
    """Cheap pre-filter before spending model tokens.

    Skip records with no website AND no phone (almost never qualifiable), and
    obvious non-HVAC noise. The heavy lifting is still Claude's job.
    """
    kept = []
    for p in places:
        if not p.website and not p.phone:
            continue
        kept.append(p)
    return kept


def _augment_contact(rocket: RocketReachClient, place, qual) -> None:
    """Fill owner email/LinkedIn (and name/title if missing) from RocketReach."""
    domain = place.domain or _domain(place.website)
    contact = rocket.find(
        company=place.name,
        domain=domain,
        first=qual.owner_first_name,
        last=qual.owner_last_name,
        linkedin=qual.owner_linkedin,
    )
    if not contact:
        return
    if contact.get("email") and not qual.contact_email:
        qual.contact_email = contact["email"]
    if contact.get("linkedin") and not qual.owner_linkedin:
        qual.owner_linkedin = contact["linkedin"]
    if contact.get("first") and not qual.owner_first_name:
        qual.owner_first_name = contact["first"]
        qual.owner_last_name = contact.get("last", "")
    if contact.get("title") and not qual.owner_title:
        qual.owner_title = contact["title"]
    if contact.get("email"):
        log.info("RocketReach: found email for %s", place.name)


def run() -> None:
    CONFIG.validate()
    sheets = SheetsClient()
    maps = get_maps_client()
    enricher = Enricher()
    # RocketReach is optional and skipped on dry runs (it consumes credits).
    rocket = (
        RocketReachClient()
        if (CONFIG.rocketreach_api_key and not CONFIG.dry_run)
        else None
    )

    all_msas = sheets.read_msas()
    if not all_msas:
        log.warning("No MSAs found in the MSA sheet — nothing to do.")
        return

    done = sheets.completed_msas()
    pending = [j for j in all_msas if j.key not in done]
    log.info("MSAs: %d total, %d already done, %d pending", len(all_msas), len(done), len(pending))

    if CONFIG.dry_run:
        pending = pending[:1]
        log.info("DRY_RUN: limiting to 1 MSA, no sheet writes")
    else:
        pending = pending[: CONFIG.max_msas_per_run]

    if not pending:
        log.info("All MSAs complete. Nothing to process this run.")
        return

    existing = set() if CONFIG.dry_run else sheets.existing_keys()
    review_tab = None if CONFIG.dry_run else sheets.ensure_review_ws()

    for job in pending:
        log.info("=== Processing MSA: %s ===", job.query)
        places = _filter_obvious_noise(maps.search_msa(job.query, job.label))
        if CONFIG.dry_run:
            places = places[: CONFIG.dry_run_sample]
            log.info("DRY_RUN: limiting to %d companies", len(places))
        elif CONFIG.max_companies_per_msa:
            places = places[: CONFIG.max_companies_per_msa]

        qualified: list[Lead] = []
        review: list[Lead] = []
        errors = 0

        for place in places:
            keys = place_dedupe_keys(place)
            if any(k in existing for k in keys):
                log.info("Skip (already in sheet): %s", place.name)
                continue

            try:
                qual, dossier = enricher.qualify(place)
            except Exception as exc:  # noqa: BLE001 — never let one company kill the run
                errors += 1
                log.warning("Qualification failed for %s: %s", place.name, exc)
                continue

            # Verified owner email via RocketReach (kept leads only, to save credits).
            if rocket and qual.verdict in ("qualified", "review"):
                _augment_contact(rocket, place, qual)

            sources = qual.sources or []
            lead = Lead(place=place, qual=qual, sources_text="\n".join(sources))

            # Reserve dedupe keys immediately so duplicates within the same MSA
            # batch don't both get written.
            for k in keys:
                existing.add(k)

            if qual.verdict == "qualified":
                qualified.append(lead)
                log.info("QUALIFIED: %s (%s, %s)", place.name, qual.revenue_band, qual.ownership)
            elif qual.verdict == "review":
                review.append(lead)
                log.info("REVIEW: %s — %s", place.name, qual.summary[:80])
            else:
                log.info("reject: %s — %s", place.name, qual.summary[:80])

        if CONFIG.dry_run:
            log.info("DRY_RUN summary for %s: %d qualified, %d review (not written)",
                     job.query, len(qualified), len(review))
            continue

        sheets.append_leads(qualified)
        if review and review_tab:
            sheets.append_leads(review, worksheet=review_tab)

        # Don't burn a metro if every company errored (rate limits, etc.) —
        # leave it pending so a later run retries it.
        if errors and not qualified and not review:
            log.warning("MSA %s: all %d lookups errored — leaving pending for retry", job.query, errors)
        else:
            sheets.mark_msa_done(job.query, len(qualified))
            log.info("MSA %s done: %d qualified, %d to review", job.query, len(qualified), len(review))

    log.info("Run complete.")
