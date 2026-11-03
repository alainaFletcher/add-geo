"""main(): exit code and recorded run status must reflect fatal failures."""

import json
import logging
import traceback
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

import GeocodeAddresses as ga

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "geocodio_v2_with_census2025.json").read_text()
)


def _boom(*args, **kwargs):
    raise RuntimeError("boom")


@pytest.fixture
def pipeline(monkeypatch, tmp_path):
    """main() with every external system faked.

    AppLogger is the real class (its status rules are what's under test) but
    writes only to tmp_path: no notifications, no Snowflake dual-write.
    """
    real_app_logger = ga.AppLogger

    def local_only_app_logger(*args, **kwargs):
        kwargs.update(log_root=tmp_path, notify=False,
                      snowflake_connection=None, purge_on_init=False)
        return real_app_logger(*args, **kwargs)

    def fake_snowflake(database=ga.SNOWFLAKE_DATABASE, schema=None):
        if database == "LOGS":
            raise RuntimeError("LOGS unavailable for user test-user")
        return MagicMock(name="snowflake")

    ic_conn = MagicMock(name="ic")
    monkeypatch.setattr(ga, "AppLogger", local_only_app_logger)
    monkeypatch.setattr(ga, "create_snowflake_connection", fake_snowflake)
    monkeypatch.setattr(ga, "fetch_addresses_to_geocode", lambda conn, limit: pd.DataFrame(
        [{"ADDRESSID": 42, "ADDRESSLINE1": "13515 Thornton Pkwy"}]))
    monkeypatch.setattr(ga, "fetch_district_lookup", lambda conn: pd.DataFrame(
        {"DISTRICTID": [17]}, index=pd.Index([805550], dtype="Int64", name="NCESDISTRICTID")))
    monkeypatch.setattr(ga, "batch_geocode_addresses", lambda df, key: [FIXTURE])
    monkeypatch.setattr(ga, "log_results_to_snowflake", lambda df, conn: (len(df), 0))
    monkeypatch.setattr(ga, "create_ic_connection", lambda: ic_conn)

    class Run:
        ic = ic_conn

        def __call__(self):
            """Run main(); return (exit code, status recorded in the run index)."""
            with pytest.raises(SystemExit) as exit_info:
                ga.main()
            index_file = next((tmp_path / "_index").glob("runs_*.jsonl"))
            self.record = json.loads(index_file.read_text().splitlines()[-1])
            return exit_info.value.code, self.record["status"]

    return Run()


def test_successful_run_exits_zero_with_success_status(pipeline):
    assert pipeline() == (0, "SUCCESS")


def test_no_addresses_exits_zero_with_skipped_status(pipeline, monkeypatch):
    monkeypatch.setattr(ga, "fetch_addresses_to_geocode", lambda conn, limit: pd.DataFrame(
        columns=["ADDRESSID", "ADDRESSLINE1"]))
    assert pipeline() == (0, "SKIPPED")


@pytest.mark.parametrize("failing", [
    "fetch_addresses_to_geocode",
    "fetch_district_lookup",
    "batch_geocode_addresses",
    "create_ic_connection",
])
def test_fatal_step_failure_exits_nonzero_with_failed_status(pipeline, monkeypatch, failing):
    monkeypatch.setattr(ga, failing, _boom)
    assert pipeline() == (1, "FAILED")


def test_snowflake_connection_failure_exits_nonzero_with_failed_status(pipeline, monkeypatch):
    def snowflake_down(database=ga.SNOWFLAKE_DATABASE, schema=None):
        raise RuntimeError("snowflake down")
    monkeypatch.setattr(ga, "create_snowflake_connection", snowflake_down)
    assert pipeline() == (1, "FAILED")
    assert [s["step"] for s in pipeline.record["failed_steps"]] == ["connect_snowflake"]


def test_geocodio_abort_still_writes_rows_geocoded_before_it(pipeline, monkeypatch):
    monkeypatch.setattr(ga, "fetch_addresses_to_geocode", lambda conn, limit: pd.DataFrame([
        {"ADDRESSID": 42, "ADDRESSLINE1": "13515 Thornton Pkwy"},
        {"ADDRESSID": 43, "ADDRESSLINE1": "13516 Thornton Pkwy"},
    ]))

    def quota_hit_on_second_address(df, key):
        raise ga.GeocodioFatalError("Geocodio rejected the request (HTTP 403)",
                                    results=[FIXTURE, None])
    monkeypatch.setattr(ga, "batch_geocode_addresses", quota_hit_on_second_address)

    assert pipeline() == (1, "FAILED")
    updates = pipeline.ic.cursor.return_value.execute.call_args_list
    assert [call.args[1][-1] for call in updates] == [42]  # WHERE addressid = ?
    assert [s["step"] for s in pipeline.record["failed_steps"]] == ["geocode_addresses"]


def test_snowflake_close_failure_does_not_fail_a_finished_run(pipeline, monkeypatch):
    def flaky_close_snowflake(database=ga.SNOWFLAKE_DATABASE, schema=None):
        if database == "LOGS":
            raise RuntimeError("LOGS unavailable")
        conn = MagicMock(name="snowflake")
        conn.close.side_effect = RuntimeError("logout failed")
        # Like the real connector: leaving a `with` block always calls close().
        conn.__exit__.side_effect = lambda *exc_info: conn.close()
        return conn
    monkeypatch.setattr(ga, "create_snowflake_connection", flaky_close_snowflake)
    assert pipeline() == (0, "SUCCESS")


def test_audit_log_failure_is_non_fatal_and_ic_still_updated(pipeline, monkeypatch):
    monkeypatch.setattr(ga, "log_results_to_snowflake", _boom)
    assert pipeline() == (0, "PARTIAL")
    assert pipeline.ic.cursor.return_value.execute.called


def test_sigterm_keeps_exit_code_143_and_records_failed(pipeline, monkeypatch):
    def terminated(*args, **kwargs):
        raise SystemExit(143)  # what app_logger's SIGTERM handler raises
    monkeypatch.setattr(ga, "batch_geocode_addresses", terminated)
    assert pipeline() == (143, "FAILED")


def test_ctrl_c_traceback_does_not_chain_earlier_errors(pipeline, monkeypatch):
    def interrupted_while_handling_driver_error(df, key):
        try:
            raise RuntimeError("Login failed for user 'test-ic-user'")
        except RuntimeError:
            raise KeyboardInterrupt  # e.g. Ctrl-C during a retry sleep
    monkeypatch.setattr(ga, "batch_geocode_addresses", interrupted_while_handling_driver_error)

    with pytest.raises(KeyboardInterrupt) as info:
        ga.main()
    # Python prints this traceback to stderr (Docker log) on the way out.
    assert "test-ic-user" not in "".join(traceback.format_exception(info.value))


def test_logs_connection_warning_omits_error_text(pipeline, caplog):
    caplog.set_level(logging.DEBUG)
    pipeline()
    assert "test-user" not in caplog.text


def test_logs_connection_failure_adds_no_unmanaged_root_handler(pipeline, monkeypatch):
    # Production starts with no root handlers; logging.warning() would then
    # basicConfig() a raw stderr handler that bypasses app_logger redaction.
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])
    pipeline()
    assert [h for h in root.handlers if not getattr(h, "_app_logger_managed", False)] == []
