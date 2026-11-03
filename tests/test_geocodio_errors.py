"""Geocodio error handling: which failures abort the run, and what must never leak."""

import json
import logging
import socket
import threading
import traceback
from pathlib import Path

import pandas as pd
import pytest
import requests
from requests.adapters import HTTPAdapter

import GeocodeAddresses as ga

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "geocodio_v2_with_census2025.json").read_text()
)

# Recognisable fake street name standing in for a student's address.
SECRET_STREET = "Zyxwvut"
# Same shape as the real key (39 lowercase hex chars), which app_logger's
# redaction does not catch.
API_KEY = "0123456789abcdef0123456789abcdef0123456"


def _addresses(n: int) -> pd.DataFrame:
    return pd.DataFrame([
        {"ADDRESSID": 100 + i, "ADDRESSLINE1": f"{i} {SECRET_STREET} Ln, Denver CO 80229"}
        for i in range(n)
    ])


class FakeGeocodio:
    """Stands in for the network: replays a script of HTTP statuses or exceptions.

    Builds real requests.Response objects so raise_for_status() produces the
    same message (full URL included) that production would.
    """

    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    def send(self, request, **kwargs):
        self.requests.append(request)
        step = self.script.pop(0)
        if isinstance(step, type) and issubclass(step, Exception):
            # Real connection errors embed the path and query string.
            raise step(f"Max retries exceeded with url: {request.path_url}", request=request)
        response = requests.Response()
        response.status_code = step
        response.url = request.url
        response.request = request
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps(FIXTURE if step == 200 else {"error": "nope"}).encode()
        return response


@pytest.fixture
def geocodio(monkeypatch):
    def install(script):
        fake = FakeGeocodio(script)
        monkeypatch.setattr(
            HTTPAdapter, "send", lambda adapter, request, **kw: fake.send(request, **kw)
        )
        return fake
    return install


def _full_error_text(exc: BaseException) -> str:
    """Everything app_logger would record: message plus the full chained traceback."""
    return "".join(traceback.format_exception(exc))


def test_api_key_travels_in_header_not_url(geocodio):
    fake = geocodio([200])
    ga.batch_geocode_addresses(_addresses(1), API_KEY)
    sent = fake.requests[0]
    assert API_KEY not in sent.url
    assert sent.headers["Authorization"] == f"Bearer {API_KEY}"


@pytest.mark.parametrize("status", [401, 403, 429])
def test_rejected_key_or_quota_aborts_run_on_first_call(geocodio, status):
    fake = geocodio([status, 200, 200])
    with pytest.raises(ga.GeocodioFatalError):
        ga.batch_geocode_addresses(_addresses(3), API_KEY)
    assert len(fake.requests) == 1


@pytest.mark.parametrize("status", [401, 403, 429])
def test_abort_error_contains_no_address_or_key(geocodio, status):
    geocodio([status])
    with pytest.raises(ga.GeocodioFatalError) as info:
        ga.batch_geocode_addresses(_addresses(1), API_KEY)
    text = _full_error_text(info.value)
    assert SECRET_STREET not in text
    assert API_KEY not in text


def test_bad_address_is_skipped_and_run_continues(geocodio):
    geocodio([422, 200])
    results = ga.batch_geocode_addresses(_addresses(2), API_KEY)
    assert results == [None, FIXTURE]


def test_five_consecutive_failures_abort_without_further_calls(geocodio):
    fake = geocodio([503] * 10)
    with pytest.raises(ga.GeocodioFatalError):
        ga.batch_geocode_addresses(_addresses(10), API_KEY)
    assert len(fake.requests) == 5


def test_connection_errors_trip_the_breaker_without_leaking_address(geocodio):
    geocodio([requests.exceptions.ConnectionError] * 5)
    with pytest.raises(ga.GeocodioFatalError) as info:
        ga.batch_geocode_addresses(_addresses(5), API_KEY)
    assert SECRET_STREET not in _full_error_text(info.value)


def test_success_resets_the_failure_count(geocodio):
    fake = geocodio([503] * 4 + [200] + [503] * 4 + [200])
    results = ga.batch_geocode_addresses(_addresses(10), API_KEY)
    assert len(fake.requests) == 10
    assert results[-1] == FIXTURE


def test_unparseable_address_does_not_reset_the_failure_count(geocodio):
    fake = geocodio([503] * 4 + [422] + [503] * 5)
    with pytest.raises(ga.GeocodioFatalError):
        ga.batch_geocode_addresses(_addresses(10), API_KEY)
    assert len(fake.requests) == 6


def test_total_outage_on_a_small_batch_aborts(geocodio):
    geocodio([503, 503, 503])
    with pytest.raises(ga.GeocodioFatalError):
        ga.batch_geocode_addresses(_addresses(3), API_KEY)


def test_batch_of_only_unparseable_addresses_does_not_abort(geocodio):
    geocodio([422, 422])
    assert ga.batch_geocode_addresses(_addresses(2), API_KEY) == [None, None]


def test_abort_hands_back_lookups_that_already_succeeded(geocodio):
    geocodio([200, 200, 403])
    with pytest.raises(ga.GeocodioFatalError) as info:
        ga.batch_geocode_addresses(_addresses(4), API_KEY)
    # One entry per address, so rows geocoded before the abort can still be saved.
    assert info.value.results == [FIXTURE, FIXTURE, None, None]


def test_missing_address_column_is_not_blamed_on_geocodio(geocodio):
    fake = geocodio([200] * 6)
    with pytest.raises(KeyError):
        ga.batch_geocode_addresses(pd.DataFrame({"ADDRESSID": range(6)}), API_KEY)
    assert fake.requests == []


@pytest.fixture
def malformed_header_server(monkeypatch):
    """Real local HTTP server whose response has a header urllib3 can't parse.

    Unlike FakeGeocodio this drives the real urllib3/http.client stack, which
    logs "Failed to parse headers (url=...)" -- full query string -- at WARNING.
    """
    body = json.dumps(FIXTURE).encode()
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(5)

    def serve():
        while True:
            try:
                client, _ = server.accept()
            except OSError:
                return
            client.recv(65536)
            client.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b"X-Cache-Status : MISS\r\n"  # space before colon: header defect
                + f"Content-Length: {len(body)}\r\n".encode()
                + b"Connection: close\r\n\r\n" + body)
            client.close()

    threading.Thread(target=serve, daemon=True).start()
    monkeypatch.setattr(
        ga, "GEOCODIO_API_URL", f"http://127.0.0.1:{server.getsockname()[1]}/v2/geocode")
    yield
    server.close()


def test_malformed_response_header_does_not_log_the_address(malformed_header_server, caplog):
    caplog.set_level(logging.DEBUG)
    results = ga.batch_geocode_addresses(_addresses(1), API_KEY)
    assert results == [FIXTURE]
    assert SECRET_STREET not in caplog.text
