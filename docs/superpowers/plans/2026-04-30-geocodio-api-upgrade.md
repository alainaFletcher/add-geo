# Geocodio API v1.9 → v1.12 Upgrade Implementation Plan

> **Historical record.** Implemented and merged in Mapleton-IT/Address-Geocoding#1. Code has moved on since (e.g. API v2/v2.1, census2025, header auth, fatal-error handling); `README.md` and `CLAUDE.md` describe current behavior. Line numbers and smoke-test steps here are out of date — smoke-test via the Docker command in `README.md`, not by running the script locally.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut over the Address Geocoding application from Geocodio v1.9 to v1.12 while adding targeted hardening (env-driven version, guarded location lookup, deprecation signal capture, census enrichment metric, parser fixture tests) so future version bumps are cheap.

**Architecture:** Single-file change to `GeocodeAddresses.py`. Adds five surgical edits and a new `tests/` tree pinned to a captured v1.9 response fixture. No new modules, no IC schema changes, no Snowflake VARIANT shape changes. Pytest is added as a dev-only dependency via a separate `requirements-dev.txt` not bundled into the Docker image.

**Tech Stack:** Python 3.13, requests, pandas, pyodbc (SQL Server), snowflake-connector-python, app_logger (vendored), pytest (new dev dep).

**Spec:** `docs/superpowers/specs/2026-04-30-geocodio-api-upgrade-design.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `GeocodeAddresses.py` | Modify | Apply 5 surgical changes (env var, location guard, deprecation scan, metric, helper extraction) |
| `tests/__init__.py` | Create | Empty marker (so pytest discovers `tests/`) |
| `tests/fixtures/__init__.py` | Create | Empty marker |
| `tests/fixtures/geocodio_v19_with_deprecation.json` | Create | Live v1.9 Thornton, CO response captured by user (includes `_warnings`) |
| `tests/fixtures/geocodio_v19_no_results.json` | Create | Empty-results response (synthetic, minimal) |
| `tests/fixtures/geocodio_v19_missing_location.json` | Create | Synthetic: success fixture with `results[0].location` deleted |
| `tests/test_parse_geocode_results.py` | Create | 5 unit tests pinning parser contract |
| `requirements-dev.txt` | Create | `pytest` only |
| `.env.example` | Modify | Replace `GEOCODIO_API_VERSION=vx.xx` with `GEOCODIO_API_VERSION=v1.12` |

---

## Task 1: Add pytest dev dependency and tests/ skeleton

**Files:**
- Create: `requirements-dev.txt`
- Create: `tests/__init__.py`
- Create: `tests/fixtures/__init__.py`

- [ ] **Step 1: Create requirements-dev.txt**

Create `/Users/fullerb/Projects/Address-Geocoding/requirements-dev.txt`:

```
pytest>=8.0
```

- [ ] **Step 2: Create empty test package markers**

Create `/Users/fullerb/Projects/Address-Geocoding/tests/__init__.py` (empty file).
Create `/Users/fullerb/Projects/Address-Geocoding/tests/fixtures/__init__.py` (empty file).

- [ ] **Step 3: Install pytest into the local venv**

Run: `pip install -r requirements-dev.txt`
Expected: pytest installed (version 8.x or later).

- [ ] **Step 4: Verify pytest discovers the tests/ directory**

Run: `cd /Users/fullerb/Projects/Address-Geocoding && python -m pytest tests/ --collect-only`
Expected: `no tests ran` or `0 tests collected` (no failures, no errors). The directory is recognized.

- [ ] **Step 5: Commit**

```bash
cd /Users/fullerb/Projects/Address-Geocoding
git add requirements-dev.txt tests/__init__.py tests/fixtures/__init__.py
git commit -m "$(cat <<'EOF'
Add pytest dev dependency and tests/ skeleton

Pytest is dev-only (not in the Docker image) — added via a separate
requirements-dev.txt so production installs stay lean.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Add the captured v1.9 response fixture

