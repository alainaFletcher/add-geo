#!/usr/bin/env python3
"""
Address Geocoding Application

This application geocodes addresses from Infinite Campus
using the Geocodio API. It looks up addresses individually, enriches them with
census and school district data, and updates both Infinite Campus and Snowflake
databases.
"""

import os
import re
import sys
import logging
import time
import base64
from typing import Tuple

from app_logger import AppLogger, ExitCode

# Set ODBC driver path for macOS with Homebrew installation
# This must be done before importing pyodbc
if sys.platform == 'darwin' and not os.environ.get('ODBCSYSINI'):
    homebrew_etc = '/opt/homebrew/etc'
    if os.path.exists(f'{homebrew_etc}/odbcinst.ini'):
        os.environ['ODBCSYSINI'] = homebrew_etc

import requests
import pandas as pd
import pyodbc
import snowflake.connector
from snowflake.connector import SnowflakeConnection
import snowflake.connector.errors as sf_errors
from cryptography.hazmat.primitives import serialization
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


# ============================================
# Configuration from Environment Variables
# ============================================

# Snowflake Data Warehouse
SNOWFLAKE_USER = os.getenv('SNOWFLAKE_USER')
SNOWFLAKE_PRIVATE_KEY_B64 = os.getenv('SNOWFLAKE_PRIVATE_KEY_B64')
SNOWFLAKE_PRIVATE_KEY_PASSPHRASE = os.getenv('SNOWFLAKE_PRIVATE_KEY_PASSPHRASE')  # optional
SNOWFLAKE_ACCOUNT = os.getenv('SNOWFLAKE_ACCOUNT', 'dv56305.west-us-2.azure')
SNOWFLAKE_WAREHOUSE = os.getenv('SNOWFLAKE_WAREHOUSE', 'dataloadingonly')
SNOWFLAKE_DATABASE = os.getenv('SNOWFLAKE_DATABASE', 'MPS_Warehouse')
SNOWFLAKE_SCHEMA = os.getenv('SNOWFLAKE_SCHEMA', 'datalake')

# Geocodio API
GEOCODIO_API_KEY = os.getenv('GEOCODIO_API_KEY')
GEOCODIO_API_VERSION = os.getenv('GEOCODIO_API_VERSION', 'v2.1')
GEOCODIO_API_URL = f"https://api.geocod.io/{GEOCODIO_API_VERSION}/geocode"

# Infinite Campus Database (SQL Server)
IC_DB_SERVER = os.getenv('IC_DB_SERVER', 'icampus.mapleton.us,7771')
IC_DB_DATABASE = os.getenv('IC_DB_DATABASE', 'mapleton')
IC_DB_USER = os.getenv('IC_DB_USER')
IC_DB_PASSWORD = os.getenv('IC_DB_PASSWORD')
IC_DB_DRIVER = os.getenv('IC_DB_DRIVER', 'ODBC Driver 18 for SQL Server')

# Application Settings
BATCH_SIZE = int(os.getenv('BATCH_SIZE', '500'))
ACCURACY_THRESHOLD = float(os.getenv('ACCURACY_THRESHOLD', '0.8'))
API_FIELDS = ["school", "zip4", "census2025"]

# Geocodio statuses that doom every later call in the batch: 403 = invalid key
# or daily quota exhausted (per Geocodio docs), 429 = pay-as-you-go rate limit.
GEOCODIO_FATAL_STATUSES = frozenset({401, 403, 429})
# Stop calling Geocodio after this many failures in a row (outage, network).
MAX_CONSECUTIVE_GEOCODE_FAILURES = 5

# SQL Queries
QUERY_ADDRESSES = """
    SELECT TOP {limit} *
    FROM MPS_WAREHOUSE.DATALAKE.VIEW_ADDRESSESNEEDINGGEOCODED
    ORDER BY addressid DESC
"""

QUERY_DISTRICTS = """
    SELECT DISTRICTID, NCESDISTRICTID
    FROM MPS_WAREHOUSE.IC.DISTRICT
"""


# Module-level logger — AppLogger's configure_root_logger=True attaches
# NDJSON handlers to the root logger, so this automatically gets structured output.
logger = logging.getLogger(__name__)


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
            "Geocodio API deprecation notice received: %s (url=%s)",
            notice,
            GEOCODIO_API_URL,
        )
        _deprecation_warned = True


