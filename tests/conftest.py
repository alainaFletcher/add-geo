"""Shared test setup.

Runs before any test module imports GeocodeAddresses, so the module-level
config constants are built from these fake values -- never from a developer's
real .env or exported shell credentials.
"""

import logging
import os

# Stop GeocodeAddresses' import-time load_dotenv() from reading the real .env
# (prod credentials, Teams webhook). Requires python-dotenv >= 1.2.0.
os.environ["PYTHON_DOTENV_DISABLED"] = "1"

# Fixed fakes (not setdefault) so real exported credentials never win.
os.environ["SNOWFLAKE_USER"] = "test-user"
os.environ["SNOWFLAKE_PRIVATE_KEY_B64"] = "test-key"
os.environ["GEOCODIO_API_KEY"] = "test-geocodio-key"
os.environ["IC_DB_USER"] = "test-ic-user"
os.environ["IC_DB_PASSWORD"] = "test-ic-password"

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _detach_app_logger_handlers():
    """Remove root-logger handlers a real AppLogger attached during a test."""
    yield
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_app_logger_managed", False):
            root.removeHandler(handler)
            handler.close()
