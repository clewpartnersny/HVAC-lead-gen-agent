"""Central configuration, loaded from environment variables.

Every tunable lives here so the rest of the code never reads ``os.environ``
directly. Values come from the process environment, which is populated either
by a local ``.env`` file (via python-dotenv) or by GitHub Actions secrets.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()  # no-op in CI where vars are already in the environment


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, "1" if default else "0").strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


def _list(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return [part.strip() for part in raw.split(",") if part.strip()]


@dataclass
class Config:
    # --- API keys ---
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    outscraper_api_key: str = field(default_factory=lambda: os.getenv("OUTSCRAPER_API_KEY", ""))

    # --- Google auth ---
    # Service account (used for unattended 24/7 runs). Optional.
    google_sa_json: str = field(default_factory=lambda: os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", ""))
    google_sa_file: str = field(default_factory=lambda: os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", ""))
    # OAuth "sign in with Google" (used for local, run-it-yourself runs).
    oauth_client_file: str = field(default_factory=lambda: os.getenv("GOOGLE_OAUTH_CLIENT_FILE", "client_secret.json"))
    oauth_token_file: str = field(default_factory=lambda: os.getenv("GOOGLE_OAUTH_TOKEN_FILE", "token.json"))

    # --- Sheets ---
    msa_sheet_id: str = field(default_factory=lambda: os.getenv("MSA_SHEET_ID", ""))
    msa_worksheet: str = field(default_factory=lambda: os.getenv("MSA_WORKSHEET", "Organized MSAs"))
    msa_name_column: str = field(default_factory=lambda: os.getenv("MSA_NAME_COLUMN", ""))

    output_sheet_id: str = field(default_factory=lambda: os.getenv("OUTPUT_SHEET_ID", ""))
    output_worksheet: str = field(default_factory=lambda: os.getenv("OUTPUT_WORKSHEET", "Leads"))
    state_worksheet: str = field(default_factory=lambda: os.getenv("STATE_WORKSHEET", "_agent_state"))
    review_worksheet: str = field(default_factory=lambda: os.getenv("REVIEW_WORKSHEET", "Needs Review"))

    # --- Tuning ---
    min_revenue_usd: int = field(default_factory=lambda: _int("MIN_REVENUE_USD", 5_000_000))
    max_msas_per_run: int = field(default_factory=lambda: _int("MAX_MSAS_PER_RUN", 2))
    results_per_query: int = field(default_factory=lambda: _int("RESULTS_PER_QUERY", 200))
    search_terms: list[str] = field(
        default_factory=lambda: _list(
            "SEARCH_TERMS",
            ["HVAC contractor", "heating and air conditioning", "HVAC service"],
        )
    )
    anthropic_model: str = field(default_factory=lambda: os.getenv("ANTHROPIC_MODEL", "claude-opus-4-8"))
    dry_run: bool = field(default_factory=lambda: _bool("DRY_RUN", False))

    def validate(self) -> None:
        """Raise a clear error if a required setting is missing."""
        missing = []
        if not self.anthropic_api_key:
            missing.append("ANTHROPIC_API_KEY")
        if not self.outscraper_api_key:
            missing.append("OUTSCRAPER_API_KEY")
        # Google auth is validated lazily in SheetsClient (OAuth or service
        # account), so it is intentionally not required here.
        if not self.msa_sheet_id:
            missing.append("MSA_SHEET_ID")
        if not self.output_sheet_id:
            missing.append("OUTPUT_SHEET_ID")
        if missing:
            raise RuntimeError(
                "Missing required configuration: " + ", ".join(missing) +
                ".\nSee .env.example for the full list."
            )


CONFIG = Config()