def _describe_db_error(exc: Exception) -> str:
    """Summarize a driver exception without its message text.

    Driver messages can carry logins (pyodbc "Login failed for user ...") or
    bound row data such as addresses, so keep only the type and error codes.
    """
    if isinstance(exc, sf_errors.Error):
        return f"{type(exc).__name__} (errno {exc.errno}, sqlstate {exc.sqlstate})"
    if isinstance(exc, pyodbc.Error) and len(exc.args) > 1 \
            and re.fullmatch(r"[0-9A-Z]{5}", str(exc.args[0])):
        return f"{type(exc).__name__} (SQLSTATE {exc.args[0]})"
    return type(exc).__name__


# ============================================
# Configuration Validation
# ============================================

def validate_config() -> None:
    """
    Validate that all required environment variables are set.

    This function should be called at startup to ensure the
    application is properly configured before attempting to
    connect to databases or APIs.

    Raises:
        EnvironmentError: If any required variables are missing
    """
    required_vars = [
        'SNOWFLAKE_USER',
        'SNOWFLAKE_PRIVATE_KEY_B64',
        'GEOCODIO_API_KEY',
        'IC_DB_USER',
        'IC_DB_PASSWORD',
    ]

    missing = [var for var in required_vars if not os.getenv(var)]

    if missing:
        error_msg = f"Missing required environment variables: {', '.join(missing)}"
        logger.error(error_msg)
        raise EnvironmentError(error_msg)

    logger.info(f"Configuration validated (Geocodio API version: {GEOCODIO_API_VERSION})")


# ============================================
# Authentication Helpers
# ============================================

def load_snowflake_private_key() -> bytes:
    """
    Load and decode the Snowflake RSA private key from environment variable.

    The private key is expected as a base64-encoded PEM string in
    SNOWFLAKE_PRIVATE_KEY_B64. An optional passphrase can be provided
    in SNOWFLAKE_PRIVATE_KEY_PASSPHRASE if the key is encrypted.

    Returns:
        bytes: DER-encoded PKCS8 private key bytes (the format Snowflake expects)

    Raises:
        EnvironmentError: If the key environment variable is not set
        ValueError: If the key cannot be decoded or parsed
    """
    if not SNOWFLAKE_PRIVATE_KEY_B64:
        raise EnvironmentError("SNOWFLAKE_PRIVATE_KEY_B64 is not set")

    try:
        pem_bytes = base64.b64decode(SNOWFLAKE_PRIVATE_KEY_B64)
    except Exception as e:
        raise ValueError(f"Failed to base64-decode SNOWFLAKE_PRIVATE_KEY_B64: {e}")

    passphrase = (
        SNOWFLAKE_PRIVATE_KEY_PASSPHRASE.encode()
        if SNOWFLAKE_PRIVATE_KEY_PASSPHRASE
        else None
    )

    try:
        private_key = serialization.load_pem_private_key(pem_bytes, password=passphrase)
    except Exception as e:
        raise ValueError(f"Failed to load PEM private key: {e}")

    private_key_der = private_key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    return private_key_der


# ============================================
# Database Connection Functions
# ============================================

def create_snowflake_connection(database: str = SNOWFLAKE_DATABASE,
                                schema: str | None = None) -> SnowflakeConnection:
    """
    Create Snowflake connection with retry logic.

    Containers may start before network is fully ready,
    so we retry connection attempts with exponential backoff.

    Args:
        database: Snowflake database to connect to (default: SNOWFLAKE_DATABASE).
            Pass "LOGS" to open a connection scoped for app_logger dual-write.
        schema: Optional schema. If omitted, defaults to SNOWFLAKE_SCHEMA only when
            the database matches the configured warehouse database; otherwise the
            connection has no default schema (caller uses fully-qualified names).

    Returns:
        SnowflakeConnection: Active Snowflake connection

    Raises:
        Exception: If connection fails after all retries
    """
    # Decode private key before retry loop (deterministic — no point retrying)
    private_key_der = load_snowflake_private_key()

    effective_schema = schema if schema is not None else (
        SNOWFLAKE_SCHEMA if database == SNOWFLAKE_DATABASE else None
    )

    max_retries = 3
    retry_delay = 5  # seconds

    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"Connecting to Snowflake [{database}] (attempt {attempt}/{max_retries})...")

            connect_kwargs = dict(
                user=SNOWFLAKE_USER,
                private_key=private_key_der,
                account=SNOWFLAKE_ACCOUNT,
                warehouse=SNOWFLAKE_WAREHOUSE,
                database=database,
            )
            if effective_schema:
                connect_kwargs["schema"] = effective_schema

            conn = snowflake.connector.connect(**connect_kwargs)

            logger.info(f"Successfully connected to Snowflake [{database}]")
            return conn

        except sf_errors.ProgrammingError as e:
            logger.warning(f"Connection attempt {attempt} failed: {_describe_db_error(e)}")

            if attempt == max_retries:
                logger.error("Failed to connect to Snowflake after all retries")
                logger.error("Please check credentials and network connectivity")
                # from None: the chained driver error's text may name the user.
                raise ConnectionError(
                    f"Snowflake connection failed: {_describe_db_error(e)}"
                ) from None

            logger.info(f"Retrying in {retry_delay} seconds...")
            time.sleep(retry_delay)
            retry_delay *= 2  # Exponential backoff

        except Exception as e:
            logger.error(f"Unexpected error connecting to Snowflake: {_describe_db_error(e)}")
            raise ConnectionError(
                f"Snowflake connection failed: {_describe_db_error(e)}"
            ) from None


