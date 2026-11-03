"""Unit tests for parse_geocode_results and supporting helpers."""

import json
import os
import sys
from pathlib import Path

# Ensure the repo root is on sys.path so we can import GeocodeAddresses.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# conftest.py already sets fake env vars before import; these are redundant fallbacks.
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


import pandas as pd  # noqa: E402


def _addresses_df():
    return pd.DataFrame([{"ADDRESSID": 42, "ADDRESSLINE1": "13515 Thornton Pkwy"}])


def test_success_extracts_all_fields():
    """Parser extracts location, accuracy, census2025, and school district from a real v2 response."""
    fixture = _load("geocodio_v2_with_census2025.json")
    df = ga.parse_geocode_results([fixture], _addresses_df())
    assert len(df) == 1
    row = df.iloc[0]
    assert row["success"]
    assert row["latitude"] == 39.830488
    assert row["longitude"] == -104.986263
    assert row["accuracy"] == 1
    assert row["accuracy_type"] == "rooftop"
    assert row["block_code"] == "2018"
    assert row["tract_code"] == "009307"
    assert row["county_fips"] == "08001"
    assert row["lea_code"] == "0805550"
    assert row["district_name"] == "Mapleton School District 1"
    assert row["raw_response"] == fixture


def test_no_results_returns_failed_row():
    """An API response with results=[] yields a failed row, not an exception."""
    fixture = _load("geocodio_v19_no_results.json")
    df = ga.parse_geocode_results([fixture], _addresses_df())
    assert len(df) == 1
    row = df.iloc[0]
    assert not row["success"]
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
    assert not row["success"]
    assert row["latitude"] is None
    assert row["longitude"] is None
    assert row["raw_response"] == fixture


def test_deprecation_scan_finds_warnings_entry():
    """The scan returns the deprecation notice from _warnings; returns None for clean payloads."""
    fixture = _load("geocodio_v19_with_deprecation.json")
    notice = ga._scan_payload_for_deprecation(fixture)
    assert notice is not None
    assert "newer API version" in notice
    assert "v1.12" in notice

    clean = {"results": [], "input": {}}
    assert ga._scan_payload_for_deprecation(clean) is None


def test_null_census_block_is_a_success_without_census():
    """census: {"2025": null} is a valid geocode with no census append, not a crash."""
    fixture = _load("geocodio_v2_with_census2025.json")
    fixture["results"][0]["fields"]["census"]["2025"] = None
    row = ga.parse_geocode_results([fixture], _addresses_df()).iloc[0]
    assert row["success"]
    assert row["tract_code"] is None
    assert row["lea_code"] == "0805550"


def test_malformed_response_fails_only_its_own_row():
    """One unexpected payload shape must not discard the rest of the (paid-for) batch."""
    good = _load("geocodio_v2_with_census2025.json")
    addresses = pd.DataFrame([
        {"ADDRESSID": 1, "ADDRESSLINE1": "1 Bad Payload St"},
        {"ADDRESSID": 2, "ADDRESSLINE1": "13515 Thornton Pkwy"},
    ])
    df = ga.parse_geocode_results([{"results": "not-a-list"}, good], addresses)
    assert list(df["success"]) == [False, True]
    assert df.iloc[1]["lea_code"] == "0805550"
