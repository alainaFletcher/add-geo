# Geocodio API v1.9 → v1.12 Upgrade — Design

**Date:** 2026-04-30
**Author:** Brian Fuller
**Status:** Approved (pending implementation plan)
**Approach:** B — Targeted hardening upgrade

## Context

Geocodio is returning a deprecation `_warnings` entry in every response from v1.9:

> "There is a newer API version available, please consider upgrading to v1.12. See changelog here: https://www.geocod.io/docs/#changelog"

The current code hardcodes `https://api.geocod.io/v1.9/geocode` at `GeocodeAddresses.py:37` and reads response paths (`results[0].location`, `fields.census["2024"]`, `fields.school_districts.unified`) inline in `parse_geocode_results`.

A four-agent review (API contract, Python correctness, data integrity, web research) verified the v1.9 → v1.12 changelog. None of the breaking changes between v1.9 and v1.12 affect the fields this app uses:

- **v1.10 breaking:** `acs-social` table B21002 row label rename — app does not use any `acs-*` appends
- **v1.11 breaking:** `stable_address_key` gets unit suffix — app does not consume this key
- **v1.12 breaking:** Mexican address formatting — app is US-only

A live v1.9 response sample (Thornton, CO) confirms `fields.census["2024"]`, `fields.school_districts.unified`, and `location.lat/lng` shapes are intact.

The version bump itself is a one-line URL change. The remaining work is targeted hardening that pays back across all future Geocodio version bumps.

## Goals

1. Cut over to Geocodio v1.12 with no observable change in production behavior.
2. Make future version bumps cheap (single env var change + fixture re-run).
3. Eliminate the unguarded `result['location']` KeyError crash mode.
4. Surface the next deprecation notice in logs instead of dropping it silently.
5. Add a `census_enrichment_rate` canary for silent census-shape regressions.
6. Pin parser behavior to a fixture so regressions are caught locally.

## Non-Goals

- No refactor of `parse_geocode_results` beyond what tests and the `.get()` guard require.
- No changes to IC `ADDRESS` schema.
- No changes to Snowflake `LOGGING_GEOCODIOAPI_ADDRESSLOOKUPS` shape (downstream consumers untouched).
- No `census2024` → `census2025` switch (separate decision, separate ticket).
- No new field capture (`match_type`, `stable_address_key`) — Approach C territory.
- No shadow-mode dual-call cutover.
- No retry/backoff on transient 5xx (P2 Python finding — separate small PR).

## Success Criteria

- Production cron run after deploy shows the same row counts to IC `ADDRESS` (±10%), the same `district_match_rate`, and the same `avg_accuracy` as the prior week's baseline.
- A future v1.12 → v1.13 upgrade requires only setting `GEOCODIO_API_VERSION=v1.13` in `.env` and re-running the fixture test.
- No deprecation warning in NDJSON logs (since we are now on the latest version).

## Architecture

Single file touched: `GeocodeAddresses.py`. No new modules. No new runtime dependencies. New dev dependency: `pytest` in a new `requirements-dev.txt` (not in the Docker image).

### Change 1 — Version parameterization

Replaces `GeocodeAddresses.py:36-37`:

```python
GEOCODIO_API_VERSION = os.getenv('GEOCODIO_API_VERSION', 'v1.12')
GEOCODIO_API_URL = f"https://api.geocod.io/{GEOCODIO_API_VERSION}/geocode"
```

The version is logged at startup so support can confirm the active version from NDJSON.

### Change 2 — Guarded `location` lookup + failed-row helper

Replaces the unguarded indexing at `GeocodeAddresses.py:565-566` and extracts the duplicated failed-row dict at lines 521-535 and 543-558 into a helper:

```python
def _failed_row(addressid, addressline1, raw_response):
    return {
        'ADDRESSID': addressid,
        'ADDRESSLINE1': addressline1,
        'success': False,
        'latitude': None,
        'longitude': None,
        'accuracy': 0.0,
        'accuracy_type': None,
        'source': None,
        'block_code': None,
        'tract_code': None,
        'county_fips': None,
        'lea_code': '0000',
        'district_name': 'none',
        'raw_response': raw_response,
    }
```

