# Geocodio API v1.12 → v2 Upgrade — Design

**Date:** 2026-06-08
**Author:** Brian Fuller
**Status:** Approved (pending implementation plan)
**Approach:** A — Straight cutover with bundled census2024 → census2025 switch

## Context

Geocodio is returning a deprecation `_warnings` entry in every response from v1.12:

> "There is a newer API version available, please consider upgrading to v2. See changelog here: https://www.geocod.io/docs/#changelog"

This was surfaced by the one-shot deprecation capture added in the v1.9 → v1.12 upgrade (`GeocodeAddresses.py:113-129`) — proof that the canary works. Today's daily cron run logs exactly one such WARN per process.

Geocodio's "Migrating to API v2" guide lists these breaking changes:

1. Base URL becomes `https://api.geocod.io/v2/`.
2. "Read the parsed address from `results`" — `address_components` is now nested under each `results[i]` rather than at a top-level alternative location.
3. Renamed `address_components` keys.
4. Canadian results return FSA only.
5. Spreadsheet (lists) output reshape.

**For this app, items 3–5 do not apply** (US-only, single-address geocoding, parser never reads `address_components`). Item 2 is satisfied — `parse_geocode_results` already reads from `results[0]`. Item 1 is a one-character config change because `GEOCODIO_API_VERSION` was parameterized in the previous upgrade.

A live v2 sample response confirmed that `location.lat/lng`, `accuracy`, `accuracy_type`, `source`, `fields.school_districts.unified`, and `fields.census` are intact across v1.12 → v2. `fields.census` supports years `census2000`–`census2025` in v2.

The current parser reads `fields.census['2024']`. Since we are touching the file anyway and no downstream consumer reads the audit table's census subtree, this design bundles a switch to the newer `census2025` ACS vintage.

## Goals

1. Cut over to Geocodio API v2 with no observable change in production IC writes.
2. Switch census appends from `census2024` to `census2025` (newer 5-year ACS data).
3. Refresh the captured test fixture against a v2 response so the parser regression test pins to the live shape.
4. Eliminate the existing `WARNING: Geocodio API deprecation notice received` in NDJSON logs.

## Non-Goals

- No capture of new v2 fields (`stable_address_key`, `match_type`, `address_components` renames).
- No retry/backoff on transient 5xx in `geocode_address`.
- No alerting threshold work on `census_enrichment_rate` / `district_match_rate`.
- No IC `ADDRESS` schema change.
- No change to `LOGGING_GEOCODIOAPI_ADDRESSLOOKUPS` ingest shape (we still write the full raw response; only the embedded census subtree key changes from `"2024"` to `"2025"`).
- No shadow-mode dual-call cutover.
- No new modules or runtime dependencies.

## Success Criteria