**Files:**
- Create: `tests/fixtures/geocodio_v19_with_deprecation.json`

- [ ] **Step 1: Save the user-supplied Thornton, CO response verbatim**

Create `/Users/fullerb/Projects/Address-Geocoding/tests/fixtures/geocodio_v19_with_deprecation.json` with the exact JSON the user pasted in the brainstorming session. The file's top-level keys must be `_warnings`, `input`, and `results`. Verify three results exist, the first has `accuracy: 0.59` and `accuracy_type: "street_center"`, and `_warnings[0]` contains the literal string `"There is a newer API version available, please consider upgrading to v1.12."`.

The JSON content is the full payload from the brainstorming conversation — keep it byte-identical so the fixture stays a faithful contract pin. No reformatting beyond what the user pasted.

- [ ] **Step 2: Verify the fixture parses as valid JSON**

Run:
```bash
python -c "import json; d=json.load(open('tests/fixtures/geocodio_v19_with_deprecation.json')); print(len(d['results']), d['_warnings'][0][:60])"
```

Expected output:
```
3 There is a newer API version available, please consider up
```

- [ ] **Step 3: Commit**

```bash
git add tests/fixtures/geocodio_v19_with_deprecation.json
git commit -m "$(cat <<'EOF'
Add captured v1.9 Geocodio response fixture

Real v1.9 response for a Thornton, CO address (public, FERPA-safe)
including the API deprecation warning. Used to pin parser behavior
across version upgrades.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Add the no-results and missing-location fixtures

**Files:**
- Create: `tests/fixtures/geocodio_v19_no_results.json`
- Create: `tests/fixtures/geocodio_v19_missing_location.json`

- [ ] **Step 1: Create no-results fixture**

Create `/Users/fullerb/Projects/Address-Geocoding/tests/fixtures/geocodio_v19_no_results.json`:

```json
{
  "input": {
    "address_components": {},
    "formatted_address": "asdfasdf"
  },
  "results": []
}
```

- [ ] **Step 2: Create missing-location fixture from the success fixture**

Run this Python one-liner to derive the missing-location fixture:

```bash
cd /Users/fullerb/Projects/Address-Geocoding
python -c "
import json
src = json.load(open('tests/fixtures/geocodio_v19_with_deprecation.json'))
src['results'][0].pop('location', None)
json.dump(src, open('tests/fixtures/geocodio_v19_missing_location.json', 'w'), indent=2)
print('ok')
"
```

Expected output: `ok`

- [ ] **Step 3: Verify both fixtures parse**

Run:
```bash
python -c "
import json
a = json.load(open('tests/fixtures/geocodio_v19_no_results.json'))
b = json.load(open('tests/fixtures/geocodio_v19_missing_location.json'))
assert a['results'] == [], 'no_results fixture should have empty results'
assert 'location' not in b['results'][0], 'missing_location fixture should have no location key'
print('ok')
"
```

Expected output: `ok`

- [ ] **Step 4: Commit**

```bash
git add tests/fixtures/geocodio_v19_no_results.json tests/fixtures/geocodio_v19_missing_location.json
git commit -m "$(cat <<'EOF'
Add no-results and missing-location parser test fixtures

The no-results fixture is synthetic minimal JSON; the missing-location
fixture is derived from the success fixture with results[0].location
deleted, exercising the new .get() guard.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Add `_failed_row` helper (TDD — write failing test first)

This task introduces the helper that deduplicates the failed-row dict literal currently appearing twice in `parse_geocode_results` at `GeocodeAddresses.py:521-535` and `543-558`.

**Files:**
- Create: `tests/test_parse_geocode_results.py`
- Modify: `GeocodeAddresses.py` (add helper function, no caller changes yet)

- [ ] **Step 1: Write the failing test for `_failed_row`**