def create_ic_connection() -> pyodbc.Connection:
    """
    Create connection to Infinite Campus SQL Server database.

    Uses ODBC Driver 18 for SQL Server with TrustServerCertificate enabled.

    Returns:
        pyodbc.Connection: Active IC database connection

    Raises:
        pyodbc.Error: If connection fails
    """
    max_retries = 3
    retry_delay = 5  # seconds

    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"Connecting to Infinite Campus database (attempt {attempt}/{max_retries})...")

            connection_string = (
                f"DRIVER={{{IC_DB_DRIVER}}};"
                f"Server={IC_DB_SERVER};"
                f"Database={IC_DB_DATABASE};"
                f"UID={IC_DB_USER};"
                f"PWD={IC_DB_PASSWORD};"
                "TrustServerCertificate=yes;"
            )

            conn = pyodbc.connect(connection_string)

            logger.info("Successfully connected to Infinite Campus database")
            return conn

        except pyodbc.Error as e:
            logger.warning(f"IC connection attempt {attempt} failed: {_describe_db_error(e)}")

            if attempt == max_retries:
                logger.error("Failed to connect to Infinite Campus after all retries")
                logger.error("Please check credentials and network connectivity")
                # from None: pyodbc's "Login failed for user '...'" must not chain in.
                raise ConnectionError(
                    f"Infinite Campus connection failed: {_describe_db_error(e)}"
                ) from None

            logger.info(f"Retrying in {retry_delay} seconds...")
            time.sleep(retry_delay)
            retry_delay *= 2  # Exponential backoff

        except Exception as e:
            logger.error(f"Unexpected error connecting to Infinite Campus: {_describe_db_error(e)}")
            raise ConnectionError(
                f"Infinite Campus connection failed: {_describe_db_error(e)}"
            ) from None


# ============================================
# Data Fetching Functions
# ============================================

def fetch_addresses_to_geocode(snowflake_conn: SnowflakeConnection,
                               limit: int = BATCH_SIZE) -> pd.DataFrame:
    """
    Fetch addresses that need geocoding from Snowflake.

    Queries the VIEW_ADDRESSESNEEDINGGEOCODED view to get addresses
    that haven't been geocoded yet or need to be re-geocoded.

    Args:
        snowflake_conn: Active Snowflake connection
        limit: Maximum number of addresses to fetch (default from BATCH_SIZE)

    Returns:
        pd.DataFrame: DataFrame containing addresses with columns:
            - ADDRESSID: Unique address identifier
            - ADDRESSLINE1: Full address string
            - Additional address fields from the view

    Raises:
        Exception: If query fails
    """
    if not isinstance(limit, int) or limit <= 0:
        raise ValueError(f"limit must be a positive integer, got {limit!r}")

    try:
        logger.info(f"Fetching up to {limit} addresses from Snowflake...")

        cursor = snowflake_conn.cursor()

        query = QUERY_ADDRESSES.format(limit=limit)

        # Execute query and fetch results as DataFrame
        cursor.execute(query)
        df = cursor.fetch_pandas_all()

        cursor.close()

        logger.info(f"Successfully fetched {len(df)} addresses")

        if len(df) == 0:
            logger.warning("No addresses found that need geocoding")

        return df

    except sf_errors.ProgrammingError as e:
        logger.error(f"Snowflake query error: {_describe_db_error(e)}")
        logger.error("Please check the view name and permissions")
        # from None: runtime conversion errors (e.g. errno 100038) echo row values.
        raise RuntimeError(f"Snowflake address fetch failed: {_describe_db_error(e)}") from None

    except Exception as e:
        logger.error(f"Error fetching addresses from Snowflake: {_describe_db_error(e)}")
        raise RuntimeError(f"Snowflake address fetch failed: {_describe_db_error(e)}") from None


