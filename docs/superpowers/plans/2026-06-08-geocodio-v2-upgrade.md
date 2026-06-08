# Geocodio v1.12 → v2 Upgrade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut over the daily geocoding cron from Geocodio API v1.12 to v2, and bundle a census append-year switch from `census2024` to `census2025`, with zero observable change in production IC writes.

**Architecture:** Four single-line edits in `GeocodeAddresses.py` plus an `.env.example` bump, a fresh captured v2 fixture, and one updated parser test. The existing hardening (env-driven `GEOCODIO_API_VERSION`, `_failed_row` guard, one-shot deprecation scanner, `census_enrichment_rate` canary) carries the day-1 observability — no new modules.

**Tech Stack:** Python 3.13, `requests`, `pyodbc`, `snowflake-connector-python`, `pandas`, `pytest`. Docker for prod runtime. NDJSON logs via vendored `app_logger`.

**Spec:** `docs/superpowers/specs/2026-06-08-geocodio-v2-upgrade-design.md`

---

## File Structure

- **Modify:** `GeocodeAddresses.py` — lines 55 (version default), 68 (`API_FIELDS`), 628-632 (parser census-year key)
- **Modify:** `.env.example` — line 32 (`GEOCODIO_API_VERSION` example value)
- **Modify:** `tests/test_parse_geocode_results.py` — repoint `test_success_extracts_all_fields` to new fixture; update expected values
- **Create:** `tests/fixtures/geocodio_v2_with_census2025.json` — captured live v2 response, FERPA-safe public address

No new modules, no new runtime dependencies, no `requirements.txt` change.

---

## Task 1: Capture a live v2 API response fixture

This is a one-time manual capture against the real Geocodio API. We can't generate the fixture from code — it has to be a real response so the parser test pins to the actual v2 shape.

**Files:**
- Create: `tests/fixtures/geocodio_v2_with_census2025.json`

- [ ] **Step 1: Pick a FERPA-safe public address to geocode**

Use a known non-student address that will geocode to a rooftop match. Recommended: the Mapleton Public Schools district office.

Address string to use: `7350 Broadway, Denver, CO 80221`

This is the published Mapleton Public Schools administrative address — public, not student-linked, FERPA-safe to commit in a fixture.

- [ ] **Step 2: Capture the v2 response**

You need a working `GEOCODIO_API_KEY` for this step. Load your existing `.env` or export it inline.

Run:

```bash
source .env 2>/dev/null || true
curl -s "https://api.geocod.io/v2/geocode?q=7350+Broadway,+Denver,+CO+80221&api_key=${GEOCODIO_API_KEY}&fields=school,zip4,census2025" \
  | python3 -m json.tool \
  > tests/fixtures/geocodio_v2_with_census2025.json
```

Expected: a JSON file ~5-15 KB containing a top-level `results` array, with `results[0].location.lat`, `results[0].location.lng`, `results[0].accuracy`, `results[0].fields.school_districts.unified`, and `results[0].fields.census["2025"]` populated.

- [ ] **Step 3: Sanity-check the captured fixture**

Run:

```bash
python3 -c "
import json
with open('tests/fixtures/geocodio_v2_with_census2025.json') as f:
    payload = json.load(f)
result = payload['results'][0]
print('lat:', result['location']['lat'])
print('lng:', result['location']['lng'])
print('accuracy:', result['accuracy'])
print('accuracy_type:', result['accuracy_type'])
print('source:', result['source'])
print('census2025 keys:', list(result.get('fields', {}).get('census', {}).get('2025', {}).keys()))
print('school district name:', result.get('fields', {}).get('school_districts', {}).get('unified', {}).get('name'))
print('school district lea_code:', result.get('fields', {}).get('school_districts', {}).get('unified', {}).get('lea_code'))
"
```

Expected output shape:

```
lat: 39.83...
lng: -104.98...
accuracy: 0.8 to 1.0
accuracy_type: rooftop or street_center
source: <some string>
census2025 keys: ['block_code', 'tract_code', 'county_fips', ...]
school district name: <some Adams County / Mapleton district name>
school district lea_code: <7-digit LEA, likely "0802130" or similar>
```

Stop and inspect manually if anything is `None`, missing, or empty. If the capture is bad, re-run the curl and inspect.

- [ ] **Step 4: Record the captured values for Task 3**

Read the actual values from the sanity-check output and write them down — Task 3 step 1 needs them to update the test assertions.