Create `/Users/fullerb/Projects/Address-Geocoding/tests/test_parse_geocode_results.py`:

```python
"""Unit tests for parse_geocode_results and supporting helpers."""

import json
import os
import sys
from pathlib import Path

# Ensure the repo root is on sys.path so we can import GeocodeAddresses.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Set required env vars so module-level config validation passes on import.
os.environ.setdefault("SNOWFLAKE_USER", "test")
os.environ.setdefault("SNOWFLAKE_PRIVATE_KEY_B64", "test")
os.environ.setdefault("GEOCODIO_API_KEY", "test")
os.environ.setdefault("IC_DB_USER", "test")
os.environ.setdefault("IC_DB_PASSWORD", "test")

import GeocodeAddresses as ga  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_failed_row_helper_consistency():
    """_failed_row produces the canonical failed-row shape."""
    row = ga._failed_row(123, "1 Main St", {"results": []})
    assert row == {
        "ADDRESSID": 123,
        "ADDRESSLINE1": "1 Main St",
        "success": False,
        "latitude": None,
        "longitude": None,
        "accuracy": 0.0,
        "accuracy_type": None,
        "source": None,
        "block_code": None,
        "tract_code": None,
        "county_fips": None,
        "lea_code": "0000",
        "district_name": "none",
        "raw_response": {"results": []},
    }
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /Users/fullerb/Projects/Address-Geocoding && python -m pytest tests/test_parse_geocode_results.py::test_failed_row_helper_consistency -v`
Expected: FAIL with `AttributeError: module 'GeocodeAddresses' has no attribute '_failed_row'`.

- [ ] **Step 3: Add the `_failed_row` helper to `GeocodeAddresses.py`**

Open `/Users/fullerb/Projects/Address-Geocoding/GeocodeAddresses.py`. Insert this helper directly above the `parse_geocode_results` function (around line 477, immediately before the `def parse_geocode_results(` line):

```python
def _failed_row(addressid, addressline1, raw_response) -> dict:
    """Canonical failed-row shape used when geocoding cannot produce coordinates.

    Centralized here so changes to the parser output schema only happen in one place.
    """
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

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd /Users/fullerb/Projects/Address-Geocoding && python -m pytest tests/test_parse_geocode_results.py::test_failed_row_helper_consistency -v`
Expected: PASS (1 passed).

- [ ] **Step 5: Commit**

```bash
git add tests/test_parse_geocode_results.py GeocodeAddresses.py
git commit -m "$(cat <<'EOF'
Add _failed_row helper with consistency test

Centralizes the failed-row dict shape so parser output stays consistent
across the three places that produce it. Caller migration in next commit.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Migrate `parse_geocode_results` callers to use `_failed_row` and guard `location`

This task changes the two existing failed-row literals in `parse_geocode_results` to call `_failed_row()`, and adds the guarded `location` lookup that creates a third caller of `_failed_row()`.

**Files:**
- Modify: `GeocodeAddresses.py:518-558` (None-result branch + empty-results branch)
- Modify: `GeocodeAddresses.py:565-571` (location read in the success path)
- Modify: `tests/test_parse_geocode_results.py` (add three behavioral tests)

- [ ] **Step 1: Add the three failing tests for parser behavior**

Append to `/Users/fullerb/Projects/Address-Geocoding/tests/test_parse_geocode_results.py`:

```python
import pandas as pd  # noqa: E402


def _addresses_df():
    return pd.DataFrame([{"ADDRESSID": 42, "ADDRESSLINE1": "13515 Thornton Pkwy"}])