def fetch_district_lookup(snowflake_conn: SnowflakeConnection) -> pd.DataFrame:
    """
    Fetch district lookup table from Snowflake.

    This table maps NCES district IDs (LEA codes from Geocodio)
    to Infinite Campus district IDs.

    Args:
        snowflake_conn: Active Snowflake connection

    Returns:
        pd.DataFrame: DataFrame with DISTRICTID and NCESDISTRICTID columns,
                     indexed by NCESDISTRICTID for fast lookups

    Raises:
        Exception: If query fails
    """
    try:
        logger.info("Fetching district lookup table from Snowflake...")

        cursor = snowflake_conn.cursor()
        cursor.execute(QUERY_DISTRICTS)
        df = cursor.fetch_pandas_all()
        cursor.close()

        # Convert NCESDISTRICTID to numeric and set as index
        df['NCESDISTRICTID'] = pd.to_numeric(df['NCESDISTRICTID'], errors='coerce').astype('Int64')
        df.set_index('NCESDISTRICTID', inplace=True)

        logger.info(f"Successfully fetched {len(df)} district mappings")

        return df

    except Exception as e:
        logger.error(f"Error fetching district lookup: {_describe_db_error(e)}")
        raise RuntimeError(f"Snowflake district fetch failed: {_describe_db_error(e)}") from None


# ============================================
# Geocoding Functions
# ============================================

class GeocodioFatalError(RuntimeError):
    """Geocodio failed in a way that dooms the rest of the batch.

    The message carries only an HTTP status or exception type -- never the
    request URL, whose q= parameter is a student address.

    ``results`` has one entry per input address (None where not geocoded) so
    the caller can still save lookups that succeeded, and were paid for,
    before the abort.
    """

    def __init__(self, message: str, results: list | None = None):
        super().__init__(message)
        self.results = results if results is not None else []


def create_geocodio_session(api_key: str) -> requests.Session:
    """
    Create a requests session that authenticates to Geocodio by header.

    Sending the key as ``Authorization: Bearer`` rather than the ``api_key``
    query parameter keeps it out of request URLs, which requests embeds in
    exception messages.

    Also raises urllib3's log level to ERROR: its WARNINGs (e.g. "Failed to
    parse headers (url=...)") include the full query string, and app_logger
    only lowers urllib3 to WARNING. urllib3 logs nothing at ERROR or above.
    """
    for name in ("urllib3", "urllib3.connectionpool"):
        logging.getLogger(name).setLevel(logging.ERROR)
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {api_key}"
    return session


def geocode_address(session: requests.Session, address: str, fields: list) -> dict:
    """
    Call Geocodio API directly to geocode a single address.

    Args:
        session: Session from create_geocodio_session() (carries the API key)
        address: The address string to geocode
        fields: List of additional fields to request (e.g., ["school", "census2025"])

    Returns:
        dict: API response containing geocoding results

    Raises:
        requests.exceptions.RequestException: If API call fails
    """
    params = {
        "q": address,
        "fields": ",".join(fields)
    }
    response = session.get(GEOCODIO_API_URL, params=params, timeout=30)
    response.raise_for_status()
    payload = response.json()
    _check_deprecation(response, payload)
    return payload


def batch_geocode_addresses(addresses_df: pd.DataFrame, api_key: str) -> list:
    """
    Geocode addresses individually using single API calls.

    Processes each address one at a time with individual API calls.
    This approach works with API keys that don't support batch operations.

    Args:
        addresses_df: DataFrame with ADDRESSID and ADDRESSLINE1 columns
        api_key: Geocod.io API key

    Returns:
        list: List of API responses (dicts), one per address (in same order as input)

    Raises:
        GeocodioFatalError: On 401/403/429 (bad key, quota, rate limit), after
            MAX_CONSECUTIVE_GEOCODE_FAILURES non-422 failures in a row, or when
            no lookup succeeded and at least one failed for a non-422 reason.
            Its ``results`` holds the lookups completed before the abort.
    """
    logger.info(f"Geocoding {len(addresses_df)} addresses individually...")

    results = []
    success_count = 0
    error_count = 0
    consecutive_failures = 0
    last_failure = None  # most recent non-422 failure

    def _abort(message: str) -> GeocodioFatalError:
        # Pad to one entry per address so earlier lookups can still be saved.
        padded = results + [None] * (len(addresses_df) - len(results))
        return GeocodioFatalError(message, results=padded)

    with create_geocodio_session(api_key) as session:
        for idx, row in addresses_df.iterrows():
            address_id = row['ADDRESSID']
            address = row['ADDRESSLINE1']  # outside try: a schema error isn't a Geocodio failure
            status = None

            # Keep only the status code or type name from exceptions: requests
            # embeds the full URL, including the q= address, in its messages.
            # GeocodioFatalError is raised after the except blocks so the
            # original exception is never chained onto it.
            try:
                response = geocode_address(session, address, API_FIELDS)
            except requests.exceptions.HTTPError as e:
                status = e.response.status_code
                failure = f"HTTP {status}"
            except Exception as e:
                failure = type(e).__name__
            else:
                results.append(response)
                success_count += 1
                consecutive_failures = 0
                if (idx + 1) % 50 == 0:
                    logger.info(f"Geocoding progress: {success_count} succeeded, {error_count} failed of {idx + 1} processed")
                continue

            if status in GEOCODIO_FATAL_STATUSES:
                logger.error(f"Geocodio rejected the request ({failure}): invalid key, quota exhausted, or rate limited")
                raise _abort(f"Geocodio rejected the request ({failure})")

            results.append(None)
            error_count += 1

            if status == 422:
                # This one address is unparseable. Neutral for the breaker: it
                # neither proves Geocodio healthy nor counts as an outage.
                logger.warning(f"Invalid geocoding request for address ID {address_id}")
                continue

            logger.warning(f"Geocoding failed for address ID {address_id}: {failure}")
            last_failure = failure
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_GEOCODE_FAILURES:
                logger.error(f"Aborting geocoding after {consecutive_failures} consecutive failures")
                raise _abort(
                    f"{consecutive_failures} consecutive Geocodio failures (last: {failure})"
                )

    # Small batches can't reach the streak limit; zero successes with any
    # non-422 failure still means Geocodio was unusable this run.
    if success_count == 0 and last_failure is not None:
        logger.error("No successful Geocodio lookups this run")
        raise _abort(f"No successful Geocodio lookups ({error_count} failed, last: {last_failure})")

    logger.info(f"Geocoding complete: {success_count} successful, {error_count} failed")

    return results


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