The success-path `location` read becomes:

```python
location = result.get('location') or {}
latitude = location.get('lat')
longitude = location.get('lng')
if latitude is None or longitude is None:
    parsed_rows.append(_failed_row(original_addressid, original_address, address_result))
    continue
```

A missing or null `location` no longer crashes the entire batch.

### Change 3 — Deprecation signal capture (one-shot)

The canonical location for Geocodio's deprecation notice is the top-level `_warnings` array, confirmed against a live v1.9 response. Module-level state plus a check inside `geocode_address`:

```python
_deprecation_warned = False  # one-shot per process

def _scan_payload_for_deprecation(payload: dict) -> str | None:
    for warning in payload.get("_warnings", []):
        if isinstance(warning, str) and (
            "newer API version" in warning or "deprecat" in warning.lower()
        ):
            return warning
    # Fallback: any top-level string mentioning deprecation/upgrade
    for value in payload.values():
        if isinstance(value, str) and (
            "deprecat" in value.lower() or "newer API version" in value
        ):
            return value
    return None

def _check_deprecation(response, payload):
    global _deprecation_warned
    if _deprecation_warned:
        return
    notice = (
        response.headers.get('Sunset')
        or response.headers.get('Deprecation')
        or _scan_payload_for_deprecation(payload)
    )
    if notice:
        logger.warning(
            "Geocodio API deprecation notice received",
            extra={"deprecation_notice": str(notice), "current_version": GEOCODIO_API_VERSION},
        )
        _deprecation_warned = True
```

`geocode_address` is updated to call `_check_deprecation(response, payload)` once per response, before returning the payload.

### Change 4 — `census_enrichment_rate` step metric

Added inside the `geocode_addresses` step in `main()` near the existing `avg_accuracy` metric (around line 919-921):

```python
if success_count > 0:
    successful = results_df[results_df['success']]
    census_complete = successful[
        successful['tract_code'].notna() & successful['block_code'].notna()
    ]
    s.metadata["census_enrichment_rate"] = round(len(census_complete) / success_count, 3)
```

A sudden drop in this rate after a future version bump signals a silent census-shape change.

### Change 5 — Test fixtures and parser tests

New files:

- `tests/fixtures/geocodio_v19_with_deprecation.json` — captured live v1.9 response (Thornton, CO) including `_warnings` deprecation notice. FERPA-safe: public address, no student PII.
- `tests/fixtures/geocodio_v19_no_results.json` — captured response for an intentionally invalid address (e.g. `"asdfasdf"`) producing `{ "results": [] }`.
- `tests/fixtures/geocodio_v19_missing_location.json` — synthetic: success fixture with `results[0].location` deleted.
- `tests/test_parse_geocode_results.py` — five tests:
  1. `test_success_extracts_all_fields` — fixture → parser → assert all extracted fields match
  2. `test_no_results_returns_failed_row` — empty results array → `success=False`, all coord fields None
  3. `test_missing_location_returns_failed_row` — missing location → no KeyError, `success=False`
  4. `test_failed_row_helper_consistency` — `_failed_row()` shape matches success-path failed dict (drift guard)
  5. `test_deprecation_scan_finds_warnings_entry` — `_scan_payload_for_deprecation` returns the notice text from `_warnings`; returns None for clean response
- `requirements-dev.txt` — `pytest` only.

No live-API integration test in CI (consumes Geocodio quota, requires secrets). Smoke test is manual, documented below.

## Data Flow

| Stage | Today (v1.9) | After (v1.12 + Approach B) |
|-------|-------------|----------------------------|
| Request URL | hardcoded `v1.9` | env-driven, default `v1.12` |
| API call | direct, no introspection | `_check_deprecation()` runs once per process |
| Parse `location` | `result['location']['lat']` (KeyError → fatal batch) | `.get()` chain → graceful failed-row |
| Parse `census['2024']` | unchanged | unchanged (still requesting `census2024`) |
| Parse `school_districts.unified` | unchanged | unchanged |
| Snowflake VARIANT log | full raw response | full raw response (unchanged) |
| IC update | only rows with `accuracy >= 0.8` | unchanged |
| Step telemetry | `avg_accuracy`, `district_match_rate` | `+ census_enrichment_rate` |

