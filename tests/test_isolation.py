"""Guards on the test environment itself."""

import os

import dotenv


def test_dotenv_files_are_never_loaded_during_tests(tmp_path, monkeypatch):
    # A real .env carries prod credentials and a Teams webhook; conftest must
    # keep load_dotenv() (called when GeocodeAddresses is imported) inert.
    (tmp_path / ".env").write_text("SENTINEL_FROM_DOTENV=leaked\n")
    monkeypatch.chdir(tmp_path)
    dotenv.load_dotenv(tmp_path / ".env")
    assert "SENTINEL_FROM_DOTENV" not in os.environ