def parse_geocode_results(api_results: list, addresses_df: pd.DataFrame) -> pd.DataFrame:
    """
    Parse Geocodio API results into structured DataFrame.

    Extracts all needed fields: lat, lng, accuracy, census data,
    school district information, etc.

    Args:
        api_results: List of individual API responses (one per address)
        addresses_df: Original DataFrame with ADDRESSID for matching

    Returns:
        pd.DataFrame: Parsed results with columns:
            - ADDRESSID: Original address ID
            - ADDRESSLINE1: Original address string
            - success: Boolean indicating if geocoding succeeded
            - latitude, longitude: Coordinates
            - accuracy: Accuracy score (0.0 to 1.0)
            - accuracy_type: Type of match (rooftop, point, etc.)
            - source: Data source used
            - block_code, tract_code: Census codes
            - county_fips: County FIPS code
            - lea_code: School district LEA code
            - district_name: School district name

    Raises:
        Exception: If parsing fails
    """
    try:
        logger.info(f"Parsing geocoding results for {len(addresses_df)} addresses...")

        parsed_rows = []

        # Process each individual API response
        for idx, address_result in enumerate(api_results):

            # Get original address info
            original_addressid = addresses_df.iloc[idx]['ADDRESSID']
            original_address = addresses_df.iloc[idx]['ADDRESSLINE1']

            # Check if this address had an error (result is None)
            if address_result is None:
                logger.warning(f"No results for address ID {original_addressid}")
                parsed_rows.append(_failed_row(original_addressid, original_address, None))
                continue

            # Check if API returned no results for this address
            results_list = address_result.get('results', [])
            if len(results_list) == 0:
                logger.warning(f"No geocoding results for address ID {original_addressid}")
                parsed_rows.append(_failed_row(original_addressid, original_address, address_result))
                continue

            # Get the first (best) result
            result = results_list[0]

            # Extract basic location data — guard against missing/null location.
            location = result.get('location') or {}
            latitude = location.get('lat')
            longitude = location.get('lng')
            if latitude is None or longitude is None:
                logger.warning(f"Missing location for address ID {original_addressid}")
                parsed_rows.append(_failed_row(original_addressid, original_address, address_result))
                continue
            accuracy = result.get('accuracy', 0.0)
            accuracy_type = result.get('accuracy_type')
            source = result.get('source')

            fields = result.get('fields', {})

            # Note: fields might be empty if not requested or not available with API key
            block_code = None
            tract_code = None
            county_fips = None
            lea_code = '0000'
            district_name = 'none'

            if fields:
                # Extract census data (2025)
                # API returns nested structure: fields.census.2025
                census_data = fields.get('census', {})
                if census_data and '2025' in census_data:
                    census_2025 = census_data['2025']
                    block_code = census_2025.get('block_code')
                    tract_code = census_2025.get('tract_code')
                    county_fips = census_2025.get('county_fips')

                # Extract school district data
                school_districts = fields.get('school_districts', {})
                if school_districts:
                    unified = school_districts.get('unified')
                    if unified:
                        lea_code = unified.get('lea_code', '0000')
                        district_name = unified.get('name', 'none')

            # Add parsed result
            parsed_rows.append({
                'ADDRESSID': original_addressid,
                'ADDRESSLINE1': original_address,
                'success': True,
                'latitude': latitude,
                'longitude': longitude,
                'accuracy': accuracy,
                'accuracy_type': accuracy_type,
                'source': source,
                'block_code': block_code,
                'tract_code': tract_code,
                'county_fips': county_fips,
                'lea_code': lea_code,
                'district_name': district_name,
                'raw_response': address_result
            })

        # Convert to DataFrame
        results_df = pd.DataFrame(parsed_rows)

        # Log summary
        success_count = results_df['success'].sum()
        failed_count = len(results_df) - success_count
        logger.info(f"Parsing complete: {success_count} successful, {failed_count} failed")

        if failed_count > 0:
            logger.warning(f"{failed_count} addresses could not be geocoded")

        return results_df

    except Exception as e:
        logger.error(f"Error parsing geocoding results: {e}")
        raise


