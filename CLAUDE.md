# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A daily Python job that geocodes student and parent addresses (FERPA PII) from Infinite Campus (IC) using the Geocodio API, enriches them with 2025 census and school district data, writes the results back to IC, and stores every raw Geocodio response in a Snowflake audit table. `README.md` is the operator-facing reference; this file is for agents changing the code.

## Architecture

### Main Script: GeocodeAddresses.py

The application follows a single-script architecture with three primary database connections:

1. **Infinite Campus (IC) Database** - Microsoft SQL Server connection via pyodbc
   - Source of addresses to geocode (queried via Snowflake view)
   - Target for updating latitude, longitude, tract, block, and district information

2. **Snowflake Data Warehouse** - Primary data source and audit logging
   - Queries `MPS_WAREHOUSE.DATALAKE.VIEW_ADDRESSESNEEDINGGEOCODED` for addresses needing geocoding (`TOP BATCH_SIZE`, default 500)
   - Queries `MPS_WAREHOUSE.IC.DISTRICT` for district ID lookups
   - Logs every Geocodio response received to `LOGGING_GEOCODIOAPI_ADDRESSLOOKUPS` as JSON (the view reads it back)

3. **Geocodio API** (`GEOCODIO_API_VERSION`, default `v2.1`) - External geocoding service
   - Requests include fields: school districts, zip+4, census 2025 data (`API_FIELDS`)
   - Returns lat/lng coordinates, census tract/block, county FIPS, and school district LEA codes

### Logging

The application uses `app_logger` for structured NDJSON logging. The `app_logger` wheel is vendored in `vendor/` (not on PyPI).

- **AppLogger** is initialized in `main()` with app name `"Address_Geocoding"` and job `"daily_geocode"`
- **Helper functions** use `logging.getLogger(__name__)` — `configure_root_logger=True` gives them NDJSON output automatically
- **NDJSON log files** go to `$APP_LOG_ROOT/Address_Geocoding/` (`daily_geocode_YYYYMMDD.log`, `daily_geocode_errors_YYYYMMDD.log`) plus the run index `$APP_LOG_ROOT/_index/runs_YYYYMM.jsonl`; in Docker that's the shared LogReader volume `../LogReader/logs`. `main()` also passes a `LOGS` Snowflake connection for best-effort dual-write to `LOGS.DATALAKE.ETL_RUN_SUMMARIES` / `ETL_LOG_LINES`, and `notify=True` sends email/Teams digests when a run FAILS or logs anything at or above `NOTIFICATION_LEVEL` (default WARNING)
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

The pipeline runs in phases, not per address:

1. Fetch up to `BATCH_SIZE` addresses from `VIEW_ADDRESSESNEEDINGGEOCODED`
2. Geocode every address (one Geocodio call each), then parse all responses
3. Match LEA codes to IC district IDs
4. Insert every raw response into the Snowflake audit table (non-fatal)
5. Update IC for results that pass the write policy below

**Address selection (the view, maintained in Snowflake, not in this repo):** active-year students' mailing households, Colorado, non-PO-box, complete street address; and either never looked up, modified in IC since the latest audit row (`ad.modifieddate > geo.datestamp`), or still missing `location_code` or `districtid` in IC. The Snowflake IC copy refreshes nightly. Consequence: any address the job does not fully write (score below threshold, not house-level, no census, no district match) is selected and looked up again **every run**. This is accepted by design; don't "fix" it in code.

### Configuration

All credentials are provided via environment variables (see `.env.example`):
- **Snowflake**: RSA key pair authentication (`SNOWFLAKE_PRIVATE_KEY_B64` — base64 of the whole PEM file, or the PEM body alone, i.e. base64 DER; `load_snowflake_private_key` detects which by the `-----BEGIN` line)
- **Infinite Campus**: Username/password via `IC_DB_USER` / `IC_DB_PASSWORD`
- **Geocodio**: API key via `GEOCODIO_API_KEY`; version via `GEOCODIO_API_VERSION`
- **Logging**: `APP_LOG_ROOT` (app_logger default `./logs`; docker-compose sets `/app/logs`), `CONSOLE_FORMAT` (`json` in Docker)
- **`.env` overrides every in-code default** (docker-compose `env_file:` and `load_dotenv()`). When you change a default, update `.env.example` and tell the user to update their `.env`.

## Running the Application

Production runs from the host's user crontab at 09:50 local time: `docker-compose run --rm geocoding` (one-shot container, no arguments). The `schedule="0 17 * * *"` passed to `log.start()` is stale metadata, not the trigger.

**Never run `python GeocodeAddresses.py` (or the container) to verify a change** without the user's go-ahead: every run writes to production IC; there is no dry-run. The user's smoke test is `docker compose build && docker compose run --rm -e BATCH_SIZE=1 geocoding` (`run --rm` reuses the existing image, so the build is required).

## Key Implementation Details

