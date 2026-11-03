"""Exception text from drivers can carry logins or student data; it must not reach logs.

app_logger records str(exc) and the traceback of anything escaping a step, so
errors raised out of connection helpers must be sanitized too, not just log lines.
"""

import logging
import traceback
from unittest.mock import MagicMock

import pandas as pd
import pyodbc
import pytest
import snowflake.connector.errors as sf_errors

import GeocodeAddresses as ga

SECRET_STREET = "Zyxwvut"


def _full_error_text(exc: BaseException) -> str:
    return "".join(traceback.format_exception(exc))


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    monkeypatch.setattr(ga.time, "sleep", lambda seconds: None)


@pytest.mark.parametrize("error", [
    pyodbc.InterfaceError("28000", "[28000] Login failed for user 'test-ic-user'. (18456)"),
    RuntimeError("driver crashed while logging in test-ic-user"),
], ids=["pyodbc-error", "unexpected-error"])
def test_ic_connection_failure_never_names_the_login(monkeypatch, caplog, error):
    caplog.set_level(logging.DEBUG)

    def refuse(connection_string):
        raise error
    monkeypatch.setattr(ga.pyodbc, "connect", refuse)

    with pytest.raises(Exception) as info:
        ga.create_ic_connection()

    assert "test-ic-user" not in _full_error_text(info.value) + caplog.text


def test_ic_connection_failure_keeps_sqlstate(monkeypatch):
    def refuse(connection_string):
        raise pyodbc.InterfaceError("28000", "[28000] Login failed for user 'x'. (18456)")
    monkeypatch.setattr(ga.pyodbc, "connect", refuse)

    with pytest.raises(Exception) as info:
        ga.create_ic_connection()
    assert "28000" in str(info.value)


@pytest.mark.parametrize("error_class", [sf_errors.DatabaseError, sf_errors.ProgrammingError])
def test_snowflake_connection_failure_never_names_the_user(monkeypatch, caplog, error_class):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(ga, "load_snowflake_private_key", lambda: b"der")

    def refuse(**kwargs):
        raise error_class(
            msg="JWT token is invalid for user test-user", errno=250001, sqlstate="08001")
    monkeypatch.setattr(ga.snowflake.connector, "connect", refuse)

    with pytest.raises(Exception) as info:
        ga.create_snowflake_connection()

    assert "test-user" not in _full_error_text(info.value) + caplog.text
    assert "250001" in str(info.value)  # errno survives for diagnosis


@pytest.mark.parametrize("fetch", [ga.fetch_addresses_to_geocode, ga.fetch_district_lookup])
def test_snowflake_query_failure_logs_codes_only(fetch, caplog):
    caplog.set_level(logging.DEBUG)
    conn = MagicMock()
    conn.cursor.return_value.execute.side_effect = sf_errors.ProgrammingError(
        msg="Insufficient privileges for user test-user", errno=3001, sqlstate="42501")

    with pytest.raises(Exception) as info:
        fetch(conn)

    assert "test-user" not in _full_error_text(info.value) + caplog.text
    assert "3001" in str(info.value)  # errno survives for diagnosis


@pytest.mark.parametrize("error", [
    sf_errors.ProgrammingError(
        msg=f"Error parsing JSON near '1 {SECRET_STREET} Ln'", errno=100069, sqlstate="22P02"),
    sf_errors.OperationalError(
        msg=f"Connection reset while sending '1 {SECRET_STREET} Ln'", errno=100069, sqlstate="08006"),
], ids=["programming-error", "unexpected-error"])
def test_audit_insert_failure_logs_no_response_content(caplog, error):
    caplog.set_level(logging.DEBUG)
    cursor = MagicMock()
    cursor.execute.side_effect = error
    conn = MagicMock()
    conn.cursor.return_value = cursor
    results = pd.DataFrame([{"ADDRESSID": 7, "raw_response": {"input": "x"}}])

    assert ga.log_results_to_snowflake(results, conn) == (0, 1)
    assert SECRET_STREET not in caplog.text
    assert "100069" in caplog.text  # errno survives for diagnosis


@pytest.mark.parametrize("error", [
    pyodbc.DataError("22003", f"[22003] Arithmetic overflow converting '1 {SECRET_STREET} Ln'"),
    ValueError(f"cannot convert '1 {SECRET_STREET} Ln'"),
], ids=["pyodbc-error", "unexpected-error"])
def test_ic_update_failure_logs_no_row_content(caplog, error):
    caplog.set_level(logging.DEBUG)
    ic_conn = MagicMock()
    ic_conn.cursor.return_value.execute.side_effect = error
    results = pd.DataFrame([{
        "ADDRESSID": 7, "success": True, "accuracy": 1.0, "accuracy_type": "rooftop", "latitude": 39.8,
        "longitude": -104.9, "tract_code": "009307", "block_code": "2018",
        "county_fips": "08001", "DISTRICTID": 17,
    }])

    assert ga.update_ic_addresses(results, ic_conn) == (0, 1)
    assert SECRET_STREET not in caplog.text