def enrich_with_district_ids(results_df: pd.DataFrame, districts_df: pd.DataFrame) -> pd.DataFrame:
    """
    Enrich geocoding results with Infinite Campus district IDs.

    Matches LEA codes from Geocodio to NCES district IDs,
    then looks up the corresponding IC district ID.

    Args:
        results_df: DataFrame with geocoding results (must have lea_code column)
        districts_df: District lookup table indexed by NCESDISTRICTID

    Returns:
        pd.DataFrame: Results with added DISTRICTID column
    """
    try:
        logger.info("Enriching results with district IDs...")

        # Initialize DISTRICTID column
        results_df['DISTRICTID'] = 0

        # Only process successful geocoding results
        successful_mask = results_df['success'] == True

        for idx in results_df[successful_mask].index:
            lea_code = results_df.at[idx, 'lea_code']

            # Convert LEA code to integer
            try:
                nces_id = int(lea_code)
            except (ValueError, TypeError):
                logger.warning(f"Invalid LEA code for address ID {results_df.at[idx, 'ADDRESSID']}: '{lea_code}'")
                continue

            # Look up district ID
            if nces_id in districts_df.index:
                district_id = districts_df.loc[nces_id, 'DISTRICTID']
                results_df.at[idx, 'DISTRICTID'] = int(district_id)
                logger.debug(f"Matched LEA {nces_id} to District ID {district_id}")
            else:
                logger.warning(
                    f"No district match for address ID {results_df.at[idx, 'ADDRESSID']}: "
                    f"LEA code {nces_id}"
                )
                results_df.at[idx, 'DISTRICTID'] = 0

        # Count matches
        matched_count = (results_df['DISTRICTID'] > 0).sum()
        logger.info(f"District ID enrichment complete: {matched_count} addresses matched to districts")

        return results_df

    except Exception as e:
        logger.error(f"Error enriching with district IDs: {e}")
        raise


# ============================================
# Snowflake Logging Functions
# ============================================

def log_results_to_snowflake(results_df: pd.DataFrame, snowflake_conn: SnowflakeConnection) -> Tuple[int, int]:
    """
    Log geocoding results to Snowflake for auditing and analysis.

    Inserts the raw JSON response from Geocod.io into the
    LOGGING_GEOCODIOAPI_ADDRESSLOOKUPS table for each address.

    Args:
        results_df: DataFrame with geocoding results (must have ADDRESSID and raw_response columns)
        snowflake_conn: Active Snowflake connection

    Returns:
        Tuple[int, int]: (success_count, error_count)

    Raises:
        Exception: If critical database error occurs
    """
    import json

    logger.info(f"Logging {len(results_df)} geocoding results to Snowflake...")

    cursor = snowflake_conn.cursor()
    success_count = 0
    error_count = 0

    try:
        for idx, row in results_df.iterrows():
            address_id = row['ADDRESSID']
            raw_response = row.get('raw_response')

            # Skip if no raw response (failed geocoding with no API response)
            if raw_response is None:
                logger.debug(f"Skipping Snowflake log for address ID {address_id}: no raw response")
                continue

            try:
                json_text = json.dumps(raw_response)

                insert_sql = """
                    INSERT INTO LOGGING_GEOCODIOAPI_ADDRESSLOOKUPS (IC_ADDRESSID, GEOCODIO_JSON)
                    SELECT %s, PARSE_JSON(%s)
                """

                cursor.execute(insert_sql, (address_id, json_text))
                success_count += 1

                if (success_count + error_count) % 100 == 0:
                    logger.info(f"Snowflake logging progress: {success_count + error_count}/{len(results_df)}")

            # Codes only: the connector inlines bound parameters into the SQL,
            # so error text can echo the raw response (address, coordinates).
            except sf_errors.ProgrammingError as e:
                logger.warning(f"Failed to log address ID {address_id} to Snowflake: {_describe_db_error(e)}")
                error_count += 1

            except Exception as e:
                logger.warning(f"Unexpected error logging address ID {address_id}: {_describe_db_error(e)}")
                error_count += 1

        # No-op under Snowflake's default AUTOCOMMIT; keeps the audit rows if
        # it is off. Done here so a failure stays inside this non-fatal step.
        snowflake_conn.commit()

        logger.info(f"Snowflake logging complete: {success_count} successful, {error_count} failed")
        return success_count, error_count

    finally:
        cursor.close()


