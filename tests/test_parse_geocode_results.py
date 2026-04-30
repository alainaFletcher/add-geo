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