- **IC write policy** (`update_ic_addresses`): a result is written only if accuracy >= `ACCURACY_THRESHOLD` (0.8) **and** `accuracy_type` is in `HOUSE_LEVEL_ACCURACY_TYPES` (rooftop, point, range_interpolation, nearest_rooftop_match). A missing (NaN/None) score is never written. The written fields are job-owned (no end-user edits), so the job writes exactly what the geocode says: missing census values are written as NULL (with a WARNING) and an unmatched district writes `districtID = NULL` — never keep values from a previous location.
- **District ID matching**: LEA codes from Geocodio are matched against `NCESDISTRICTID` in the district table. `'0000'` (parser default: no unified district) never matches. Identical duplicate rows are collapsed; an NCES ID mapped to more than one IC district is ignored with a WARNING (those addresses stay unmatched) instead of crashing the run.
- **Parsing**: each response is parsed independently (`_parse_result`); an unexpected payload shape fails only its own row. Null/absent `census`/`school_districts` appends mean "no data", not failure.
- **Error handling**: `main()` owns the run lifecycle; the steps live in `run_pipeline()`. Any exception escaping `run_pipeline()` forces run status `FAILED` and exit 1 — app_logger on its own only reports `FAILED` when *every* step failed, otherwise `PARTIAL`, which `main()` maps to exit 0. `log_to_snowflake` is non-fatal (and does its own `commit()`), so IC updates still happen. Closing connections at teardown is best-effort (`_close_quietly`). SIGTERM keeps exit code 143.
- **Geocodio failures**: `GeocodioFatalError` ends geocoding on 401/403/429 (bad key, quota exhausted, rate limit), after `MAX_CONSECUTIVE_GEOCODE_FAILURES` (5) non-422 failures in a row, or when no lookup succeeded and any failed for a non-422 reason (small-batch outage). Its `results` carries the lookups completed before the abort; `run_pipeline()` still parses, audits, and writes those to IC, marks `geocode_addresses` FAILED, then re-raises so the run ends `FAILED`. A 422 (unparseable address) is per-row and neutral to the breaker. Per-row failures are retried on the next run.
- **IC write failures**: `update_ic_addresses` counts scattered per-row UPDATE failures (run ends `PARTIAL`), but raises `IcWriteError` after `MAX_CONSECUTIVE_IC_FAILURES` (5) failures in a row or when every attempted write failed, so the run ends `FAILED` / exit 1. Rows committed before the stop stay written. Rows skipped by the write policy are not attempts.
- **Connection retries**: Snowflake retries only `OperationalError` (network/backend, e.g. network not ready); `DatabaseError`/`ProgrammingError` (auth, config) fail fast. IC retries pyodbc errors except a rejected login (SQLSTATE `28000`), which fails on the first attempt to avoid locking the service account.
- **Geocodio auth**: API key is sent as `Authorization: Bearer`, never as the `api_key` query parameter
- **Batch size**: Configurable via `BATCH_SIZE` env var (default 500)
- **Census data**: Uses 2025 census tract, block, and county FIPS codes (`census2025` field; parser reads `fields.census['2025']`)
- **FERPA**: No student addresses, coordinates, or PII are written to log files. Rules that keep it true:
  - Never interpolate text from `requests`, pyodbc, or Snowflake-connector exceptions (`{e}`, `str(e)`, `e.msg`) into log messages; use `_describe_db_error(e)` (type + errno/SQLSTATE) or `type(e).__name__`. `requests` errors embed the URL (`q=<address>`), pyodbc errors name the login, Snowflake errors can echo bound row data.
  - When such an exception would escape a `log.step()`, re-raise a sanitized one `from None` (or raise outside the `except` block) — app_logger records `str(exc)` and the full chained traceback to NDJSON, Snowflake LOGS, and email/Teams notifications, and its redaction does not catch addresses. See `GeocodioFatalError`, `IcWriteError`, the `ConnectionError`s in the connection helpers, the `ValueError`s in `load_snowflake_private_key`, and the `RuntimeError`s in the fetch, parse, and enrich helpers.
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

- **Dockerfile**: `python:3.13-slim-bookworm` (Debian 12, to match Microsoft's `debian/12` repo for ODBC Driver 18; don't move to trixie unless Microsoft's repo for it carries `msodbcsql18`). One apt layer: `curl`/`gnupg` are purged after adding the repo; no `unixodbc-dev` (nothing is compiled: pip runs with `--only-binary=:all:`). Optional `MS_KEY_FINGERPRINT` build arg enforces Microsoft's key fingerprint.
- **app_logger wheel**: copied by exact filename and installed `--no-deps`; when upgrading it, update the filename in the Dockerfile.
- **Volume**: `../LogReader/logs:/app/logs` — shared with LogReader dashboard
- **Non-root**: Runs as `appuser` (UID 10001); app code is root-owned, only `/app/logs` is writable
- **Exec-form CMD**: Ensures SIGTERM reaches Python for graceful shutdown
- **Compose**: `init: true`, `stop_grace_period: 90s` (app_logger's SIGTERM handler sends notifications with 30s timeouts), `cap_drop: [ALL]`, `no-new-privileges`, no `container_name`. Cron uses `/usr/local/bin/docker-compose`; keep the file compatible with it (no Compose-v2-only keys such as `pull_policy` unless that binary is confirmed to be v2).
- **Checking an image without a production run**: `docker run --rm address-geocoding:latest sh -c "odbcinst -q -d"` and `... python -c "import pyodbc, snowflake.connector, pandas, app_logger"` (see README)

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

See `requirements.txt` (floors only, no lockfile; `requirements-dev.txt` adds pytest). Notes:
- `app_logger` is the vendored wheel in `vendor/`; install it alongside `requirements.txt`
- `snowflake-connector-python[pandas]` is pinned `<5`: 5.x logs a proxy WARNING on every connection, which alerts every run
- `python-dotenv>=1.2.0` is needed for `PYTHON_DOTENV_DISABLED` (tests)
- `cryptography` is imported directly but arrives transitively via the Snowflake connector