# ============================================
# Infinite Campus Update Functions
# ============================================

def update_ic_addresses(results_df: pd.DataFrame, ic_conn: pyodbc.Connection,
                        accuracy_threshold: float = ACCURACY_THRESHOLD) -> Tuple[int, int]:
    """
    Update Infinite Campus ADDRESS table with geocoded data.

    Only updates addresses that meet the accuracy threshold.
    Updates latitude, longitude, tract, block, location_code (county FIPS),
    and optionally districtID if a match was found.

    Args:
        results_df: DataFrame with geocoding results
        ic_conn: Active Infinite Campus database connection
        accuracy_threshold: Minimum accuracy score required for update (default 0.8)

    Returns:
        Tuple[int, int]: (success_count, error_count)

    Raises:
        Exception: If critical database error occurs
    """
    logger.info(f"Updating Infinite Campus addresses (accuracy threshold: {accuracy_threshold})...")

    cursor = ic_conn.cursor()
    success_count = 0
    error_count = 0
    skipped_count = 0

    try:
        for idx, row in results_df.iterrows():
            address_id = row['ADDRESSID']

            # Skip unsuccessful geocoding results
            if not row.get('success', False):
                logger.debug(f"Skipping address ID {address_id}: geocoding failed")
                skipped_count += 1
                continue

            # Skip results below accuracy threshold
            accuracy = row.get('accuracy', 0.0)
            if accuracy < accuracy_threshold:
                logger.debug(f"Skipping address ID {address_id}: accuracy {accuracy} below threshold {accuracy_threshold}")
                skipped_count += 1
                continue

            # Extract values for update
            latitude = row.get('latitude')
            longitude = row.get('longitude')
            tract_code = row.get('tract_code')
            block_code = row.get('block_code')
            county_fips = row.get('county_fips')
            district_id = row.get('DISTRICTID', 0)

            try:
                # Use different SQL based on whether we have a valid district ID
                if district_id == 0 or pd.isna(district_id):
                    # Update without district ID
                    sql_update = """
                        UPDATE ADDRESS
                        SET latitude = ?, longitude = ?, tract = ?, block = ?, location_code = ?
                        WHERE addressid = ?
                    """
                    args = (latitude, longitude, tract_code, block_code, county_fips, address_id)
                else:
                    # Update with district ID
                    sql_update = """
                        UPDATE ADDRESS
                        SET latitude = ?, longitude = ?, tract = ?, block = ?, location_code = ?, districtID = ?
                        WHERE addressid = ?
                    """
                    args = (latitude, longitude, tract_code, block_code, county_fips, int(district_id), address_id)

                cursor.execute(sql_update, args)
                cursor.commit()
                success_count += 1

                if success_count % 50 == 0:
                    logger.info(f"IC update progress: {success_count} addresses updated...")

            except pyodbc.Error as e:
                logger.warning(f"Failed to update address ID {address_id} in IC: {_describe_db_error(e)}")
                error_count += 1

            except Exception as e:
                logger.warning(f"Unexpected error updating address ID {address_id}: {_describe_db_error(e)}")
                error_count += 1

        logger.info(f"IC update complete: {success_count} updated, {error_count} errors, {skipped_count} skipped")
        return success_count, error_count

    finally:
        cursor.close()


# ============================================
# Main Execution
# ============================================

def _close_quietly(conn, name: str) -> None:
    """Close a connection at teardown; failing to close must not fail a finished run."""
    try:
        conn.close()
    except Exception as e:
        logger.warning(f"Error closing {name} connection: {_describe_db_error(e)}")


