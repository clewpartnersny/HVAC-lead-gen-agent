# HVAC Lead-Gen Agent

An autonomous agent that finds **independent, service-focused HVAC companies
($5M+ revenue)** across US metros and writes qualified leads into a Google Sheet
shaped like your *Research Template*. Built to run **24/7 on GitHub Actions** —
no computer required.

## What it does

For each MSA (metro) in your *Organized MSAs* sheet, the agent:

1. **Scrapes Google Maps** (via Outscraper) for HVAC businesses.
2. **Qualifies each company with Claude**, which runs live web searches to judge:
   - **Independence** — rejects PE-owned, acquired, subsidiary, public, and
     franchise companies (your PE clients want un-acquired targets).
   - **Size** — estimates revenue; targets ~$5M+.
   - **Service vs. construction** — keeps service-focused firms; rejects
     majority-new-construction shops (some construction exposure is fine).
   - **End markets** — residential and/or commercial.
3. **Writes results** to your output sheet:
   - `qualified` → the **Leads** tab (mapped to your Research Template columns).
   - `review` (genuinely ambiguous) → the **Needs Review** tab.
   - `reject` → dropped.
4. **Checkpoints progress** per MSA, so runs resume where the last one stopped.

> **Why qualification needs web search:** Google Maps only gives name, address,
> phone, website, category, and reviews. Revenue, ownership, and service mix are
> *not* in Maps data — the agent infers them from the open web (company site,
> LinkedIn, news, PE portfolio pages). Treat the output as a high-quality
> first pass; the **Needs Review** tab is where uncertain calls land instead of
> being silently dropped.

## Architecture

```
GitHub Actions (cron)
        │
        ▼
   main.py ──► src/pipeline.py
                 │   ├─ src/sheets.py   read MSAs · read template · dedupe · write · checkpoint
                 │   ├─ src/maps.py     Outscraper Google Maps scrape
                 │   └─ src/enrich.py   Claude: web-search research → structured qualification
                 ▼
        Google Sheets (Leads · Needs Review · _agent_state)
```

## Run it yourself (default — no service account, no admin)

This path runs the agent **on your own machine, on demand**, and signs in with
your own Google account via a browser ("Allow" once, cached after). No
service-account key, no GitHub secrets, no Google org-policy changes.

### 1. Prepare your sheets

- **Organized MSAs sheet:** a tab named `Organized MSAs` (configurable) whose
  rows list metros. The agent auto-detects the metro column (or set
  `MSA_NAME_COLUMN`).
- **Output sheet:** a tab named `Leads` (configurable) whose **row 1 contains
  your Research Template column headers**. The agent maps its fields onto your
  columns by fuzzy-matching the headers (see `FIELD_ALIASES` in `src/sheets.py`),
  so you don't have to match our names exactly. The `Needs Review` and
  `_agent_state` tabs are created automatically.

Grab each spreadsheet's ID from its URL:
`https://docs.google.com/spreadsheets/d/`**`THIS_IS_THE_ID`**`/edit`.

### 2. Create an OAuth client (one-time, ~3 minutes, not blocked by the key policy)

