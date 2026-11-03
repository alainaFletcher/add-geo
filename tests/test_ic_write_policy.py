"""What gets written to Infinite Campus, and when.

IC fields written by this job are owned by the job (no end-user edits), so the
policy is: write exactly what the geocode says -- including blanks -- but only
for confident, house-level matches.
"""

import logging
from unittest.mock import MagicMock

import pandas as pd
import pytest

import GeocodeAddresses as ga


def _row(**overrides):
    row = {
        "ADDRESSID": 7, "success": True, "accuracy": 1.0, "accuracy_type": "rooftop",
        "latitude": 39.8, "longitude": -104.9, "tract_code": "009307",
        "block_code": "2018", "county_fips": "08001", "DISTRICTID": 17,
    }
    row.update(overrides)
    return row


def _ic_writes(rows):
    """Run update_ic_addresses on rows; return the (sql, params) of each UPDATE."""
    ic_conn = MagicMock()
    ga.update_ic_addresses(pd.DataFrame(rows), ic_conn)
    return [call.args for call in ic_conn.cursor.return_value.execute.call_args_list]


# --- accuracy ---------------------------------------------------------------

@pytest.mark.parametrize("missing", [float("nan"), None])
def test_missing_accuracy_is_never_written(missing):
    writes = _ic_writes([_row(ADDRESSID=1, accuracy=missing), _row(ADDRESSID=2)])
    assert [params[-1] for _, params in writes] == [2]


def test_missing_accuracy_alone_does_not_crash_the_step():
    assert _ic_writes([_row(accuracy=None)]) == []


@pytest.mark.parametrize("accuracy_type", [
    "street_center", "intersection", "place", "county", "state", None])
def test_imprecise_match_is_not_written_even_with_high_score(accuracy_type):
    assert _ic_writes([_row(accuracy_type=accuracy_type)]) == []


@pytest.mark.parametrize("accuracy_type", [
    "rooftop", "point", "range_interpolation", "nearest_rooftop_match"])
def test_house_level_match_is_written(accuracy_type):
    assert len(_ic_writes([_row(accuracy_type=accuracy_type)])) == 1


# --- what gets written ------------------------------------------------------

def test_full_result_writes_every_field():
    [(sql, params)] = _ic_writes([_row()])
    assert "districtID = ?" in sql
    assert params == (39.8, -104.9, "009307", "2018", "08001", 17, 7)
    assert type(params[5]) is int  # pyodbc needs a plain int, not numpy.int64


def test_missing_census_writes_blanks_and_warns(caplog):
    caplog.set_level(logging.WARNING)
    [(_, params)] = _ic_writes([_row(tract_code=None, block_code=None, county_fips=None)])
    assert params == (39.8, -104.9, None, None, None, 17, 7)
    assert any("census" in r.getMessage() and "7" in r.getMessage()
               for r in caplog.records if r.levelno == logging.WARNING)


def test_unmatched_district_clears_district_id():
    [(sql, params)] = _ic_writes([_row(DISTRICTID=0)])
    assert "districtID = ?" in sql
    assert params == (39.8, -104.9, "009307", "2018", "08001", None, 7)


# --- district matching ------------------------------------------------------

def _districts(rows):
    """Build the lookup the way fetch_district_lookup does, from (DISTRICTID, NCESDISTRICTID) rows."""
    cursor = MagicMock()
    cursor.fetch_pandas_all.return_value = pd.DataFrame(rows, columns=["DISTRICTID", "NCESDISTRICTID"])
    conn = MagicMock()
    conn.cursor.return_value = cursor
    return ga.fetch_district_lookup(conn)


def _match(lea_codes, districts_df):
    results = pd.DataFrame([{"ADDRESSID": i, "success": True, "lea_code": lea}
                            for i, lea in enumerate(lea_codes)])
    return list(ga.enrich_with_district_ids(results, districts_df)["DISTRICTID"])


def test_no_unified_district_never_matches_a_zero_id_row():
    districts = _districts([(99, "0"), (17, "0805550")])
    assert _match(["0000", "0805550"], districts) == [0, 17]


def test_duplicate_district_rows_with_the_same_id_still_match():
    districts = _districts([(17, "0805550"), (17, "0805550")])
    assert _match(["0805550"], districts) == [17]


def test_conflicting_district_rows_are_unmatched_and_warned(caplog):
    caplog.set_level(logging.WARNING)
    districts = _districts([(17, "0805550"), (18, "0805550"), (20, "0803000")])
    assert _match(["0805550", "0803000"], districts) == [0, 20]
    assert any("805550" in r.getMessage()
               for r in caplog.records if r.levelno == logging.WARNING)