def run_pipeline(log: AppLogger) -> None:
    """
    Run the geocoding steps under AppLogger step tracking.

    Exceptions from fatal steps propagate to main(), which records the run as
    FAILED. log.step() has already logged them (sanitized) by then.
    """
    with log.step("validate_config") as s:
        validate_config()
        if not GEOCODIO_API_KEY:
            raise EnvironmentError("GEOCODIO_API_KEY is not set")

    with log.step("connect_snowflake"):
        sf_conn = create_snowflake_connection()

    try:
        with log.step("fetch_addresses") as s:
            addresses_df = fetch_addresses_to_geocode(sf_conn, BATCH_SIZE)
            s.rows = len(addresses_df)
            if len(addresses_df) == 0:
                s.status = "SKIPPED"

        if len(addresses_df) == 0:
            log.info("No addresses need geocoding")
            log.finish(override_status="SKIPPED")
            return

        with log.step("fetch_districts") as s:
            districts_df = fetch_district_lookup(sf_conn)
            s.rows = len(districts_df)

        geocode_error = None
        with log.step("geocode_addresses") as s:
            try:
                api_results = batch_geocode_addresses(addresses_df, GEOCODIO_API_KEY)
            except GeocodioFatalError as exc:
                if len(exc.results) != len(addresses_df):
                    raise
                # Save the lookups completed (and paid for) before the abort;
                # the run is failed once they are written.
                geocode_error = exc
                api_results = exc.results
                s.status = "FAILED"
                s.error_type = type(exc).__name__
                s.error_summary = str(exc)
            results_df = parse_geocode_results(api_results, addresses_df)
            success_count = int(results_df['success'].sum())
            failed_count = int((~results_df['success']).sum())
            s.rows = success_count
            s.rows_failed = failed_count
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

        with log.step("enrich_districts") as s:
            results_df = enrich_with_district_ids(results_df, districts_df)
            matched = int((results_df['DISTRICTID'] > 0).sum())
            s.rows = matched
            s.metadata["district_match_rate"] = round(
                matched / len(results_df), 3
            ) if len(results_df) > 0 else 0.0

        with log.step("log_to_snowflake", fatal=False) as s:
            sf_success, sf_errors_count = log_results_to_snowflake(results_df, sf_conn)
            s.rows = sf_success
            s.rows_failed = sf_errors_count

        with log.step("update_ic") as s:
            ic_conn = create_ic_connection()
            try:
                ic_success, ic_errors_count = update_ic_addresses(results_df, ic_conn)
                s.rows = ic_success
                s.rows_failed = ic_errors_count
                s.metadata["skipped_below_threshold"] = (
                    len(results_df) - ic_success - ic_errors_count
                )
            finally:
                _close_quietly(ic_conn, "Infinite Campus")

        if geocode_error is not None:
            raise geocode_error

    finally:
        _close_quietly(sf_conn, "Snowflake")


def main() -> None:
    """
    Main execution function.

    Owns the AppLogger run lifecycle and the process exit code; the steps
    themselves live in run_pipeline().
    """
    # Open a dedicated Snowflake connection for app_logger dual-write.
    # Best-effort: if Snowflake is unreachable, log locally and continue without
    # remote logging. The local NDJSON files remain the durable record.
    sf_log_conn = None
    try:
        sf_log_conn = create_snowflake_connection(database="LOGS")
    except Exception as exc:
        # No AppLogger yet. Use the module logger, not logging.warning(): with
        # no root handlers that would basicConfig() a raw stderr handler that
        # outlives this call and bypasses app_logger's redaction.
        logger.warning("Could not open Snowflake connection for app_logger: %s",
                       _describe_db_error(exc))

    log = AppLogger(
        "Address_Geocoding",
        job="daily_geocode",
        configure_root_logger=True,
        purge_on_init=True,
        snowflake_connection=sf_log_conn,
        notify=True,
    )
    log.start(trigger="cron", schedule="0 17 * * *")

    # app_logger only reports FAILED when *every* step failed, so a fatal
    # failure after validate_config would otherwise be recorded as PARTIAL
    # and exit 0. Force FAILED whenever a fatal error ends the run.
    failed = False
    try:
        run_pipeline(log)
    except Exception as exc:
        failed = True
        # Codes only: exception text can carry addresses or credentials.
        logger.error("Run aborted by %s", _describe_db_error(exc))
    except BaseException as exc:
        # SIGTERM (SystemExit 143) or Ctrl-C: record FAILED, keep the exit code.
        # Suppress the chained context: Ctrl-C during a retry sleep or inside
        # requests would otherwise print the driver error / URL to stderr.
        failed = True
        exc.__suppress_context__ = True
        raise
    finally:
        status = log.finish(override_status="FAILED" if failed else None)
        if sf_log_conn is not None:
            try:
                sf_log_conn.close()
            except Exception:
                pass

    sys.exit(ExitCode.SUCCESS if status in ("SUCCESS", "PARTIAL", "SKIPPED") else ExitCode.FAILED)


if __name__ == "__main__":
    main()