1. Enable the APIs (if not already): open
   [Google Sheets API](https://console.cloud.google.com/apis/library/sheets.googleapis.com)
   → **Enable**, and
   [Google Drive API](https://console.cloud.google.com/apis/library/drive.googleapis.com)
   → **Enable**.
2. Configure the consent screen:
   [console.cloud.google.com/apis/credentials/consent](https://console.cloud.google.com/apis/credentials/consent)
   → **User Type: Internal** (since you're on a Workspace domain) → fill app name
   and your email → **Save**.
3. Create the client:
   [console.cloud.google.com/apis/credentials](https://console.cloud.google.com/apis/credentials)
   → **Create credentials** → **OAuth client ID** → **Application type: Desktop
   app** → **Create** → **Download JSON**.
4. Save that file in the project folder as **`client_secret.json`**.

### 3. API keys

- **Anthropic** — qualification (`ANTHROPIC_API_KEY`).
- **Outscraper** — Google Maps data (`OUTSCRAPER_API_KEY`), from
  [outscraper.com](https://outscraper.com).

### 4. Run it

```bash
pip install -r requirements.txt
cp .env.example .env          # fill in ANTHROPIC_API_KEY, OUTSCRAPER_API_KEY,
                              # MSA_SHEET_ID, OUTPUT_SHEET_ID

DRY_RUN=1 python main.py      # first run: a browser opens — sign in & click Allow
                              # (one MSA, verbose, no sheet writes)

python main.py                # real run: processes MAX_MSAS_PER_RUN metros
```

The first run pops a browser to authorize and caches the token to `token.json`;
later runs reuse it silently. Re-run `python main.py` whenever you want another
batch — it resumes from where it left off.

> Both `client_secret.json` and `token.json` are git-ignored — they never get
> committed.

## Later: unattended 24/7 on GitHub Actions

When you're ready to have it run while your computer is off, switch from OAuth to
a **service account** and let the included workflow run it on a schedule.

1. Create a service account + JSON key (needs the org's key-creation policy to
   allow it — see the project chat for the exact admin steps), and **share both
   sheets** with the service account's email (output sheet = **Editor**).
2. In the repo: **Settings → Secrets and variables → Actions**, add **Secrets**:

   | Secret | Value |
   | --- | --- |
   | `ANTHROPIC_API_KEY` | your Anthropic key |
   | `OUTSCRAPER_API_KEY` | your Outscraper key |
   | `GOOGLE_SERVICE_ACCOUNT_JSON` | the **entire** service-account JSON |
   | `MSA_SHEET_ID` | ID of the *Organized MSAs* spreadsheet |
   | `OUTPUT_SHEET_ID` | ID of the output spreadsheet |

   Optionally add **Variables** for tuning: `MAX_MSAS_PER_RUN`, `SEARCH_TERMS`,
   `MIN_REVENUE_USD`, `MSA_WORKSHEET`, `OUTPUT_WORKSHEET`, `ANTHROPIC_MODEL`.

The workflow (`.github/workflows/run-agent.yml`) runs **every 2 hours**, processes
`MAX_MSAS_PER_RUN` metros, and checkpoints. Disable it in the Actions tab to
pause; trigger a manual run with `dry_run=1` to test.

## Tuning

All knobs live in `.env.example` / the workflow env. Most-used:

| Setting | Default | Effect |
| --- | --- | --- |
| `MIN_REVENUE_USD` | `5000000` | Revenue floor used in qualification prompt. |
| `MAX_MSAS_PER_RUN` | `2` | Metros processed per scheduled run (controls pace). |
| `RESULTS_PER_QUERY` | `200` | Max Maps results per search term per MSA. |
| `SEARCH_TERMS` | HVAC contractor, … | Search phrases run against each MSA. |
| `ANTHROPIC_MODEL` | `claude-opus-4-8` | Qualification model. |
| `DRY_RUN` | `0` | `1` = one MSA, no writes. |

## Costs (rough)

Per company you pay one Outscraper result + ~2 Claude calls (one web-search
research turn + one extraction). Most of the spend is the research turn. Keep an
eye on it for the first few MSAs and adjust `RESULTS_PER_QUERY` / `SEARCH_TERMS`
to trade coverage for cost.

## Notes & limitations

- Revenue and ownership are **inferred**, not authoritative. The `confidence`
  and `sources` columns, plus the **Needs Review** tab, exist so a human can
  spot-check. If you have a paid data provider (ZoomInfo, PitchBook, Apollo),
  it can be wired into `src/enrich.py` later for harder numbers.
- De-duplication keys on website domain and company-name+state, so re-runs and
  overlapping metros won't write the same company twice.
- If the Outscraper Python SDK on your version names the blocking flag
  differently (`async_request` vs `async_`), adjust the call in `src/maps.py`.
