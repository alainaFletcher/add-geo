#!/usr/bin/env python3
"""
Address Geocoding Application

This application geocodes addresses from Infinite Campus
using the Geocodio API. It looks up addresses individually, enriches them with
census and school district data, and updates both Infinite Campus and Snowflake
databases.
"""

import os
import sys
import logging
import time
from typing import Optional, Tuple

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
from dotenv import load_dotenv

# Geocodio API configuration
GEOCODIO_API_URL = "https://api.geocod.io/v1.9/geocode"

# Load environment variables from .env file
load_dotenv()


# ============================================
# Configuration from Environment Variables
# ============================================

# Snowflake Data Warehouse
SNOWFLAKE_USER = os.getenv('SNOWFLAKE_USER')
SNOWFLAKE_PASSWORD = os.getenv('SNOWFLAKE_PASSWORD')
SNOWFLAKE_ACCOUNT = os.getenv('SNOWFLAKE_ACCOUNT', 'dv56305.west-us-2.azure')
SNOWFLAKE_WAREHOUSE = os.getenv('SNOWFLAKE_WAREHOUSE', 'dataloadingonly')
SNOWFLAKE_DATABASE = os.getenv('SNOWFLAKE_DATABASE', 'MPS_Warehouse')
SNOWFLAKE_SCHEMA = os.getenv('SNOWFLAKE_SCHEMA', 'datalake')

# Geocodio API
GEOCODIO_API_KEY = os.getenv('GEOCODIO_API_KEY')

# Infinite Campus Database (SQL Server)
IC_DB_SERVER = os.getenv('IC_DB_SERVER', 'icampus.mapleton.us,7771')
IC_DB_DATABASE = os.getenv('IC_DB_DATABASE', 'mapleton')
IC_DB_USER = os.getenv('IC_DB_USER')
IC_DB_PASSWORD = os.getenv('IC_DB_PASSWORD')
IC_DB_DRIVER = os.getenv('IC_DB_DRIVER', 'ODBC Driver 18 for SQL Server')

# Application Settings
BATCH_SIZE = int(os.getenv('BATCH_SIZE', '500'))
ACCURACY_THRESHOLD = float(os.getenv('ACCURACY_THRESHOLD', '0.8'))
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
API_FIELDS = ["school", "zip4", "census2024"]

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


# ============================================
# Logging Configuration
# ============================================

def setup_logging() -> logging.Logger:
    """
    Configure logging for both console (Docker) and file output.

    Docker best practice: log to stdout for 'docker logs' command.
    Also write to file for persistent debugging.

    Returns:
        logging.Logger: Configured logger instance
    """
    log_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    log_level = getattr(logging, LOG_LEVEL.upper(), logging.INFO)

    # Create logger
    logger = logging.getLogger('geocoding')
    logger.setLevel(log_level)

    # Console handler (for Docker logs and terminal output)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter(log_format))
    logger.addHandler(console_handler)

    # File handler (for persistent logs)
    # Check if logs directory exists, create if in Docker environment
    log_dir = '/app/logs' if os.path.exists('/app') else './logs'
    os.makedirs(log_dir, exist_ok=True)

    file_handler = logging.FileHandler(f'{log_dir}/geocoding.log')
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(log_format))
    logger.addHandler(file_handler)

    return logger


# Initialize logger
logger = setup_logging()


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
        'SNOWFLAKE_PASSWORD',
        'GEOCODIO_API_KEY',
        'IC_DB_USER',
        'IC_DB_PASSWORD',
    ]

    missing = [var for var in required_vars if not os.getenv(var)]

    if missing:
        error_msg = f"Missing required environment variables: {', '.join(missing)}"
        logger.error(error_msg)
        logger.error("Please check your .env file or environment configuration")
        raise EnvironmentError(error_msg)

    logger.info("Configuration validated successfully")
    logger.info(f"Batch size: {BATCH_SIZE}")
    logger.info(f"Accuracy threshold: {ACCURACY_THRESHOLD}")
    logger.info(f"Snowflake database: {SNOWFLAKE_DATABASE}.{SNOWFLAKE_SCHEMA}")
    logger.info(f"IC database: {IC_DB_SERVER}/{IC_DB_DATABASE}")


# ============================================
# Database Connection Functions
# ============================================