def test_success_extracts_all_fields():
    """Parser extracts location, accuracy, census, and school district from a real v1.9 response."""
    fixture = _load("geocodio_v19_with_deprecation.json")
    df = ga.parse_geocode_results([fixture], _addresses_df())
    assert len(df) == 1
    row = df.iloc[0]
    assert row["success"] is True
    assert row["latitude"] == 39.870709
    assert row["longitude"] == -104.968308
    assert row["accuracy"] == 0.59
    assert row["accuracy_type"] == "street_center"
    assert row["block_code"] == "2013"
    assert row["tract_code"] == "009204"
    assert row["county_fips"] == "08001"
    assert row["lea_code"] == "0806900"
    assert row["district_name"] == "Adams 12 Five Star Schools"
    assert row["raw_response"] == fixture


def test_no_results_returns_failed_row():
    """An API response with results=[] yields a failed row, not an exception."""
    fixture = _load("geocodio_v19_no_results.json")
    df = ga.parse_geocode_results([fixture], _addresses_df())
    assert len(df) == 1
    row = df.iloc[0]
    assert row["success"] is False
    assert row["latitude"] is None
    assert row["longitude"] is None
    assert row["lea_code"] == "0000"
    assert row["district_name"] == "none"
    assert row["raw_response"] == fixture


def test_missing_location_returns_failed_row():
    """A result missing the location key does not raise — it produces a failed row."""
    fixture = _load("geocodio_v19_missing_location.json")
    df = ga.parse_geocode_results([fixture], _addresses_df())
    assert len(df) == 1
    row = df.iloc[0]
    assert row["success"] is False
    assert row["latitude"] is None
    assert row["longitude"] is None
    assert row["raw_response"] == fixture
```

- [ ] **Step 2: Run the three new tests to verify they fail**

Run:
```bash
cd /Users/fullerb/Projects/Address-Geocoding
python -m pytest tests/test_parse_geocode_results.py::test_success_extracts_all_fields tests/test_parse_geocode_results.py::test_no_results_returns_failed_row tests/test_parse_geocode_results.py::test_missing_location_returns_failed_row -v
```

Expected: `test_success_extracts_all_fields` and `test_no_results_returns_failed_row` may PASS already (existing parser handles them); `test_missing_location_returns_failed_row` MUST FAIL with a `KeyError: 'location'` raised from inside `parse_geocode_results`.

If `test_success_extracts_all_fields` fails, inspect the diff between fixture values and assertions — the assertions are derived from the live response, so a mismatch means the fixture was altered.

- [ ] **Step 3: Replace the duplicated failed-row literals with `_failed_row()` calls**

In `/Users/fullerb/Projects/Address-Geocoding/GeocodeAddresses.py`, find the block at the original lines 518-535 (the `if address_result is None:` branch inside `parse_geocode_results`):

```python
            # Check if this address had an error (result is None)
            if address_result is None:
                logger.warning(f"No results for address ID {original_addressid}")
                parsed_rows.append({
                    'ADDRESSID': original_addressid,
                    'ADDRESSLINE1': original_address,
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
                    'raw_response': None
                })
                continue
```

Replace it with:

```python
            # Check if this address had an error (result is None)
            if address_result is None:
                logger.warning(f"No results for address ID {original_addressid}")
                parsed_rows.append(_failed_row(original_addressid, original_address, None))
                continue
```

Find the block at the original lines 540-559 (the `if len(results_list) == 0:` branch):

```python
            # Check if API returned no results for this address
            results_list = address_result.get('results', [])
            if len(results_list) == 0:
                logger.warning(f"No geocoding results for address ID {original_addressid}")
                parsed_rows.append({
                    'ADDRESSID': original_addressid,
                    'ADDRESSLINE1': original_address,
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
                    'raw_response': address_result
                })
                continue
```

Replace it with:

```python
            # Check if API returned no results for this address
            results_list = address_result.get('results', [])
            if len(results_list) == 0:
                logger.warning(f"No geocoding results for address ID {original_addressid}")
                parsed_rows.append(_failed_row(original_addressid, original_address, address_result))
                continue
```

- [ ] **Step 4: Guard the `location` lookup in the success path**

In the same file, find the success-path location read (originally lines 564-566):

```python
            # Extract basic location data
            latitude = result['location']['lat']
            longitude = result['location']['lng']