- Production cron run after deploy shows row counts to IC `ADDRESS` within ±10% of the prior week's baseline, with `district_match_rate` and `avg_accuracy` within historical norms.
- `census_enrichment_rate` step metric remains within ±0.05 of the prior week's value (proves `census2025` enrichment density matches what `census2024` was producing).
- No `WARNING: Geocodio API deprecation notice received` line in NDJSON (since we are now on the latest version).
- A future `census2025` → `census2026` swap requires editing two literals (`API_FIELDS` and the parser's census-year key) — no other changes.

## Architecture

Single application file touched: `GeocodeAddresses.py`. Plus `.env.example` and a fixture/test refresh. No new modules. No new runtime dependencies. No change to `requirements.txt` or `requirements-dev.txt`.

### Change 1 — Default API version

`.env.example`:

```diff
- GEOCODIO_API_VERSION=v1.12
+ GEOCODIO_API_VERSION=v2
```

`GeocodeAddresses.py:55`:

```diff
- GEOCODIO_API_VERSION = os.getenv('GEOCODIO_API_VERSION', 'v1.12')
+ GEOCODIO_API_VERSION = os.getenv('GEOCODIO_API_VERSION', 'v2')
```

The version is already logged at startup by `validate_config()` so support can confirm the active version from NDJSON.

### Change 2 — Census append year

`GeocodeAddresses.py:68`:

```diff
- API_FIELDS = ["school", "zip4", "census2024"]
+ API_FIELDS = ["school", "zip4", "census2025"]
```

### Change 3 — Parser census-year key

`GeocodeAddresses.py:628-632`:

```diff
  census_data = fields.get('census', {})
- if census_data and '2024' in census_data:
-     census_2024 = census_data['2024']
-     block_code = census_2024.get('block_code')
-     tract_code = census_2024.get('tract_code')
-     county_fips = census_2024.get('county_fips')
+ if census_data and '2025' in census_data:
+     census_2025 = census_data['2025']
+     block_code = census_2025.get('block_code')
+     tract_code = census_2025.get('tract_code')
+     county_fips = census_2025.get('county_fips')
```

The local variable rename is purely for readability; no callers reference it.

### Change 4 — Fixture and test refresh

| File | Action | Purpose |
|------|--------|---------|
| `tests/fixtures/geocodio_v2_with_census2025.json` | **new** | Captured live v2 response for a public Mapleton-area address, requesting `fields=school,zip4,census2025`. FERPA-safe: public address only. |
| `tests/fixtures/geocodio_v19_with_deprecation.json` | **keep** | Repurposed as the canonical "response carrying a deprecation notice" fixture. Used only by `test_deprecation_scan_finds_warnings_entry`. Filename retained to preserve git history. |
| `tests/fixtures/geocodio_v19_no_results.json` | **keep** | Shape-invariant (`{"results": []}`). |
| `tests/fixtures/geocodio_v19_missing_location.json` | **keep** | Shape-invariant (success result with `location` deleted). |
| `tests/test_parse_geocode_results.py` | **update** | Repoint `test_success_extracts_all_fields` to the new v2 fixture and update expected coords / census codes / district. No new tests. |

Test inventory after this change (5 tests, same count as today):

1. `test_failed_row_helper_consistency` — unchanged.
2. `test_success_extracts_all_fields` — **updated** to load `geocodio_v2_with_census2025.json` and assert against v2 values.
3. `test_no_results_returns_failed_row` — unchanged.
4. `test_missing_location_returns_failed_row` — unchanged.
5. `test_deprecation_scan_finds_warnings_entry` — unchanged (still uses the v1.9 fixture's `_warnings` entry as a known-good positive case).

The v2 fixture is captured manually with a one-off `curl` against `https://api.geocod.io/v2/geocode?q=<public address>&api_key=<key>&fields=school,zip4,census2025`. Procedure documented in the implementation plan.

## Data Flow

| Stage | Today (v1.12 + census2024) | After (v2 + census2025) |
|-------|----------------------------|--------------------------|
| Request URL | `https://api.geocod.io/v1.12/geocode` | `https://api.geocod.io/v2/geocode` |
| `fields` param | `school,zip4,census2024` | `school,zip4,census2025` |
| Parse `location.lat/lng` | unchanged | unchanged |
| Parse `accuracy`, `accuracy_type`, `source` | unchanged | unchanged |
| Parse `fields.census[<year>]` | reads `'2024'` subtree | reads `'2025'` subtree |
| Parse `fields.school_districts.unified` | unchanged | unchanged |
| Snowflake VARIANT log | full raw response | full raw response (embedded census key changes; no downstream readers) |
| IC `ADDRESS` update | only rows with `accuracy >= 0.8` | unchanged |
| Step telemetry | `avg_accuracy`, `census_enrichment_rate`, `district_match_rate` | unchanged |
| Deprecation scan | one-shot WARN on `_warnings`/`Sunset`/`Deprecation` headers | unchanged (now silent since we're on the latest version) |

## Error Handling

| Failure mode | Today | After |
|--------------|-------|-------|
| `census['2024']` key returns `None` | block/tract/county FIPS fall through to `None`, IC writes NULLs | Same fallthrough, but the key being checked is `'2025'`. Day-1 visibility comes from `census_enrichment_rate` in NDJSON. |
| v2 returns subtly different `location` / `accuracy` shape | n/a | Existing `.get()` guards on `location`, `accuracy`, `accuracy_type`, `source` survive any field-level absence. `_failed_row()` covers the fully-missing case. |
| v2 emits `_warnings` we don't recognize | n/a | Generic top-level-string scanner in `_scan_payload_for_deprecation` already catches "deprecat" / "newer API version" patterns. Missed warning is non-fatal. |
| `GEOCODIO_API_VERSION=v2` rejected by API | n/a | 404 → step fails fatally (acceptable: invalid config should fail loudly). |
| Hidden v2 response shape divergence we didn't anticipate | n/a | `avg_accuracy` and `census_enrichment_rate` step metrics surface drift on the first cron run. |

## Logging Boundaries (FERPA)

- No new log lines emit addresses, coordinates, or PII.
- Fixture address must be non-PII (e.g. district office, public landmark).
- `census_enrichment_rate` remains an aggregate ratio, not per-address.

## Backward Compatibility

- Env var defaults flip to `v2`. Operator updates `.env` (or relies on the new default). Deploy IS the cutover.
- One-shot deprecation flag resets per process restart, so each daily cron run gets one fresh warning if a future v3 deprecation surfaces.
- Snowflake VARIANT log shape unchanged at the top level; the embedded `fields.census` subtree changes from key `"2024"` to key `"2025"`. No downstream readers per pre-design check.
- IC `ADDRESS` schema unchanged.

## Rollout

1. PR merges to `main` with defaults `GEOCODIO_API_VERSION=v2` and `census2025` in `API_FIELDS`. `.env.example` updated to match.
2. Manual smoke test in dev container with `BATCH_SIZE=1`:
   - NDJSON shows `Configuration validated (Geocodio API version: v2)` at startup.
   - `census_enrichment_rate` step metric is present and ≥ 0.9 for a known-rooftop address.
   - No `WARNING: Geocodio API deprecation notice received` line.
   - The IC `ADDRESS` row updates with non-null `tract`, `block`, `location_code`.
   - Snowflake `LOGGING_GEOCODIOAPI_ADDRESSLOOKUPS` shows the raw v2 response with `fields.census."2025"` populated.
3. Restore `BATCH_SIZE=500`. First production cron run is the next 10am Mountain. Monitor NDJSON at `../LogReader/logs/Address_Geocoding/`:
   - `census_enrichment_rate` ≥ historical baseline.
   - `district_match_rate` ≥ historical baseline.
   - `avg_accuracy` within ±0.05 of the prior week's average.
   - `update_ic` row count within ±10% of historical daily volume.

## Rollback

Set `GEOCODIO_API_VERSION=v1.12` in `.env` and restart the container. The parser still expects `census['2025']`, but Geocodio v1.12 also returns `census2025` when requested in the `fields` param (both years are valid append names in v1.12). Net: env-var rollback recovers geocoding/coordinates/district immediately and keeps census enrichment intact.

A code revert is only needed if v1.12 itself is later sunset and the env-var rollback path stops working.

## Risks at Deploy

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| v2 returns subtly different accuracy distribution | Low | Existing `avg_accuracy` metric surfaces drift on day 1 |
| `census2025` returns different tract/block codes than `census2024` for the same address | Expected for some addresses (boundaries shift between ACS vintages) | Acceptable — this is the point of moving to fresher data; IC schema is year-agnostic |
| v2 emits an unrecognized `_warnings` payload shape | Low | Generic top-level-string fallback in `_scan_payload_for_deprecation` catches expected patterns |
| Captured v2 fixture diverges from production response over time | Low | Fixture is a contract pin for the parser's read paths only — drift in unrelated fields is fine |
| `address_components` key renames break a Snowflake reader we forgot about | Very low | Pre-design check confirmed no downstream readers of the audit table |

## Deferred Follow-ups

Documented here, not implemented in this change:

- Capture `stable_address_key` from v2 results into Snowflake (would need an IC schema change too if surfaced there).
- Capture `match_type` per result.
- Retry/backoff on transient 5xx in `geocode_address`.
- NDJSON-based alerting thresholds on `district_match_rate` / `census_enrichment_rate` drops.
- A future `census2025` → `census2026` swap when next year's ACS release lands.

## References

- Geocodio "Migrating to API v2" guide: https://www.geocod.io/docs/#migrating-to-api-v2
- Geocodio docs index: https://www.geocod.io/docs/
- Geocodio updates feed: https://www.geocod.io/updates/
- Prior upgrade spec (v1.9 → v1.12): `docs/superpowers/specs/2026-04-30-geocodio-api-upgrade-design.md`
- Prior upgrade plan: `docs/superpowers/plans/2026-04-30-geocodio-api-upgrade.md`
- Source file: `GeocodeAddresses.py` (lines 55, 68, 113-129, 628-632)
- Project conventions: `CLAUDE.md`