## Error Handling

| Failure mode | Today | After |
|--------------|-------|-------|
| `location` key missing or null | KeyError → entire batch step fails (fatal) | Single row marked `success=False`, batch continues |
| Deprecation notice in response | silently ignored | one WARN log per process |
| `census2024` shape changes in v1.13+ | tract/block fall through to None, IC writes NULLs | Same writes, but `census_enrichment_rate` drops visibly in NDJSON / Snowflake step log |
| `GEOCODIO_API_VERSION` unset | n/a | falls back to default `v1.12` |
| `GEOCODIO_API_VERSION` set to garbage | n/a | first API call returns 404 → fatal step (acceptable: invalid config should fail loudly) |

## Logging Boundaries (FERPA)

- `_check_deprecation` logs only the notice text and current version — no addresses, no PII.
- Fixture addresses must be non-PII (public address, district office, or known synthetic).
- `census_enrichment_rate` is an aggregate ratio, not per-address — safe.

## Backward Compatibility

- Env var defaults to `v1.12`. Operator has already added `GEOCODIO_API_VERSION` to `.env`. Deploy IS the cutover.
- One-shot deprecation flag resets per process restart, so each daily cron run gets one fresh warning if applicable.
- Snowflake VARIANT log shape unchanged — downstream consumers unaffected.
- IC `ADDRESS` schema unchanged.

## Rollout

1. Code change merges to `main` with `GEOCODIO_API_VERSION` defaulting to `v1.12`.
2. Manual smoke test in dev container with `BATCH_SIZE=1`:
   - Verify NDJSON shows new `census_enrichment_rate` metric.
   - Verify no deprecation warning fires (since we are now on the recommended version).
   - Verify IC `ADDRESS` row updates as expected.
   - Inspect Snowflake `LOGGING_GEOCODIOAPI_ADDRESSLOOKUPS` for the v1.12 raw response.
3. First production cron run (next 10am Mountain). Monitor NDJSON at `/app/logs/Address_Geocoding/`:
   - `census_enrichment_rate` >= historical baseline
   - `district_match_rate` >= historical baseline
   - No `WARNING: Geocodio API deprecation notice received` line
   - `update_ic` row count within ±10% of historical daily volume

## Rollback

Set `GEOCODIO_API_VERSION=v1.9` in `.env`, restart the container. No code revert needed. Geocodio guarantees v1.9 availability for ≥12 months after replacement (per their stated changelog policy).

## Risks at Deploy

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| v1.12 returns subtly different accuracy distribution | Low | Existing `avg_accuracy` metric shows drift on day 1 |
| New `_warnings` shape in v1.13 not caught by the scan | Low | Generic top-level scan as fallback; missed warning is non-fatal |
| Fixture diverges from production response over time | Low | Fixture is a contract pin for the parser's read paths only |

## Deferred Follow-ups

Documented here, not implemented in this change:

- Switch `census2024` → `census2025` (downstream consumers of `LOGGING_GEOCODIOAPI_ADDRESSLOOKUPS:results[0]:fields:census:"2024"` need confirmation first).
- Capture `match_type` (v1.11) and `stable_address_key` (v1.10) into Snowflake / IC.
- Retry/backoff on transient 5xx in `geocode_address`.
- Alerting threshold on `district_match_rate` or `census_enrichment_rate` drop (separate observability ticket).

## References

- Verified changelog source: https://www.geocod.io/docs/#changelog
- v1.10 release notes: https://www.geocod.io/updates/2026-02-24-new-api-release-census-acs-2024-update/
- v1.9 census2025 update: https://www.geocod.io/updates/2025-12-17-api-v1.9-census2025-geographies-now-available/
- Geocodio updates index: https://www.geocod.io/updates/
- Source file: `GeocodeAddresses.py` (lines 37, 69, 478-632, 565-566, 695-757)
- Project conventions: `CLAUDE.md`