```

Replace those three lines with:

```python
            # Extract basic location data — guard against missing/null location.
            location = result.get('location') or {}
            latitude = location.get('lat')
            longitude = location.get('lng')
            if latitude is None or longitude is None:
                logger.warning(f"Missing location for address ID {original_addressid}")
                parsed_rows.append(_failed_row(original_addressid, original_address, address_result))
                continue
```

- [ ] **Step 5: Run all four parser tests to verify they pass**

Run:
```bash
cd /Users/fullerb/Projects/Address-Geocoding
python -m pytest tests/test_parse_geocode_results.py -v
```

Expected: 4 passed (the original `test_failed_row_helper_consistency` plus the three behavioral tests).

- [ ] **Step 6: Commit**

```bash
git add GeocodeAddresses.py tests/test_parse_geocode_results.py
git commit -m "$(cat <<'EOF'
Guard location lookup and migrate to _failed_row helper

The success-path now treats missing/null location as a failed row instead
of raising KeyError, which would abort the whole batch. Both pre-existing
failed-row branches now call _failed_row() so the parser output schema
lives in one place.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Add `_scan_payload_for_deprecation` and `_check_deprecation`

**Files:**
- Modify: `GeocodeAddresses.py` (add module-level state, two helpers, wire into `geocode_address`)
- Modify: `tests/test_parse_geocode_results.py` (add deprecation-scan test)

- [ ] **Step 1: Write the failing deprecation-scan test**

Append to `/Users/fullerb/Projects/Address-Geocoding/tests/test_parse_geocode_results.py`:

```python
def test_deprecation_scan_finds_warnings_entry():
    """The scan returns the deprecation notice from _warnings; returns None for clean payloads."""
    fixture = _load("geocodio_v19_with_deprecation.json")
    notice = ga._scan_payload_for_deprecation(fixture)
    assert notice is not None
    assert "newer API version" in notice
    assert "v1.12" in notice

    clean = {"results": [], "input": {}}
    assert ga._scan_payload_for_deprecation(clean) is None
```

- [ ] **Step 2: Run the new test to verify it fails**

Run: `cd /Users/fullerb/Projects/Address-Geocoding && python -m pytest tests/test_parse_geocode_results.py::test_deprecation_scan_finds_warnings_entry -v`
Expected: FAIL with `AttributeError: module 'GeocodeAddresses' has no attribute '_scan_payload_for_deprecation'`.

- [ ] **Step 3: Add module-level deprecation state and helpers**

In `/Users/fullerb/Projects/Address-Geocoding/GeocodeAddresses.py`, locate the comment block and `logger = logging.getLogger(__name__)` line near the original line 86. Directly below that line, insert:

```python


# One-shot flag so we don't spam 500 deprecation warnings per batch.
_deprecation_warned = False


def _scan_payload_for_deprecation(payload: dict) -> str | None:
    """Return the first deprecation notice in the payload, or None.

    Geocodio's canonical surface is the top-level `_warnings` array.
    Fallback: scan top-level string values for deprecation/upgrade hints.
    """
    if not isinstance(payload, dict):
        return None
    for warning in payload.get("_warnings", []) or []:
        if isinstance(warning, str) and (
            "newer API version" in warning or "deprecat" in warning.lower()
        ):
            return warning
    for value in payload.values():
        if isinstance(value, str) and (
            "deprecat" in value.lower() or "newer API version" in value
        ):
            return value
    return None


def _check_deprecation(response, payload) -> None:
    """Log a one-shot WARNING if the response carries a deprecation signal."""
    global _deprecation_warned
    if _deprecation_warned:
        return
    notice = (
        response.headers.get("Sunset")
        or response.headers.get("Deprecation")
        or _scan_payload_for_deprecation(payload)
    )
    if notice:
        logger.warning(
            "Geocodio API deprecation notice received: %s (current_version=%s)",
            notice,
            GEOCODIO_API_VERSION,
        )
        _deprecation_warned = True
```