You need these eight values:
- `latitude` (float)
- `longitude` (float)
- `accuracy` (float, 0.0–1.0)
- `accuracy_type` (string, e.g. `"rooftop"` or `"street_center"`)
- `block_code` (string, from `census2025`)
- `tract_code` (string, from `census2025`)
- `county_fips` (string, from `census2025`)
- `lea_code` (string, from `school_districts.unified`)
- `district_name` (string, from `school_districts.unified`)

- [ ] **Step 5: Commit the fixture**

```bash
git add tests/fixtures/geocodio_v2_with_census2025.json
git commit -m "$(cat <<'EOF'
Add captured v2 Geocodio response fixture

Captures a real v2 API response for a public Mapleton Public Schools
district office address (7350 Broadway, Denver, CO 80221) requesting
fields=school,zip4,census2025. Used by the parser test to pin the v2
response shape.

FERPA-safe: public administrative address, no student PII.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Switch census append year from 2024 to 2025 (TDD)

This task tightens the test against the new v2 fixture (red), then updates the parser and `API_FIELDS` so the test passes (green). Three files change in one logical commit: test, parser, fields constant.

**Files:**
- Modify: `tests/test_parse_geocode_results.py` — `test_success_extracts_all_fields` (lines 56-72)
- Modify: `GeocodeAddresses.py` — `API_FIELDS` (line 68), parser census key (lines 628-632)

- [ ] **Step 1: Update the success-path test to use the new v2 fixture**

Open `tests/test_parse_geocode_results.py`. Replace the existing `test_success_extracts_all_fields` (lines 56-72) with the version below. **Substitute the actual captured values from Task 1 Step 4** for the placeholders marked `<...>`.

```python
def test_success_extracts_all_fields():
    """Parser extracts location, accuracy, census2025, and school district from a real v2 response."""
    fixture = _load("geocodio_v2_with_census2025.json")
    df = ga.parse_geocode_results([fixture], _addresses_df())
    assert len(df) == 1
    row = df.iloc[0]
    assert row["success"]
    assert row["latitude"] == <CAPTURED_LATITUDE>
    assert row["longitude"] == <CAPTURED_LONGITUDE>
    assert row["accuracy"] == <CAPTURED_ACCURACY>
    assert row["accuracy_type"] == "<CAPTURED_ACCURACY_TYPE>"
    assert row["block_code"] == "<CAPTURED_BLOCK_CODE>"
    assert row["tract_code"] == "<CAPTURED_TRACT_CODE>"
    assert row["county_fips"] == "<CAPTURED_COUNTY_FIPS>"
    assert row["lea_code"] == "<CAPTURED_LEA_CODE>"
    assert row["district_name"] == "<CAPTURED_DISTRICT_NAME>"
    assert row["raw_response"] == fixture
```

- [ ] **Step 2: Run the updated test to verify it fails**

Run:

```bash
pytest tests/test_parse_geocode_results.py::test_success_extracts_all_fields -v
```

Expected: **FAIL.** The parser still reads `census['2024']`, so `block_code`, `tract_code`, and `county_fips` come back as `None`, which won't equal the captured 2025 strings. You should see assertion errors on the `block_code` / `tract_code` / `county_fips` lines.

If the test passes instead, stop — that means either the fixture has a `'2024'` key under `fields.census` (which would mean we requested the wrong year in Task 1), or the test wasn't actually pointed at the new fixture. Re-check Task 1 step 2 and step 1 of this task.

- [ ] **Step 3: Update the parser to read `census['2025']`**

Open `GeocodeAddresses.py`. Replace lines 628-632 inside `parse_geocode_results`:

```python
                # Extract census data (2025)
                # API returns nested structure: fields.census.2025
                census_data = fields.get('census', {})
                if census_data and '2025' in census_data:
                    census_2025 = census_data['2025']
                    block_code = census_2025.get('block_code')
                    tract_code = census_2025.get('tract_code')
                    county_fips = census_2025.get('county_fips')
