# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a Python-based geocoding application that processes student and parent addresses from Infinite Campus (IC) using the Geocode.io API. The script geocodes addresses, enriches them with census and school district data, and writes the results back to both Infinite Campus and Snowflake databases.

## Architecture

### Main Script: GeocodeAddresses.py

The application follows a single-script architecture with three primary database connections:

1. **Infinite Campus (IC) Database** - Microsoft SQL Server connection via pyodbc
   - Source of addresses to geocode (queried via Snowflake view)
   - Target for updating latitude, longitude, tract, block, and district information

2. **Snowflake Data Warehouse** - Primary data source and audit logging
   - Queries `MPS_WAREHOUSE.DATALAKE.VIEW_ADDRESSESNEEDINGGEOCODED` for addresses needing geocoding (limited to top 500)
   - Queries `MPS_WAREHOUSE.IC.DISTRICT` for district ID lookups
   - Logs all Geocode.io API responses to `LOGGING_GEOCODIOAPI_ADDRESSLOOKUPS` as JSON

3. **Geocode.io API** - External geocoding service
   - Requests include fields: school districts, zip+4, census 2025 data (`API_FIELDS`)
   - Returns lat/lng coordinates, census tract/block, county FIPS, and school district LEA codes

### Logging

The application uses `app_logger` for structured NDJSON logging. The `app_logger` wheel is vendored in `vendor/` (not on PyPI).

- **AppLogger** is initialized in `main()` with app name `"Address_Geocoding"` and job `"daily_geocode"`
- **Helper functions** use `logging.getLogger(__name__)` — `configure_root_logger=True` gives them NDJSON output automatically
- **NDJSON log files** are written to the shared LogReader volume at `../LogReader/logs/Address_Geocoding/`
- **Snowflake audit logging** (raw Geocodio JSON responses) is separate and remains as-is

#### Pipeline Steps

| Step | Fatal | Description |
|------|-------|-------------|
| `validate_config` | Yes | Checks required env vars and API key |
| `connect_snowflake` | Yes | Opens the main Snowflake connection |
| `fetch_addresses` | Yes | Queries Snowflake for addresses needing geocoding |
| `fetch_districts` | Yes | Loads district lookup table from Snowflake |
| `geocode_addresses` | Yes | Calls Geocodio API per address, parses results |
| `enrich_districts` | Yes | Matches LEA codes to IC district IDs |
| `log_to_snowflake` | **No** | Logs raw API responses to Snowflake (audit trail) |
| `update_ic` | Yes | Writes geocoded data back to Infinite Campus |

### Data Flow

1. Fetch up to 500 addresses from Snowflake view
2. For each address:
   - Call Geocode.io API with address and requested fields
   - Log full JSON response to Snowflake
   - If accuracy >= 0.8, update IC database with coordinates and census data
   - Match LEA code to district ID using the district lookup dataframe

### Configuration

All credentials are provided via environment variables (see `.env.example`):
- **Snowflake**: RSA key pair authentication (`SNOWFLAKE_PRIVATE_KEY_B64` — base64-encoded PEM)
- **Infinite Campus**: Username/password via `IC_DB_USER` / `IC_DB_PASSWORD`
- **Geocod.io**: API key via `GEOCODIO_API_KEY`
- **Logging**: `APP_LOG_ROOT` (default `/app/logs`), `CONSOLE_FORMAT` (`json` in Docker)

## Running the Application

```bash
python GeocodeAddresses.py
```

The script has no command-line arguments. It processes up to 500 addresses per run. Runs daily at 10am Mountain (cron: `0 17 * * *` UTC).

## Key Implementation Details