- [ ] **Step 4: Wire `_check_deprecation` into `geocode_address`**

Find the `geocode_address` function (originally around line 390-412). Locate the body:

```python
    params = {
        "q": address,
        "api_key": api_key,
        "fields": ",".join(fields)
    }
    response = requests.get(GEOCODIO_API_URL, params=params, timeout=30)
    response.raise_for_status()
    return response.json()
```

Replace those lines with:

```python
    params = {
        "q": address,
        "api_key": api_key,
        "fields": ",".join(fields)
    }
    response = requests.get(GEOCODIO_API_URL, params=params, timeout=30)
    response.raise_for_status()
    payload = response.json()
    _check_deprecation(response, payload)
    return payload
```

- [ ] **Step 5: Run the deprecation test to verify it passes**

Run: `cd /Users/fullerb/Projects/Address-Geocoding && python -m pytest tests/test_parse_geocode_results.py::test_deprecation_scan_finds_warnings_entry -v`
Expected: PASS (1 passed).

- [ ] **Step 6: Run the full test suite to verify nothing regressed**

Run: `cd /Users/fullerb/Projects/Address-Geocoding && python -m pytest tests/ -v`
Expected: 5 passed.

- [ ] **Step 7: Commit**

```bash
git add GeocodeAddresses.py tests/test_parse_geocode_results.py
git commit -m "$(cat <<'EOF'
Capture Geocodio API deprecation notices once per process

Adds _scan_payload_for_deprecation (canonical _warnings array, with a
top-level fallback) and _check_deprecation (one-shot WARN log per process,
gated on a module flag). geocode_address now invokes the check on every
response, so the next deprecation surfaces in NDJSON instead of vanishing.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Parameterize the API version via env var

**Files:**
- Modify: `GeocodeAddresses.py:36-37` (URL constant)
- Modify: `.env.example` (replace placeholder default with `v1.12`)

- [ ] **Step 1: Replace the hardcoded URL constant**

In `/Users/fullerb/Projects/Address-Geocoding/GeocodeAddresses.py`, find lines 36-37:

```python
# Geocodio API configuration
GEOCODIO_API_URL = "https://api.geocod.io/v1.9/geocode"
```

Replace those two lines with:

```python
# Geocodio API configuration
GEOCODIO_API_VERSION = os.getenv('GEOCODIO_API_VERSION', 'v1.12')
GEOCODIO_API_URL = f"https://api.geocod.io/{GEOCODIO_API_VERSION}/geocode"
```

- [ ] **Step 2: Log the active version at config validation time**

Find the `validate_config` function and locate its final line:

```python
    logger.info("Configuration validated")
```

Replace that line with:

```python
    logger.info(f"Configuration validated (Geocodio API version: {GEOCODIO_API_VERSION})")
```

- [ ] **Step 3: Update `.env.example`**

In `/Users/fullerb/Projects/Address-Geocoding/.env.example`, find the line:

```
GEOCODIO_API_VERSION=vx.xx
```

Replace it with:

```
GEOCODIO_API_VERSION=v1.12
```

- [ ] **Step 4: Run the full test suite (smoke check the import still works)**

Run: `cd /Users/fullerb/Projects/Address-Geocoding && python -m pytest tests/ -v`
Expected: 5 passed.

- [ ] **Step 5: Manual sanity check — confirm URL renders correctly**

Run:
```bash
cd /Users/fullerb/Projects/Address-Geocoding
GEOCODIO_API_VERSION=v1.12 python -c "
import os
os.environ.setdefault('SNOWFLAKE_USER', 'test')
os.environ.setdefault('SNOWFLAKE_PRIVATE_KEY_B64', 'test')
os.environ.setdefault('GEOCODIO_API_KEY', 'test')
os.environ.setdefault('IC_DB_USER', 'test')
os.environ.setdefault('IC_DB_PASSWORD', 'test')
import GeocodeAddresses as ga
assert ga.GEOCODIO_API_URL == 'https://api.geocod.io/v1.12/geocode', ga.GEOCODIO_API_URL
print('ok:', ga.GEOCODIO_API_URL)
"
```

Expected output: `ok: https://api.geocod.io/v1.12/geocode`