```

Also update the docstring comment two lines above the block (line 626) if it mentions "2024" — replace with "2025" so the comment matches the code.

- [ ] **Step 4: Update `API_FIELDS` to request `census2025`**

Open `GeocodeAddresses.py`. Replace line 68:

```python
API_FIELDS = ["school", "zip4", "census2025"]
```

- [ ] **Step 5: Run the full test suite to verify all five tests pass**

Run:

```bash
pytest tests/test_parse_geocode_results.py -v
```

Expected: **all 5 tests pass.**

- `test_failed_row_helper_consistency` — unchanged, still passes.
- `test_success_extracts_all_fields` — now passes against the v2 fixture with census2025 values.
- `test_no_results_returns_failed_row` — unchanged, still passes.
- `test_missing_location_returns_failed_row` — unchanged, still passes.
- `test_deprecation_scan_finds_warnings_entry` — unchanged, still passes against the v1.9 fixture.

If any unchanged test fails, stop and investigate — your parser edit may have unintended side effects.

- [ ] **Step 6: Commit**

```bash
git add tests/test_parse_geocode_results.py GeocodeAddresses.py
git commit -m "$(cat <<'EOF'
Switch Geocodio census append from 2024 to 2025

API_FIELDS now requests census2025, and parse_geocode_results reads
the census2025 subtree. The parser test repoints to the new v2
fixture captured against a public Mapleton-area address.

The fields.census["2024"] -> fields.census["2025"] key change in the
Snowflake LOGGING_GEOCODIOAPI_ADDRESSLOOKUPS audit trail has no
downstream readers per pre-design check.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Bump default `GEOCODIO_API_VERSION` to `v2`

Two single-line changes — `.env.example` and the in-code default — flip the production URL from `https://api.geocod.io/v1.12/geocode` to `https://api.geocod.io/v2/geocode`. This commit IS the cutover for any environment that relies on the default.

**Files:**
- Modify: `.env.example` — line 32
- Modify: `GeocodeAddresses.py` — line 55

- [ ] **Step 1: Update `.env.example`**

Open `.env.example`. Replace line 32:

```
GEOCODIO_API_VERSION=v2
```

- [ ] **Step 2: Update the in-code default**

Open `GeocodeAddresses.py`. Replace line 55:

```python
GEOCODIO_API_VERSION = os.getenv('GEOCODIO_API_VERSION', 'v2')
```

- [ ] **Step 3: Run the test suite to confirm nothing broke**

The default change has no test coverage of its own (no test exercises `GEOCODIO_API_URL` directly), but the broader suite must still pass.

Run:

```bash
pytest tests/test_parse_geocode_results.py -v
```

Expected: **all 5 tests pass.**

- [ ] **Step 4: Sanity-check the resulting URL by import**

Run:

```bash
python3 -c "
import os
os.environ.setdefault('SNOWFLAKE_USER', 'test')
os.environ.setdefault('SNOWFLAKE_PRIVATE_KEY_B64', 'test')
os.environ.setdefault('GEOCODIO_API_KEY', 'test')
os.environ.setdefault('IC_DB_USER', 'test')
os.environ.setdefault('IC_DB_PASSWORD', 'test')
import GeocodeAddresses as ga
print('URL:', ga.GEOCODIO_API_URL)
print('VERSION:', ga.GEOCODIO_API_VERSION)
print('FIELDS:', ga.API_FIELDS)
"
```

Expected output:

```
URL: https://api.geocod.io/v2/geocode
VERSION: v2
FIELDS: ['school', 'zip4', 'census2025']
```

If `URL` still shows `v1.12`, your local `.env` is overriding the default — that's fine for prod (the deploy uses the container's env), but for sanity make sure the default itself reads correctly by temporarily unsetting `GEOCODIO_API_VERSION` and re-running.

- [ ] **Step 5: Commit**

