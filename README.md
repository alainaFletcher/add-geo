# Address Geocoding Application

An internal daily job that geocodes student and parent addresses from Infinite Campus (IC) using the [Geocodio](https://www.geocod.io/) API, then writes coordinates, 2025 census tract/block, county FIPS, and school district back to IC. Every Geocodio response is also stored in Snowflake as an audit trail.

Addresses are student PII (FERPA). The job never writes addresses, coordinates, credentials, or driver error text to its logs; see [Privacy](#privacy-ferpa).

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [Configuration](#configuration)
- [Running with Docker](#running-with-docker)
- [Running Locally](#running-locally)
- [Testing](#testing)
- [How Addresses Are Selected](#how-addresses-are-selected)
- [Pipeline Steps](#pipeline-steps)
- [Geocodio](#geocodio)
- [What Gets Written](#what-gets-written)
- [Logging and Alerts](#logging-and-alerts)
- [Exit Codes](#exit-codes)
- [Troubleshooting](#troubleshooting)
- [File Structure](#file-structure)

## Overview

Each run works in phases, not address by address:

1. Fetch up to `BATCH_SIZE` addresses from the Snowflake view `VIEW_ADDRESSESNEEDINGGEOCODED`
2. Geocode each address with one Geocodio call, then parse every response
3. Match each address's school district (LEA code) to an IC district ID
4. Insert every Geocodio response into the Snowflake audit table
5. Update the IC `ADDRESS` table for confident, house-level matches

## Architecture

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│                 │     │                 │     │                 │
│   Snowflake     │◄────│ GeocodeAddresses│────►│  Geocodio API   │
│   (read/write)  │     │  (Python, cron) │     │  (external)     │
│                 │     │                 │     │                 │
└─────────────────┘     └────────┬────────┘     └─────────────────┘
                                 │
                                 ▼
                        ┌─────────────────┐
                        │ Infinite Campus │
                        │  (SQL Server)   │
                        └─────────────────┘
```

| System | Purpose | Connection |
|--------|---------|------------|
| Snowflake | Address source (view), district lookup, response audit log, `app_logger` run logs | `snowflake-connector-python` 4.x, RSA key-pair auth |
| Infinite Campus | Target for geocoded address updates | `pyodbc` + ODBC Driver 18 |
| Geocodio | Geocoding, census, and school-district appends | REST (`requests`), API key in `Authorization: Bearer` header |

| Object | Where | Purpose |
|--------|-------|---------|
| `MPS_WAREHOUSE.DATALAKE.VIEW_ADDRESSESNEEDINGGEOCODED` | Snowflake | Addresses to geocode (see [How Addresses Are Selected](#how-addresses-are-selected)) |
| `MPS_WAREHOUSE.IC.DISTRICT` | Snowflake | NCES district ID → IC district ID lookup |
| `LOGGING_GEOCODIOAPI_ADDRESSLOOKUPS` | Snowflake (`SNOWFLAKE_DATABASE`.`SNOWFLAKE_SCHEMA`) | Audit log of every Geocodio response |
| `LOGS.DATALAKE.ETL_RUN_SUMMARIES` / `ETL_LOG_LINES` | Snowflake | `app_logger` run summaries and log lines |
| `ADDRESS` | Infinite Campus | Target table for updates |

## Prerequisites

**Docker (how production runs):** Docker Desktop and a configured `.env`.

**Local development:** Python 3.13 (matches the image), Microsoft ODBC Driver 18 for SQL Server and unixODBC, and network access to Snowflake and IC for real runs. Tests need none of the databases.

## Configuration

```bash
cp .env.example .env    # then fill in real values; never commit .env
```

`.env.example` documents every variable. The important ones:

| Variable | Default | Notes |
|----------|---------|-------|
| `SNOWFLAKE_USER`, `SNOWFLAKE_PRIVATE_KEY_B64` | (required) | Base64-encoded PEM; optional `SNOWFLAKE_PRIVATE_KEY_PASSPHRASE` |
| `IC_DB_USER`, `IC_DB_PASSWORD` | (required) | |
| `GEOCODIO_API_KEY` | (required) | |
| `GEOCODIO_API_VERSION` | `v2.1` | Bump here when Geocodio publishes a new version (the job logs a deprecation WARNING when it does) |
| `BATCH_SIZE` | `500` | Max addresses per run |
| `ACCURACY_THRESHOLD` | `0.8` | Minimum Geocodio accuracy score written to IC |
| `NOTIFICATION_*` | off | Email/Teams alerts from `app_logger`; see `.env.example` |
| `APP_LOG_ROOT` | `./logs` | docker-compose sets `/app/logs` |

**`.env` overrides every in-code default.** docker-compose loads it via `env_file:` and the script calls `load_dotenv()`. Changing a default in code does nothing if `.env` sets the variable; update `.env` too. Keep comments on their own lines (no trailing `# comment` after a value).

## Running with Docker

Production runs from the Mac mini's user crontab, daily at 09:50 local time:

```
50 9 * * * cd ~/Projects/Address-Geocoding && /usr/local/bin/docker-compose run --rm geocoding
```

The container runs once and exits; there is no long-running service.

```bash
docker compose build                                    # required after any code or requirements change
docker compose run --rm geocoding                       # one run
docker compose run --rm -e BATCH_SIZE=1 geocoding       # smoke test
```

- **Rebuild after every change.** `run --rm` reuses the existing `address-geocoding:latest` image; without `build`, the old code keeps running.
- **Every run writes to production IC.** There is no dry-run mode. A `BATCH_SIZE=1` smoke test updates at most one real address (often zero, if it scores below the threshold). Check the `RUN_END` line: `status`, `steps_total: 8`, `warning_count`.

## Running Locally

Prefer the Docker smoke test above. Running the script directly also writes to production IC and logs to `./logs`.

```bash
python3.13 -m venv .venv && source .venv/bin/activate
pip install vendor/app_logger-*.whl -r requirements.txt -r requirements-dev.txt
python GeocodeAddresses.py
```

On macOS, install the driver with `brew install microsoft/mssql-release/msodbcsql18 unixodbc`; the script auto-detects Homebrew's ODBC config.

## Testing

```bash
pip install vendor/app_logger-*.whl -r requirements.txt -r requirements-dev.txt
python -m pytest tests/ -q
```

Tests are offline and never touch Snowflake, IC, or Geocodio. `tests/conftest.py` disables `.env` loading (`PYTHON_DOTENV_DISABLED`) and sets fake credentials before the module is imported, so a developer's real `.env` (credentials, Teams webhook) is never read. `main()` tests use the real `AppLogger` writing to a temp directory with notifications off.

## How Addresses Are Selected

`VIEW_ADDRESSESNEEDINGGEOCODED` is maintained in Snowflake (not in this repo). It returns `ADDRESSID` and a formatted `ADDRESSLINE1` for addresses that are:

- the mailing address of a household with a student enrolled in the active school year
- in Colorado, not a PO box, with number, street, city, state, and zip present
- **and** either never looked up, modified in IC since the latest audit-log row, or still missing `location_code` or `districtid` in IC

The Snowflake copy of IC refreshes nightly, so each run sees the previous night's IC data. Any address the job doesn't fully write (score below threshold, not a house-level match, no census data, no district match) is selected and looked up again every run. That is intentional.

## Pipeline Steps

Each step is tracked by `app_logger`. If a fatal step fails, the run ends as `FAILED` (exit 1).

| Step | Fatal | Description |
|------|-------|-------------|
| `validate_config` | Yes | Required environment variables are set |
| `connect_snowflake` | Yes | Opens the main Snowflake connection |
| `fetch_addresses` | Yes | Reads the view (`SKIPPED` run if empty) |
| `fetch_districts` | Yes | Loads the district lookup |
| `geocode_addresses` | Yes | Calls Geocodio per address and parses results |
| `enrich_districts` | Yes | Matches LEA codes to IC district IDs |
| `log_to_snowflake` | **No** | Inserts raw responses into the audit table |
| `update_ic` | Yes | Writes results to IC |

## Geocodio

- **Endpoint:** `GET https://api.geocod.io/{GEOCODIO_API_VERSION}/geocode`, one call per address, with fields `school`, `zip4`, and `census2025`.
- **Auth:** API key in the `Authorization: Bearer` header, never in the URL (requests puts URLs into error messages).
- **Failure handling:**

  | Situation | Behavior |
  |-----------|----------|
  | HTTP 403 (invalid key or daily quota used up), 401, 429 (rate limit) | Stop geocoding at once |
  | 5 non-422 failures in a row (outage, network) | Stop geocoding |
  | Zero successful lookups and at least one non-422 failure (small-batch outage) | Stop geocoding |
  | 422 (address Geocodio can't parse) | That row fails; the run continues |
  | Isolated 5xx / timeout / bad response | That row fails; the run continues |

  When geocoding stops, the lookups already completed are still audited and written to IC, then the run ends `FAILED`. There is deliberately no automatic retry (urllib3's retry logging would leak the address URL); unsuccessful addresses are retried by the next run.

## What Gets Written

### Infinite Campus (`ADDRESS`)

A result is written only if **both**:

- accuracy ≥ `ACCURACY_THRESHOLD` (a missing score is never written), and
- `accuracy_type` is house-level: `rooftop`, `point`, `range_interpolation`, or `nearest_rooftop_match`. `intersection` and `street_center` only locate a street; `place`, `county`, and `state` mean Geocodio did not find the address.

| Column | Source |
|--------|--------|
| `latitude`, `longitude` | `results[0].location` |
| `tract` | `fields.census["2025"].tract_code` |
| `block` | `fields.census["2025"].block_code` |
| `location_code` | `fields.census["2025"].county_fips` |
| `districtID` | IC district matched from `fields.school_districts.unified.lea_code` via `MPS_WAREHOUSE.IC.DISTRICT.NCESDISTRICTID` |

These columns are owned by this job (users don't edit them), so the job writes exactly what the geocode says. Missing census values are written as NULL (with a WARNING), and an unmatched district writes `districtID = NULL`. Values from a previous location are never kept. District matching ignores `'0000'` (no unified district returned) and any NCES ID that maps to more than one IC district (logged as a WARNING).

### Snowflake audit (`LOGGING_GEOCODIOAPI_ADDRESSLOOKUPS`)

| Column | Description |
|--------|-------------|
| `IC_ADDRESSID` | IC address ID |
| `GEOCODIO_JSON` | Full Geocodio response (`VARIANT`) |

Every response received is inserted, including no-result and low-accuracy responses. Requests that got no response (network errors, aborted batch) are not. This step is non-fatal, so an audit failure leaves the run `PARTIAL` and IC updates still happen. The view uses this table's latest row per address to decide what's already been looked up.

## Logging and Alerts

The job uses the vendored `app_logger` (`vendor/app_logger-*.whl`, not on PyPI) for structured NDJSON logs:

| Output | Location | Retention |
|--------|----------|-----------|
| Main log | `$APP_LOG_ROOT/Address_Geocoding/daily_geocode_YYYYMMDD.log` | 90 days |
| Errors-only log | `$APP_LOG_ROOT/Address_Geocoding/daily_geocode_errors_YYYYMMDD.log` | 180 days |
| Run index (one line per run) | `$APP_LOG_ROOT/_index/runs_YYYYMM.jsonl` | 365 days |
| Snowflake (best effort) | `LOGS.DATALAKE.ETL_RUN_SUMMARIES`, `ETL_LOG_LINES` | n/a |
| Console | stdout, JSON in Docker (`CONSOLE_FORMAT=json`) | Docker json-file (10 MB × 3) |

In Docker, `APP_LOG_ROOT=/app/logs` is mounted from `../LogReader/logs`, which the LogReader dashboard reads.

**Alerts:** when notifications are enabled, `app_logger` sends an email/Teams digest at the end of a run if the run `FAILED` or any line is at or above `NOTIFICATION_LEVEL` (default `WARNING`). Any WARNING therefore alerts. Expected per-address WARNINGs include "No district match", "Invalid geocoding request", and "Incomplete census data".

### Privacy (FERPA)

Logs carry address IDs, counts, error types, and error codes only. The code guarantees this by:

- never putting `requests`, urllib3, pyodbc, or Snowflake exception text in log messages (error type plus errno/SQLSTATE only)
- re-raising errors that escape a step as sanitized exceptions (`from None`)
- keeping urllib3 logging at ERROR (its WARNINGs include the request URL)

`CLAUDE.md` lists the rules for anyone changing the code. The audit table, by design, stores full addresses and coordinates.

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | `SUCCESS`, `PARTIAL` (some rows or the non-fatal audit step failed), or `SKIPPED` (nothing to geocode) |
| 1 | `FAILED`: a fatal step failed or Geocodio stopped the batch |
| 143 | Terminated by SIGTERM (run recorded `FAILED`) |

The real outcome, including `PARTIAL`, is in the run index and `RUN_END` line.

## Troubleshooting

**A run is `FAILED`.** Open the errors log or the run index entry; `failed_steps` names the step and a sanitized error.

| `error_type` / message | Likely cause |
|------------------------|--------------|
| `GeocodioFatalError ... (HTTP 403)` | Invalid/revoked API key or daily quota used up |
| `GeocodioFatalError ... (HTTP 429)` | Pay-as-you-go rate limit |
| `GeocodioFatalError ... consecutive ...` or `No successful Geocodio lookups` | Geocodio outage or network problem |
| `ConnectionError: Infinite Campus connection failed ... (SQLSTATE 28000)` | IC login failed (credentials) |
| `ConnectionError: Snowflake connection failed ... (errno ...)` | Snowflake auth or network; look up the errno |
| `RuntimeError: Snowflake ... fetch failed ...` | View or district query failed (permissions, object missing) |

**Deprecation WARNING every run** (`Geocodio API deprecation notice received`): Geocodio released a newer API version. Read their migration notes, then raise `GEOCODIO_API_VERSION` in `.env` (and the default in code and `.env.example`).

**Many addresses skipped.** Below-threshold or street-level matches are skipped on purpose; DEBUG lines in the main log give each address ID and reason. Fix the address data in IC rather than lowering `ACCURACY_THRESHOLD`.

**ODBC driver not found (macOS):** `Can't open lib 'ODBC Driver 18 for SQL Server'` means the driver isn't installed: `brew install microsoft/mssql-release/msodbcsql18`.

**Snowflake connector pinned below 5.** 5.x logs a proxy WARNING on every connection (an alert every run). Upgrade deliberately, with that logger handled.

## File Structure

```
Address-Geocoding/
├── GeocodeAddresses.py     # The job
├── tests/                  # Offline pytest suite (+ fixtures/ of captured Geocodio responses)
├── vendor/                 # app_logger wheel (not on PyPI)
├── docs/superpowers/       # Design docs and plans for past upgrades (historical)
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── requirements-dev.txt
├── .env.example            # Configuration template (.env itself is git-ignored)
├── CLAUDE.md               # Guidance for AI coding agents, incl. FERPA coding rules
└── README.md
```