- [ ] **Step 6: Commit**

```bash
git add GeocodeAddresses.py .env.example
git commit -m "$(cat <<'EOF'
Parameterize Geocodio API version via GEOCODIO_API_VERSION env var

Defaults to v1.12 so the deploy is the cutover. Future version bumps are
now a config change, not a code change. Active version is logged at
config validation so support can confirm from NDJSON.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Add `census_enrichment_rate` step metric

**Files:**
- Modify: `GeocodeAddresses.py` — inside `main()`, the `geocode_addresses` step block (originally around lines 911-921)

- [ ] **Step 1: Locate the existing `avg_accuracy` metadata line**

In `/Users/fullerb/Projects/Address-Geocoding/GeocodeAddresses.py`, find the `with log.step("geocode_addresses") as s:` block in `main()`:

```python
            with log.step("geocode_addresses") as s:
                api_results = batch_geocode_addresses(addresses_df, GEOCODIO_API_KEY)
                results_df = parse_geocode_results(api_results, addresses_df)
                success_count = int(results_df['success'].sum())
                failed_count = int((~results_df['success']).sum())
                s.rows = success_count
                s.rows_failed = failed_count
                if success_count > 0:
                    s.metadata["avg_accuracy"] = round(
                        results_df[results_df['success']]['accuracy'].mean(), 3
                    )
```

- [ ] **Step 2: Add the `census_enrichment_rate` calculation inside the same `if success_count > 0:` block**

Replace the `if success_count > 0:` block with:

```python
                if success_count > 0:
                    successful = results_df[results_df['success']]
                    s.metadata["avg_accuracy"] = round(
                        successful['accuracy'].mean(), 3
                    )
                    census_complete = successful[
                        successful['tract_code'].notna() & successful['block_code'].notna()
                    ]
                    s.metadata["census_enrichment_rate"] = round(
                        len(census_complete) / success_count, 3
                    )
```

- [ ] **Step 3: Run the full test suite (no regression check)**

Run: `cd /Users/fullerb/Projects/Address-Geocoding && python -m pytest tests/ -v`
Expected: 5 passed (this change is in `main()`, not exercised by parser tests).

- [ ] **Step 4: Commit**

```bash
git add GeocodeAddresses.py
git commit -m "$(cat <<'EOF'
Add census_enrichment_rate canary metric

Tracks the fraction of successful geocodes that produced both tract and
block codes. A sudden drop after a future version bump is the canary
signal for a silent census-shape change (e.g., Geocodio renames the
'2024' year key).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: Manual smoke test against v1.12 (no code change)

This task does not modify code. It validates the full pipeline end-to-end against the live v1.12 API before declaring the upgrade done. Skip only if you have no access to the dev container or the production secrets.

**Files:** None (verification only).

- [ ] **Step 1: Confirm `.env` has the new version set**

Run: `grep GEOCODIO_API_VERSION /Users/fullerb/Projects/Address-Geocoding/.env`
Expected: `GEOCODIO_API_VERSION=v1.12`

If the line is missing or set to a different version, add or update it before continuing.

- [ ] **Step 2: Run the script with a tiny batch**

Run:
```bash
cd /Users/fullerb/Projects/Address-Geocoding
BATCH_SIZE=1 python GeocodeAddresses.py
```

Expected: the script runs to completion. The final NDJSON log line should show `status: SUCCESS` (or `SKIPPED` if no addresses needed geocoding).