```bash
git add .env.example GeocodeAddresses.py
git commit -m "$(cat <<'EOF'
Bump default Geocodio API version v1.12 -> v2

Defaults to v2 so the deploy is the cutover. .env.example updated to
match. Future version bumps remain a single config change.

v2's documented breaking changes (renamed address_components keys,
moved parsed-address location, Canadian FSA, spreadsheet output)
don't touch any path this parser reads.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Smoke test in dev container

Manual verification step before merging. Requires access to the dev Docker container, dev Snowflake credentials, and dev/test access to the Infinite Campus DB. No code change, no commit — this is a go/no-go gate.

**Files:** None modified.

- [ ] **Step 1: Set `BATCH_SIZE=1` in the dev `.env`**

Edit your dev environment file so the smoke run only geocodes one address. Make sure `GEOCODIO_API_VERSION` is unset (or set to `v2`) so it picks up the new default.

- [ ] **Step 2: Run the script**

From the dev environment (container or local Python with the dev `.env` loaded):

```bash
python GeocodeAddresses.py
```

- [ ] **Step 3: Inspect the NDJSON log**

Look at the most recent NDJSON file under `../LogReader/logs/Address_Geocoding/`.

Verify the following lines appear:

- A `validate_config` step with `"Configuration validated (Geocodio API version: v2)"` in the message.
- A `geocode_addresses` step with `metadata.avg_accuracy` present and `metadata.census_enrichment_rate` between 0.9 and 1.0 for the single rooftop address.
- A `update_ic` step with `rows: 1` (the one address was successfully written).
- **No** line containing `"Geocodio API deprecation notice received"`.

If any of these fail, stop and investigate before merging.

- [ ] **Step 4: Inspect the Snowflake audit row**

Connect to Snowflake (dev/test). Query the most recent row from the audit table:

```sql
SELECT IC_ADDRESSID, GEOCODIO_JSON
FROM MPS_WAREHOUSE.DATALAKE.LOGGING_GEOCODIOAPI_ADDRESSLOOKUPS
ORDER BY LOAD_TIMESTAMP DESC
LIMIT 1;
```

Drill into the `GEOCODIO_JSON` VARIANT and confirm:

- `results[0].fields.census."2025"` exists and is populated.
- `results[0].fields.census."2024"` is **absent or empty** (we no longer request it).
- `results[0].location.lat` and `results[0].location.lng` are present and numeric.

- [ ] **Step 5: Inspect the IC `ADDRESS` row**

Connect to the Infinite Campus dev database. Query the address that was just processed:

```sql
SELECT addressid, latitude, longitude, tract, block, location_code, districtID
FROM ADDRESS
WHERE addressid = <ID_FROM_LOG>;
```

Confirm:

- `latitude` and `longitude` are non-NULL and look like Colorado coordinates (lat ~39-40, lng ~-104 to -105).
- `tract`, `block`, and `location_code` are non-NULL.
- `districtID` is non-zero if the LEA matched (most Mapleton-area addresses should match district `1`).

- [ ] **Step 6: Restore `BATCH_SIZE`**

Set `BATCH_SIZE=500` (or remove the override) in the dev `.env` so the next scheduled run processes a normal batch.

No commit — this task is verification only.

---

## Task 5: Open the PR

**Files:** None modified.

- [ ] **Step 1: Push the branch**

```bash
git push -u origin HEAD
```

- [ ] **Step 2: Open the PR**

```bash
gh pr create --title "Upgrade Geocodio API v1.12 -> v2 with census2025 switch" --body "$(cat <<'EOF'
## Summary

- Bumps default `GEOCODIO_API_VERSION` from `v1.12` to `v2`, eliminating the daily deprecation WARN.
- Switches census append from `census2024` to `census2025` (newer 5-year ACS vintage); no downstream readers of the audit table's census subtree per pre-design check.
- Refreshes the captured parser fixture against a real v2 response (`tests/fixtures/geocodio_v2_with_census2025.json`).
- No new modules or runtime dependencies. Four single-line code edits plus a fixture and test update.

## Design doc

`docs/superpowers/specs/2026-06-08-geocodio-v2-upgrade-design.md`

## Test plan

- [x] `pytest tests/test_parse_geocode_results.py -v` — all 5 tests pass locally
- [x] Manual smoke test in dev container with `BATCH_SIZE=1` — NDJSON shows v2 active, no deprecation warning, IC row updated with non-null tract/block/location_code, Snowflake audit row contains `results[0].fields.census."2025"`

## Rollback

Set `GEOCODIO_API_VERSION=v1.12` in `.env` and restart the container. Geocodio v1.12 still returns `census2025` when requested in `fields`, so census enrichment survives the env-var rollback without a code revert.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: Return the PR URL**

The `gh pr create` output prints the PR URL. Share it back so it can be reviewed and merged.

---

## Post-merge verification

Not a task — operational observation. After the merge image deploys and the next 10am Mountain cron fires, watch the NDJSON for the first production run:

- `census_enrichment_rate` ≥ historical baseline (within ±0.05 of prior week).
- `district_match_rate` ≥ historical baseline.
- `avg_accuracy` within ±0.05 of prior week.
- `update_ic` row count within ±10% of historical daily volume.
- No `WARNING: Geocodio API deprecation notice received` line.

If any metric drifts beyond these bounds, follow the rollback procedure (set `GEOCODIO_API_VERSION=v1.12` in `.env`, restart container, investigate before re-deploying).
