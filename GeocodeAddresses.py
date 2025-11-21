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

import pandas as pd
import snowflake.connector
from snowflake.connector import SnowflakeConnection
import snowflake.connector.errors as sf_errors
from dotenv import load_dotenv
from geocodio import Geocodio
from geocodio.exceptions import AuthenticationError, InvalidRequestError, GeocodioError

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

def batch_geocode_addresses(addresses_df: pd.DataFrame, client: Geocodio) -> list:
    """
    Geocode addresses individually using single API calls.

    Processes each address one at a time with individual API calls.
    This approach works with API keys that don't support batch operations.

    Args:
        addresses_df: DataFrame with ADDRESSID and ADDRESSLINE1 columns
        client: Geocodio client instance

    Returns:
        list: List of API responses, one per address (in same order as input)

    Raises:
        AuthenticationError: If API key is invalid
        InvalidRequestError: If request is malformed
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
            print(f"API Fields: {API_FIELDS}")
            response = client.geocode(address, fields=API_FIELDS)
            results.append(response)
            success_count += 1

            if (idx + 1) % 50 == 0:
                logger.info(f"Progress: {idx + 1}/{len(addresses_df)} addresses geocoded...")

        except AuthenticationError as e:
            logger.error(f"Geocodio API authentication failed: {e}")
            logger.error("Action required: Check GEOCODIO_API_KEY in .env file")
            raise

        except InvalidRequestError as e:
            logger.error(f"Invalid geocoding request for address ID {address_id}: {e}")
            logger.error(f"Address: {address}")
            # Add empty result for this address
            results.append(None)
            error_count += 1

        except Exception as e:
            logger.warning(f"Error geocoding address ID {address_id}: {e}")
            logger.warning(f"Address: {address}")
            # Add empty result for this address
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
                    'district_name': 'none'
                })
                continue

            # Check if API returned no results for this address
            if not hasattr(address_result, 'results') or len(address_result.results) == 0:
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
                    'district_name': 'none'
                })
                continue

            # Get the first (best) result
            result = address_result.results[0]

            # Extract basic location data
            latitude = result.location.lat
            longitude = result.location.lng
            accuracy = result.accuracy
            accuracy_type = result.accuracy_type
            source = result.source

            # DEBUG: Show what fields are available (for first few addresses)
            if idx < 3:  # Only show for first 3 addresses to avoid spam
                logger.info(f"DEBUG - Address ID {original_addressid}: {original_address}")
                logger.info(f"DEBUG - Geocoded to: {latitude}, {longitude} (accuracy: {accuracy})")
                logger.info(f"DEBUG - Fields available: {result.fields}")
                if result.fields is not None:
                    logger.info(f"DEBUG - School districts: {result.fields.school_districts}")
                    logger.info(f"DEBUG - Census 2023: {result.fields.census2023}")

            # Extract census data (2023) and school district data
            # Note: fields might be None if not requested or not available with API key
            block_code = None
            tract_code = None
            county_fips = None
            lea_code = '0000'
            district_name = 'none'

            if result.fields is not None:
                # Extract census data (2023)
                # The attribute is census2023, not census.census_year_2023
                if hasattr(result.fields, 'census2023') and result.fields.census2023:
                    census_2023 = result.fields.census2023
                    block_code = getattr(census_2023, 'block_code', None)
                    tract_code = getattr(census_2023, 'tract_code', None)
                    county_fips = getattr(census_2023, 'county_fips', None)

                # Extract school district data
                if hasattr(result.fields, 'school_districts') and result.fields.school_districts:
                    if hasattr(result.fields.school_districts, 'unified') and result.fields.school_districts.unified:
                        unified = result.fields.school_districts.unified
                        lea_code = getattr(unified, 'lea_code', '0000')
                        district_name = getattr(unified, 'name', 'none')

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
                'district_name': district_name
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

        # Step 2: Initialize Geocodio client
        logger.info("Step 2: Initializing Geocodio API client...")
        geocodio_client = Geocodio(GEOCODIO_API_KEY)
        logger.info("Geocodio client initialized successfully")

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
            api_results = batch_geocode_addresses(addresses_df, geocodio_client)

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

            # TODO: Step 9: Log to Snowflake (to be implemented next)
            logger.info("Step 9: Logging to Snowflake (NOT YET IMPLEMENTED)")

            # TODO: Step 10: Update IC database (to be implemented next)
            logger.info("Step 10: Updating IC database (NOT YET IMPLEMENTED)")

        # Calculate duration
        duration = time.time() - start_time

        logger.info("=" * 60)
        logger.info(f"Application completed successfully in {duration:.1f} seconds")
        logger.info("=" * 60)

        return 0

    except EnvironmentError as e:
        logger.error(f"Configuration error: {e}")
        return 1

    except AuthenticationError as e:
        logger.error(f"Geocodio API authentication error: {e}")
        return 2

    except InvalidRequestError as e:
        logger.error(f"Geocodio API request error: {e}")
        return 3

    except GeocodioError as e:
        logger.error(f"Geocodio API error: {e}")
        return 4

    except sf_errors.ProgrammingError as e:
        logger.error(f"Snowflake database error: {e}")
        return 5

    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        return 6


if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)