- [ ] **Step 3: Verify NDJSON output for the new metric and no deprecation warning**

Find today's NDJSON log file (e.g., `../LogReader/logs/Address_Geocoding/<timestamp>.ndjson`) and inspect the `geocode_addresses` step entry. It must contain `census_enrichment_rate` in metadata. Search the whole file for `"deprecation"` — it must NOT appear (we're now on v1.12, the recommended version).

Run:
```bash
LATEST=$(ls -t ../LogReader/logs/Address_Geocoding/*.ndjson | head -1)
grep -c "census_enrichment_rate" "$LATEST"
grep -c "deprecation" "$LATEST"
```

Expected: first command outputs `1` (or higher), second outputs `0`.

- [ ] **Step 4: Verify Snowflake VARIANT log received the v1.12 response**

In a Snowflake worksheet, run:
```sql
SELECT IC_ADDRESSID, GEOCODIO_JSON:_warnings AS warnings, GEOCODIO_JSON:results[0]:source::string AS source
FROM MPS_WAREHOUSE.DATALAKE.LOGGING_GEOCODIOAPI_ADDRESSLOOKUPS
ORDER BY IC_ADDRESSID DESC
LIMIT 1;
```

Expected: `warnings` is null or `[]` (no deprecation notice on v1.12). `source` is non-null. The most recent row corresponds to the smoke-test address.

- [ ] **Step 5: Verify IC `ADDRESS` row was updated**

In SQL Server Management Studio (or sqlcmd), look up the address you smoke-tested:

```sql
SELECT TOP 1 addressid, latitude, longitude, tract, block, location_code, districtID
FROM ADDRESS
WHERE addressid = <the test addressid>;
```

Expected: latitude and longitude are non-null and reasonable; tract, block, location_code are populated if the geocode accuracy was >= 0.8.

- [ ] **Step 6: Restore `BATCH_SIZE` to default (or whatever production uses)**

If you set `BATCH_SIZE=1` in `.env` for the smoke test, revert it. If you only set it inline (`BATCH_SIZE=1 python …`), nothing to do.

---

## Self-Review

Spec coverage check:

- Spec Goal 1 ("Cut over with no observable change") → Tasks 7, 9
- Spec Goal 2 ("Future version bumps cheap") → Task 7
- Spec Goal 3 ("Eliminate location KeyError") → Task 5
- Spec Goal 4 ("Surface next deprecation") → Task 6
- Spec Goal 5 ("census_enrichment_rate canary") → Task 8
- Spec Goal 6 ("Pin parser behavior to fixture") → Tasks 2, 3, 4, 5, 6
- Spec Change 1 (Version env var) → Task 7
- Spec Change 2 (Guarded location + `_failed_row`) → Tasks 4, 5
- Spec Change 3 (Deprecation capture) → Task 6
- Spec Change 4 (`census_enrichment_rate` metric) → Task 8
- Spec Change 5 (Fixtures + tests) → Tasks 1, 2, 3, 4, 5, 6
- Spec Rollout (manual smoke test) → Task 9

No gaps.

Type/name consistency:

- `_failed_row(addressid, addressline1, raw_response)` — same signature in Task 4 helper definition and all Task 5 call sites.
- `_scan_payload_for_deprecation(payload) -> str | None` — same in Task 6 definition and test.
- `_check_deprecation(response, payload)` — same in Task 6 definition and call site.
- `GEOCODIO_API_VERSION` / `GEOCODIO_API_URL` — same in Task 7 definition, `.env.example`, and Task 9 manual check.
- `census_enrichment_rate` metadata key — same in Task 8 implementation and Task 9 verification.

No drift.

No placeholders, no "TODO" / "TBD" / "similar to Task N" anywhere.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-04-30-geocodio-api-upgrade.md`. Two execution options:

1. **Subagent-Driven (recommended)** — Fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