- **Accuracy threshold**: Only addresses with accuracy >= 0.8 are written to IC database
- **District ID matching**: LEA codes from Geocode.io are matched against `NCESDISTRICTID` in the district table
- **Error handling**: `main()` owns the run lifecycle; the steps live in `run_pipeline()`. Any exception escaping `run_pipeline()` forces run status `FAILED` and exit 1 — app_logger on its own only reports `FAILED` when *every* step failed, otherwise `PARTIAL`, which `main()` maps to exit 0. `log_to_snowflake` is non-fatal (and does its own `commit()`), so IC updates still happen. Closing connections at teardown is best-effort (`_close_quietly`). SIGTERM keeps exit code 143.
- **Geocodio failures**: `GeocodioFatalError` ends geocoding on 401/403/429 (bad key, quota exhausted, rate limit), after `MAX_CONSECUTIVE_GEOCODE_FAILURES` (5) non-422 failures in a row, or when no lookup succeeded and any failed for a non-422 reason (small-batch outage). Its `results` carries the lookups completed before the abort; `run_pipeline()` still parses, audits, and writes those to IC, marks `geocode_addresses` FAILED, then re-raises so the run ends `FAILED`. A 422 (unparseable address) is per-row and neutral to the breaker. Per-row failures are retried on the next run.
- **Geocodio auth**: API key is sent as `Authorization: Bearer`, never as the `api_key` query parameter
- **Batch size**: Configurable via `BATCH_SIZE` env var (default 500)
- **Census data**: Uses 2025 census tract, block, and county FIPS codes (`census2025` field; parser reads `fields.census['2025']`)
- **FERPA**: No student addresses, coordinates, or PII are written to log files. Rules that keep it true:
  - Never interpolate text from `requests`, pyodbc, or Snowflake-connector exceptions (`{e}`, `str(e)`, `e.msg`) into log messages; use `_describe_db_error(e)` (type + errno/SQLSTATE) or `type(e).__name__`. `requests` errors embed the URL (`q=<address>`), pyodbc errors name the login, Snowflake errors can echo bound row data.
  - When such an exception would escape a `log.step()`, re-raise a sanitized one `from None` (or raise outside the `except` block) — app_logger records `str(exc)` and the full chained traceback to NDJSON, Snowflake LOGS, and email/Teams notifications, and its redaction does not catch addresses. See `GeocodioFatalError`, the `ConnectionError`s in the connection helpers, and the `RuntimeError`s in the fetch helpers.
  - urllib3 WARNINGs include the full URL (e.g. "Failed to parse headers (url=...)" on a malformed response header; `Retry`'s "Retrying ... url"). `create_geocodio_session()` raises the `urllib3` loggers to ERROR (urllib3 logs nothing at ERROR+). Don't lower them, and don't add a urllib3 `Retry`.
  - Before `AppLogger()` exists, log via the module `logger`, never `logging.warning()` — with no root handlers the latter calls `basicConfig()`, leaving a raw stderr handler that bypasses app_logger redaction for the whole run.
  - `main()` sets `__suppress_context__` on `BaseException`s (Ctrl-C/SIGTERM) so an interrupt inside an `except` block (retry sleep, requests internals) doesn't print the chained driver error or URL.

## Testing

```bash
pip install vendor/app_logger-*.whl -r requirements.txt -r requirements-dev.txt
python -m pytest tests/ -q
```

Tests are offline. `tests/conftest.py` disables `.env` loading (`PYTHON_DOTENV_DISABLED`) and sets fake credentials before `GeocodeAddresses` is imported. `main()` tests use the real `AppLogger` redirected to `tmp_path` with `notify=False` — never let a test construct an AppLogger with notifications on (real `.env` has a Teams webhook).

## Docker

- **Dockerfile**: Python 3.13-slim with ODBC Driver 18, vendored app_logger wheel
- **Volume**: `../LogReader/logs:/app/logs` — shared with LogReader dashboard
- **Non-root**: Runs as `appuser`
- **Exec-form CMD**: Ensures SIGTERM reaches Python for graceful shutdown

## Database Connections

### Infinite Campus Connection
- Server: `icampus.mapleton.us,7771`
- Database: `mapleton`
- Driver: ODBC Driver 18 for SQL Server
- Trust server certificate: Yes

### Snowflake Connection
- Account: `dv56305.west-us-2.azure`
- Warehouse: `dataloadingonly`
- Database: `MPS_Warehouse`
- Schema: `datalake`
- Authentication: RSA key pair (private key passed as base64-encoded PEM in `SNOWFLAKE_PRIVATE_KEY_B64`)

## Dependencies

See `requirements.txt`. Key packages:
- `app_logger` (vendored wheel in `vendor/`)
- `requests`
- `pyodbc`
- `snowflake-connector-python[pandas]`
- `pandas`
- `python-dotenv`
- `cryptography`