def create_snowflake_connection() -> SnowflakeConnection:
    """
    Create Snowflake connection with retry logic.

    Containers may start before network is fully ready,
    so we retry connection attempts with exponential backoff.

    Returns:
        SnowflakeConnection: Active Snowflake connection

    Raises:
        Exception: If connection fails after all retries
    """
    max_retries = 3
    retry_delay = 5  # seconds

    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"Connecting to Snowflake (attempt {attempt}/{max_retries})...")

            conn = snowflake.connector.connect(
                user=SNOWFLAKE_USER,
                password=SNOWFLAKE_PASSWORD,
                account=SNOWFLAKE_ACCOUNT,
                warehouse=SNOWFLAKE_WAREHOUSE,
                database=SNOWFLAKE_DATABASE,
                schema=SNOWFLAKE_SCHEMA
            )

            logger.info("Successfully connected to Snowflake")
            return conn

        except sf_errors.ProgrammingError as e:
            logger.warning(f"Connection attempt {attempt} failed: {e}")

            if attempt == max_retries:
                logger.error("Failed to connect to Snowflake after all retries")
                logger.error("Please check credentials and network connectivity")
                raise

            logger.info(f"Retrying in {retry_delay} seconds...")
            time.sleep(retry_delay)
            retry_delay *= 2  # Exponential backoff

        except Exception as e:
            logger.error(f"Unexpected error connecting to Snowflake: {e}")
            raise


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
            logger.warning(f"IC connection attempt {attempt} failed: {e}")

            if attempt == max_retries:
                logger.error("Failed to connect to Infinite Campus after all retries")
                logger.error("Please check credentials and network connectivity")
                raise

            logger.info(f"Retrying in {retry_delay} seconds...")
            time.sleep(retry_delay)
            retry_delay *= 2  # Exponential backoff

        except Exception as e:
            logger.error(f"Unexpected error connecting to Infinite Campus: {e}")
            raise


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
    try:
        logger.info(f"Fetching up to {limit} addresses from Snowflake...")

        cursor = snowflake_conn.cursor()

        # Format query with limit
        query = QUERY_ADDRESSES.format(limit=limit)

        # Execute query and fetch results as DataFrame
        cursor.execute(query)
        df = cursor.fetch_pandas_all()

        cursor.close()

        logger.info(f"Successfully fetched {len(df)} addresses")

        if len(df) == 0:
            logger.warning("No addresses found that need geocoding")
        else:
            logger.debug(f"Address columns: {df.columns.tolist()}")
            logger.debug(f"First address: {df.iloc[0]['ADDRESSLINE1'] if len(df) > 0 else 'N/A'}")

        return df

    except sf_errors.ProgrammingError as e:
        logger.error(f"Snowflake query error: {e}")
        logger.error("Please check the view name and permissions")
        raise

    except Exception as e:
        logger.error(f"Error fetching addresses from Snowflake: {e}")
        raise


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
        logger.error(f"Error fetching district lookup: {e}")
        raise


# ============================================
# Geocoding Functions
# ============================================

