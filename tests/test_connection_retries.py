"""Which connection failures are retried.

Retry only what can heal by waiting (network not ready yet). Auth/config
errors fail the same way every time, and retrying a rejected IC login only
moves the service account toward a lockout.
"""

import pyodbc
import pytest
import snowflake.connector.errors as sf_errors

import GeocodeAddresses as ga


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    monkeypatch.setattr(ga.time, "sleep", lambda seconds: None)


def _count_calls(monkeypatch, target, attr, outcomes):
    """Patch target.attr to return/raise each of outcomes in turn; return the call log."""
    calls = []

    def fake(*args, **kwargs):
        outcome = outcomes[min(len(calls), len(outcomes) - 1)]
        calls.append(1)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome
    monkeypatch.setattr(target, attr, fake)
    return calls


# --- Snowflake --------------------------------------------------------------

def _network_down():
    return sf_errors.OperationalError(msg="Could not connect", errno=250003, sqlstate="08001")


@pytest.fixture
def snowflake_key(monkeypatch):
    monkeypatch.setattr(ga, "load_snowflake_private_key", lambda: b"der")


def test_snowflake_network_error_is_retried_until_it_connects(monkeypatch, snowflake_key):
    conn = object()
    calls = _count_calls(monkeypatch, ga.snowflake.connector, "connect",
                         [_network_down(), _network_down(), conn])
    assert ga.create_snowflake_connection() is conn
    assert len(calls) == 3


def test_snowflake_network_error_gives_up_after_max_retries(monkeypatch, snowflake_key):
    calls = _count_calls(monkeypatch, ga.snowflake.connector, "connect", [_network_down()])
    with pytest.raises(ConnectionError):
        ga.create_snowflake_connection()
    assert len(calls) == 3


@pytest.mark.parametrize("error_class", [sf_errors.DatabaseError, sf_errors.ProgrammingError])
def test_snowflake_auth_or_config_error_is_not_retried(monkeypatch, snowflake_key, error_class):
    calls = _count_calls(monkeypatch, ga.snowflake.connector, "connect", [
        error_class(msg="JWT token is invalid", errno=250001, sqlstate="08001")])
    with pytest.raises(ConnectionError):
        ga.create_snowflake_connection()
    assert len(calls) == 1


# --- Infinite Campus --------------------------------------------------------

def test_ic_rejected_login_is_not_retried(monkeypatch):
    calls = _count_calls(monkeypatch, ga.pyodbc, "connect", [
        pyodbc.InterfaceError("28000", "[28000] Login failed for user 'x'. (18456)")])
    with pytest.raises(ConnectionError) as info:
        ga.create_ic_connection()
    assert len(calls) == 1
    assert "28000" in str(info.value)


def test_ic_missing_odbc_driver_fails_fast_and_names_it(monkeypatch):
    monkeypatch.setattr(ga.pyodbc, "drivers", lambda: [])
    calls = _count_calls(monkeypatch, ga.pyodbc, "connect", [pyodbc.Error(
        "01000", "[01000] [unixODBC][Driver Manager]Can't open lib "
        "'ODBC Driver 18 for SQL Server' : file not found (0) (SQLDriverConnect)")])
    with pytest.raises(ConnectionError, match="is not installed") as info:
        ga.create_ic_connection()
    assert len(calls) == 1
    assert ga.IC_DB_DRIVER in str(info.value)


def test_ic_01000_with_driver_installed_is_still_retried(monkeypatch):
    monkeypatch.setattr(ga.pyodbc, "drivers", lambda: [ga.IC_DB_DRIVER])
    conn = object()
    calls = _count_calls(monkeypatch, ga.pyodbc, "connect", [
        pyodbc.Error("01000", "[01000] some other warning-class failure"), conn])
    assert ga.create_ic_connection() is conn
    assert len(calls) == 2


def test_ic_network_error_is_retried(monkeypatch):
    conn = object()
    calls = _count_calls(monkeypatch, ga.pyodbc, "connect", [
        pyodbc.OperationalError("08001", "[08001] TCP Provider: timeout"), conn])
    assert ga.create_ic_connection() is conn
    assert len(calls) == 2