def geocode_address(address: str, api_key: str, fields: list) -> dict:
    """
    Call Geocod.io API directly to geocode a single address.

    Args:
        address: The address string to geocode
        api_key: Geocod.io API key
        fields: List of additional fields to request (e.g., ["school", "census2024"])

    Returns:
        dict: API response containing geocoding results

    Raises:
        requests.exceptions.RequestException: If API call fails
    """
    params = {
        "q": address,
        "api_key": api_key,
        "fields": ",".join(fields)
    }
    response = requests.get(GEOCODIO_API_URL, params=params)
    response.raise_for_status()
    return response.json()


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
        requests.exceptions.HTTPError: If API returns 401 (authentication error)
    """
    logger.info(f"Geocoding {len(addresses_df)} addresses individually...")
    logger.debug(f"API fields requested: {API_FIELDS}")

    results = []
    success_count = 0
    error_count = 0

    for idx, row in addresses_df.iterrows():
        address = row['ADDRESSLINE1']
        address_id = row['ADDRESSID']

        try:
            # Single API call per address
            logger.debug(f"API Fields: {API_FIELDS}")
            response = geocode_address(address, api_key, API_FIELDS)
            results.append(response)
            success_count += 1

            if (idx + 1) % 50 == 0:
                logger.info(f"Progress: {idx + 1}/{len(addresses_df)} addresses geocoded...")

        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 401:
                logger.error(f"Geocodio API authentication failed: {e}")
                logger.error("Action required: Check GEOCODIO_API_KEY in .env file")
                raise
            elif e.response.status_code == 422:
                logger.error(f"Invalid geocoding request for address ID {address_id}: {e}")
                logger.error(f"Address: {address}")
                results.append(None)
                error_count += 1
            else:
                logger.warning(f"HTTP error geocoding address ID {address_id}: {e}")
                logger.warning(f"Address: {address}")
                results.append(None)
                error_count += 1

        except requests.exceptions.RequestException as e:
            logger.warning(f"Request error geocoding address ID {address_id}: {e}")
            logger.warning(f"Address: {address}")
            results.append(None)
            error_count += 1

        except Exception as e:
            logger.warning(f"Error geocoding address ID {address_id}: {e}")
            logger.warning(f"Address: {address}")
            results.append(None)
            error_count += 1

    logger.info(f"Geocoding complete: {success_count} successful, {error_count} failed")

    return results


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
                logger.warning(f"No results for address ID {original_addressid}: {original_address}")
                parsed_rows.append({
                    'ADDRESSID': original_addressid,
                    'ADDRESSLINE1': original_address,
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
                    'raw_response': None
                })
                continue

            # Check if API returned no results for this address
            results_list = address_result.get('results', [])
            if len(results_list) == 0:
                logger.warning(f"No geocoding results for address ID {original_addressid}: {original_address}")
                parsed_rows.append({
                    'ADDRESSID': original_addressid,
                    'ADDRESSLINE1': original_address,
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
                    'raw_response': address_result
                })
                continue

            # Get the first (best) result
            result = results_list[0]

            # Extract basic location data
            latitude = result['location']['lat']
            longitude = result['location']['lng']
            accuracy = result.get('accuracy', 0.0)
            accuracy_type = result.get('accuracy_type')
            source = result.get('source')

            # DEBUG: Show what fields are available (for first few addresses)
            fields = result.get('fields', {})
            if idx < 3:  # Only show for first 3 addresses to avoid spam
                logger.info(f"DEBUG - Address ID {original_addressid}: {original_address}")
                logger.info(f"DEBUG - Geocoded to: {latitude}, {longitude} (accuracy: {accuracy})")
                logger.info(f"DEBUG - Fields available: {list(fields.keys()) if fields else 'None'}")
                if fields:
                    logger.info(f"DEBUG - School districts: {fields.get('school_districts')}")
                    logger.info(f"DEBUG - Census 2024: {fields.get('census', {}).get('2024')}")

            # Extract census data (2024) and school district data
            # Note: fields might be empty if not requested or not available with API key
            block_code = None
            tract_code = None
            county_fips = None
            lea_code = '0000'
            district_name = 'none'

            if fields:
                # Extract census data (2024)
                # API returns nested structure: fields.census.2024
                census_data = fields.get('census', {})
                if census_data and '2024' in census_data:
                    census_2024 = census_data['2024']
                    block_code = census_2024.get('block_code')
                    tract_code = census_2024.get('tract_code')
                    county_fips = census_2024.get('county_fips')

                # Extract school district data
                school_districts = fields.get('school_districts', {})
                if school_districts:
                    unified = school_districts.get('unified')
                    if unified:
                        lea_code = unified.get('lea_code', '0000')
                        district_name = unified.get('name', 'none')

                        # DEBUG: Show what LEA code we got
                        if idx < 3:
                            logger.info(f"DEBUG - Extracted LEA code: {lea_code}, District: {district_name}")

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

        # DEBUG: Show what's in the districts lookup table
        logger.info(f"DEBUG - Districts lookup table has {len(districts_df)} entries")
        logger.info(f"DEBUG - Sample NCES IDs in lookup: {list(districts_df.index[:5])}")

        # Initialize DISTRICTID column
        results_df['DISTRICTID'] = 0

        # Only process successful geocoding results
        successful_mask = results_df['success'] == True

        # DEBUG: Show unique LEA codes we're trying to match
        unique_leas = results_df[successful_mask]['lea_code'].unique()
        logger.info(f"DEBUG - Unique LEA codes to match: {unique_leas}")

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
                logger.info(f"✓ Matched LEA {nces_id} to District ID {district_id}")
            else:
                # District not found - log for review
                logger.warning(
                    f"✗ No district match for address ID {results_df.at[idx, 'ADDRESSID']}: "
                    f"LEA code {nces_id} ({results_df.at[idx, 'district_name']})"
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
                # Convert response to JSON string and escape single quotes
                json_text = json.dumps(raw_response)
                json_text = json_text.replace("'", "''")

                # Insert into Snowflake logging table
                insert_sql = f"""
                    INSERT INTO LOGGING_GEOCODIOAPI_ADDRESSLOOKUPS (IC_ADDRESSID, GEOCODIO_JSON)
                    SELECT {address_id}, PARSE_JSON('{json_text}')
                """

                cursor.execute(insert_sql)
                success_count += 1

                if (success_count + error_count) % 100 == 0:
                    logger.info(f"Snowflake logging progress: {success_count + error_count}/{len(results_df)}")

            except sf_errors.ProgrammingError as e:
                logger.warning(f"Failed to log address ID {address_id} to Snowflake: {e}")
                logger.debug(f"Error details - errno: {e.errno}, sqlstate: {e.sqlstate}, msg: {e.msg}")
                error_count += 1

            except Exception as e:
                logger.warning(f"Unexpected error logging address ID {address_id}: {e}")
                error_count += 1

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
                logger.warning(f"Failed to update address ID {address_id} in IC: {e}")
                logger.debug(f"SQL: {sql_update}")
                logger.debug(f"Args: {args}")
                error_count += 1

            except Exception as e:
                logger.warning(f"Unexpected error updating address ID {address_id}: {e}")
                error_count += 1

        logger.info(f"IC update complete: {success_count} updated, {error_count} errors, {skipped_count} skipped")
        return success_count, error_count

    finally:
        cursor.close()


# ============================================
# Main Execution
# ============================================

def main() -> int:
    """
    Main execution function.

    Orchestrates the entire geocoding process:
    1. Validate configuration
    2. Connect to databases
    3. Fetch addresses to geocode
    4. (Future) Geocode addresses
    5. (Future) Update databases

    Returns:
        int: Exit code (0 for success, non-zero for error)
    """
    start_time = time.time()

    try:
        logger.info("=" * 60)
        logger.info("Starting Address Geocoding Application v2.0")
        logger.info("=" * 60)

        # Step 1: Validate configuration
        logger.info("Step 1: Validating configuration...")
        validate_config()

        # Step 2: Validate Geocodio API key
        logger.info("Step 2: Validating Geocodio API key...")
        if not GEOCODIO_API_KEY:
            raise EnvironmentError("GEOCODIO_API_KEY is not set")
        logger.info("Geocodio API key validated")

        # Step 3: Connect to Snowflake
        logger.info("Step 3: Connecting to Snowflake...")
        with create_snowflake_connection() as sf_conn:

            # Step 4: Fetch addresses to geocode
            logger.info("Step 4: Fetching addresses to geocode...")
            addresses_df = fetch_addresses_to_geocode(sf_conn, BATCH_SIZE)

            if len(addresses_df) == 0:
                logger.info("No addresses to process. Exiting.")
                return 0

            # Step 5: Fetch district lookup table
            logger.info("Step 5: Fetching district lookup table...")
            districts_df = fetch_district_lookup(sf_conn)

            logger.info(f"Addresses fetched: {len(addresses_df)}")
            logger.info(f"Districts in lookup: {len(districts_df)}")

            # Step 6: Geocode all addresses (individual API calls)
            logger.info("Step 6: Geocoding addresses...")
            api_results = batch_geocode_addresses(addresses_df, GEOCODIO_API_KEY)

            # Step 7: Parse geocoding results
            logger.info("Step 7: Parsing geocoding results...")
            results_df = parse_geocode_results(api_results, addresses_df)

            # Step 8: Enrich with district IDs
            logger.info("Step 8: Enriching with district IDs...")
            results_df = enrich_with_district_ids(results_df, districts_df)

            # Log summary of results
            logger.info("=" * 60)
            logger.info("GEOCODING RESULTS SUMMARY")
            logger.info("=" * 60)
            logger.info(f"Total addresses processed:    {len(results_df)}")
            logger.info(f"Successfully geocoded:        {results_df['success'].sum()}")
            logger.info(f"Failed to geocode:            {(~results_df['success']).sum()}")
            logger.info(f"Matched to districts:         {(results_df['DISTRICTID'] > 0).sum()}")
            logger.info(f"Average accuracy:             {results_df[results_df['success']]['accuracy'].mean():.3f}")
            logger.info("=" * 60)

            # Step 9: Log results to Snowflake
            logger.info("Step 9: Logging results to Snowflake...")
            sf_success, sf_errors_count = log_results_to_snowflake(results_df, sf_conn)
            logger.info(f"Snowflake logging: {sf_success} logged, {sf_errors_count} errors")

            # Step 10: Connect to IC and update addresses
            logger.info("Step 10: Updating Infinite Campus database...")
            ic_conn = create_ic_connection()
            try:
                ic_success, ic_errors_count = update_ic_addresses(results_df, ic_conn)
                logger.info(f"IC update: {ic_success} updated, {ic_errors_count} errors")
            finally:
                ic_conn.close()
                logger.info("Infinite Campus connection closed")

        # Calculate duration
        duration = time.time() - start_time

        logger.info("=" * 60)
        logger.info(f"Application completed successfully in {duration:.1f} seconds")
        logger.info("=" * 60)

        return 0

    except EnvironmentError as e:
        logger.error(f"Configuration error: {e}")
        return 1

    except requests.exceptions.HTTPError as e:
        if hasattr(e, 'response') and e.response.status_code == 401:
            logger.error(f"Geocodio API authentication error: {e}")
            return 2
        logger.error(f"Geocodio API HTTP error: {e}")
        return 3

    except requests.exceptions.RequestException as e:
        logger.error(f"Geocodio API request error: {e}")
        return 4

    except sf_errors.ProgrammingError as e:
        logger.error(f"Snowflake database error: {e}")
        return 5

    except pyodbc.Error as e:
        logger.error(f"Infinite Campus database error: {e}")
        return 6

    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        return 7


if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)
