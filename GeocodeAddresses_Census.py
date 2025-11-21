#!/usr/bin/env python3
"""
Geocoding API Comparison Tool

This script compares geocoding results between:
1. Geocodio API (commercial service)
2. US Census Bureau Geocoder API (free government service)

It geocodes the same address with both services and displays a detailed
comparison of the results, including:
- Latitude/Longitude coordinates
- Census tract and block data
- School district information
- Data quality and accuracy metrics
"""

import os
import sys
import json
import logging
import time
from typing import Dict, Optional, Any, List
from datetime import datetime

import pandas as pd
import requests
import snowflake.connector
from snowflake.connector import SnowflakeConnection
import snowflake.connector.errors as sf_errors
from dotenv import load_dotenv
from geocodio import Geocodio
from geocodio.exceptions import GeocodioError

# Load environment variables
load_dotenv()

# ============================================
# Configuration
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
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')

# Application Settings
BATCH_SIZE = int(os.getenv('BATCH_SIZE', '10'))  # Default to 10 for comparison testing
DELAY_BETWEEN_ADDRESSES = float(os.getenv('DELAY_BETWEEN_ADDRESSES', '1.0'))  # Delay in seconds

# Census API Configuration
CENSUS_BASE_URL = "https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress"
CENSUS_BENCHMARK = "Public_AR_Current"
CENSUS_VINTAGE = "Current_Current"

# SQL Queries
QUERY_ADDRESSES = """
    SELECT TOP {limit} *
    FROM MPS_WAREHOUSE.DATALAKE.VIEW_ADDRESSESNEEDINGGEOCODED
    ORDER BY addressid DESC
"""

# Test address (can be overridden via command line)
DEFAULT_TEST_ADDRESS = "638 E 13th Ave, Denver, CO"


# ============================================
# Logging Configuration
# ============================================

def setup_logging() -> logging.Logger:
    """Configure logging for console output."""
    log_format = '%(asctime)s - %(levelname)s - %(message)s'
    log_level = getattr(logging, LOG_LEVEL.upper(), logging.INFO)

    logger = logging.getLogger('geocoding_comparison')
    logger.setLevel(log_level)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(logging.Formatter(log_format))
    logger.addHandler(console_handler)

    return logger


logger = setup_logging()


# ============================================
# Database Connection Functions
# ============================================

def create_snowflake_connection() -> SnowflakeConnection:
    """
    Create Snowflake connection with retry logic.

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


def fetch_addresses_to_geocode(snowflake_conn: SnowflakeConnection,
                               limit: int = BATCH_SIZE) -> pd.DataFrame:
    """
    Fetch addresses that need geocoding from Snowflake.

    Args:
        snowflake_conn: Active Snowflake connection
        limit: Maximum number of addresses to fetch

    Returns:
        pd.DataFrame: DataFrame containing addresses with ADDRESSID and ADDRESSLINE1 columns

    Raises:
        Exception: If query fails
    """
    try:
        logger.info(f"Fetching up to {limit} addresses from Snowflake...")

        cursor = snowflake_conn.cursor()
        query = QUERY_ADDRESSES.format(limit=limit)
        cursor.execute(query)
        df = cursor.fetch_pandas_all()
        cursor.close()

        logger.info(f"Successfully fetched {len(df)} addresses")

        if len(df) == 0:
            logger.warning("No addresses found that need geocoding")

        return df

    except sf_errors.ProgrammingError as e:
        logger.error(f"Snowflake query error: {e}")
        logger.error("Please check the view name and permissions")
        raise

    except Exception as e:
        logger.error(f"Error fetching addresses from Snowflake: {e}")
        raise


# ============================================
# Geocodio API Functions
# ============================================

def geocode_with_geocodio(address: str) -> Optional[Dict[str, Any]]:
    """
    Geocode an address using Geocodio API.

    Args:
        address: Full address string to geocode

    Returns:
        Dict containing parsed Geocodio results, or None if error
    """
    try:
        logger.info(f"Geocoding with Geocodio: {address}")

        if not GEOCODIO_API_KEY:
            logger.error("GEOCODIO_API_KEY not found in environment variables")
            return None

        client = Geocodio(GEOCODIO_API_KEY)

        # Request census and school district data
        response = client.geocode(address, fields=["school", "census2024"])

        if not response or not hasattr(response, 'results') or len(response.results) == 0:
            logger.warning("No results returned from Geocodio")
            return None

        # Get the first (best) result
        result = response.results[0]

        # Parse the result
        parsed = {
            'success': True,
            'matched_address': result.formatted_address if hasattr(result, 'formatted_address') else None,
            'latitude': result.location.lat if hasattr(result, 'location') else None,
            'longitude': result.location.lng if hasattr(result, 'location') else None,
            'accuracy': result.accuracy if hasattr(result, 'accuracy') else None,
            'accuracy_type': result.accuracy_type if hasattr(result, 'accuracy_type') else None,
            'source': result.source if hasattr(result, 'source') else None,
            'census_tract': None,
            'census_block': None,
            'county_fips': None,
            'school_district_unified': None,
            'school_district_elementary': None,
            'school_district_secondary': None,
            'school_lea_code': None,
            'raw_response': result
        }

        # Extract census data (try 2024, fall back to 2023)
        if hasattr(result, 'fields') and result.fields:
            # Try census2024 first
            if hasattr(result.fields, 'census2024') and result.fields.census2024:
                census = result.fields.census2024
                parsed['census_tract'] = getattr(census, 'tract_code', None)
                parsed['census_block'] = getattr(census, 'block_code', None)
                parsed['county_fips'] = getattr(census, 'county_fips', None)
                logger.info("Using Census 2024 data from Geocodio")
            # Fall back to census2023
            elif hasattr(result.fields, 'census2023') and result.fields.census2023:
                census = result.fields.census2023
                parsed['census_tract'] = getattr(census, 'tract_code', None)
                parsed['census_block'] = getattr(census, 'block_code', None)
                parsed['county_fips'] = getattr(census, 'county_fips', None)
                logger.info("Using Census 2023 data from Geocodio (2024 not available)")

            # Extract school district data
            if hasattr(result.fields, 'school_districts') and result.fields.school_districts:
                school = result.fields.school_districts

                # Unified school district
                if hasattr(school, 'unified') and school.unified:
                    parsed['school_district_unified'] = getattr(school.unified, 'name', None)
                    parsed['school_lea_code'] = getattr(school.unified, 'lea_code', None)

                # Elementary school district
                if hasattr(school, 'elementary') and school.elementary:
                    parsed['school_district_elementary'] = getattr(school.elementary, 'name', None)

                # Secondary school district
                if hasattr(school, 'secondary') and school.secondary:
                    parsed['school_district_secondary'] = getattr(school.secondary, 'name', None)

        logger.info("Successfully geocoded with Geocodio")
        return parsed

    except GeocodioError as e:
        logger.error(f"Geocodio API error: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error with Geocodio: {e}", exc_info=True)
        return None


# ============================================
# Census API Functions
# ============================================

def geocode_with_census(address: str) -> Optional[Dict[str, Any]]:
    """
    Geocode an address using US Census Bureau Geocoder API.

    Args:
        address: Full address string to geocode

    Returns:
        Dict containing parsed Census results, or None if error
    """
    try:
        logger.info(f"Geocoding with Census API: {address}")

        # Build API request
        params = {
            'address': address,
            'benchmark': CENSUS_BENCHMARK,
            'vintage': CENSUS_VINTAGE,
            'layers': 'all',  # Request all layers including school districts
            'format': 'json'
        }

        # Make the API request
        response = requests.get(CENSUS_BASE_URL, params=params, timeout=30)
        response.raise_for_status()

        data = response.json()

        # Check for results
        if 'result' not in data or 'addressMatches' not in data['result']:
            logger.warning("Unexpected Census API response format")
            return None

        matches = data['result']['addressMatches']

        if not matches or len(matches) == 0:
            logger.warning("No results returned from Census API")
            return None

        # Get the first (best) match
        match = matches[0]

        # Parse the result
        parsed = {
            'success': True,
            'matched_address': match.get('matchedAddress'),
            'latitude': None,
            'longitude': None,
            'accuracy': None,  # Census doesn't provide accuracy score
            'accuracy_type': match.get('addressComponents', {}).get('matchType'),
            'tiger_line_id': match.get('tigerLine', {}).get('tigerLineId'),
            'census_tract': None,
            'census_block': None,
            'census_block_group': None,
            'county_fips': None,
            'county_name': None,
            'state_fips': None,
            'state_name': None,
            'school_district_unified': None,
            'school_district_elementary': None,
            'school_district_secondary': None,
            'school_lea_unified': None,
            'school_lea_elementary': None,
            'school_lea_secondary': None,
            'raw_response': match
        }

        # Extract coordinates
        if 'coordinates' in match:
            coords = match['coordinates']
            parsed['longitude'] = coords.get('x')
            parsed['latitude'] = coords.get('y')

        # Extract geographic data
        if 'geographies' in match:
            geographies = match['geographies']

            # Census Blocks
            if 'Census Blocks' in geographies and len(geographies['Census Blocks']) > 0:
                block = geographies['Census Blocks'][0]
                parsed['census_block'] = block.get('BLOCK')
                parsed['census_block_group'] = block.get('BLKGRP')
                # Also get FIPS from block if not already set
                if not parsed['county_fips']:
                    parsed['county_fips'] = block.get('COUNTY')
                if not parsed['state_fips']:
                    parsed['state_fips'] = block.get('STATE')

            # Census Tracts
            if 'Census Tracts' in geographies and len(geographies['Census Tracts']) > 0:
                tract = geographies['Census Tracts'][0]
                parsed['census_tract'] = tract.get('TRACT')
                if not parsed['county_fips']:
                    parsed['county_fips'] = tract.get('COUNTY')
                if not parsed['state_fips']:
                    parsed['state_fips'] = tract.get('STATE')

            # Counties
            if 'Counties' in geographies and len(geographies['Counties']) > 0:
                county = geographies['Counties'][0]
                parsed['county_name'] = county.get('NAME')
                if not parsed['county_fips']:
                    parsed['county_fips'] = county.get('COUNTY')

            # States
            if 'States' in geographies and len(geographies['States']) > 0:
                state = geographies['States'][0]
                parsed['state_name'] = state.get('NAME')
                if not parsed['state_fips']:
                    parsed['state_fips'] = state.get('STATE')

            # School Districts
            if 'Unified School Districts' in geographies and len(geographies['Unified School Districts']) > 0:
                district = geographies['Unified School Districts'][0]
                parsed['school_district_unified'] = district.get('NAME')
                parsed['school_lea_unified'] = district.get('SDLEA')

            if 'Elementary School Districts' in geographies and len(geographies['Elementary School Districts']) > 0:
                district = geographies['Elementary School Districts'][0]
                parsed['school_district_elementary'] = district.get('NAME')
                parsed['school_lea_elementary'] = district.get('SDLEA')

            if 'Secondary School Districts' in geographies and len(geographies['Secondary School Districts']) > 0:
                district = geographies['Secondary School Districts'][0]
                parsed['school_district_secondary'] = district.get('NAME')
                parsed['school_lea_secondary'] = district.get('SDLEA')

        logger.info("Successfully geocoded with Census API")
        return parsed

    except requests.exceptions.RequestException as e:
        logger.error(f"Census API request error: {e}")
        return None
    except json.JSONDecodeError as e:
        logger.error(f"Census API JSON parsing error: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error with Census API: {e}", exc_info=True)
        return None


# ============================================
# Comparison and Display Functions
# ============================================

def calculate_coordinate_difference(lat1: Optional[float], lng1: Optional[float],
                                   lat2: Optional[float], lng2: Optional[float]) -> Optional[float]:
    """
    Calculate the distance between two coordinate pairs in meters (approximate).

    Uses the Haversine formula for great-circle distance.

    Args:
        lat1, lng1: First coordinate pair
        lat2, lng2: Second coordinate pair

    Returns:
        Distance in meters, or None if any coordinate is missing
    """
    if None in [lat1, lng1, lat2, lng2]:
        return None

    from math import radians, sin, cos, sqrt, atan2

    # Earth's radius in meters
    R = 6371000

    # Convert to radians
    lat1_rad = radians(lat1)
    lat2_rad = radians(lat2)
    delta_lat = radians(lat2 - lat1)
    delta_lng = radians(lng2 - lng1)

    # Haversine formula
    a = sin(delta_lat / 2) ** 2 + cos(lat1_rad) * cos(lat2_rad) * sin(delta_lng / 2) ** 2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))
    distance = R * c

    return distance


def display_comparison(address: str, geocodio_result: Optional[Dict], census_result: Optional[Dict]):
    """
    Display a detailed comparison of geocoding results from both APIs.

    Args:
        address: Original address that was geocoded
        geocodio_result: Parsed results from Geocodio
        census_result: Parsed results from Census API
    """
    print("\n" + "=" * 80)
    print("GEOCODING API COMPARISON RESULTS")
    print("=" * 80)
    print(f"\nOriginal Address: {address}")
    print(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("\n" + "=" * 80)

    # ----------------------------------------
    # Matched Addresses
    # ----------------------------------------
    print("\nMATCHED ADDRESSES")
    print("-" * 80)
    print(f"{'Source':<15} {'Matched Address'}")
    print("-" * 80)
    print(f"{'Geocodio:':<15} {geocodio_result.get('matched_address', 'N/A') if geocodio_result else 'ERROR'}")
    print(f"{'Census:':<15} {census_result.get('matched_address', 'N/A') if census_result else 'ERROR'}")

    # ----------------------------------------
    # Coordinates (Latitude/Longitude)
    # ----------------------------------------
    print("\n" + "=" * 80)
    print("COORDINATES")
    print("-" * 80)
    print(f"{'Source':<15} {'Latitude':<15} {'Longitude':<15}")
    print("-" * 80)

    geo_lat = geocodio_result.get('latitude') if geocodio_result else None
    geo_lng = geocodio_result.get('longitude') if geocodio_result else None
    cen_lat = census_result.get('latitude') if census_result else None
    cen_lng = census_result.get('longitude') if census_result else None

    print(f"{'Geocodio:':<15} {geo_lat if geo_lat else 'N/A':<15} {geo_lng if geo_lng else 'N/A':<15}")
    print(f"{'Census:':<15} {cen_lat if cen_lat else 'N/A':<15} {cen_lng if cen_lng else 'N/A':<15}")

    # Calculate difference
    if geo_lat and geo_lng and cen_lat and cen_lng:
        distance = calculate_coordinate_difference(geo_lat, geo_lng, cen_lat, cen_lng)
        if distance is not None:
            print("-" * 80)
            print(f"{'Difference:':<15} {distance:.2f} meters apart")
            if distance < 10:
                print(f"{'Assessment:':<15} Excellent agreement (< 10m)")
            elif distance < 50:
                print(f"{'Assessment:':<15} Good agreement (< 50m)")
            elif distance < 100:
                print(f"{'Assessment:':<15} Fair agreement (< 100m)")
            else:
                print(f"{'Assessment:':<15} Poor agreement (>= 100m)")

    # ----------------------------------------
    # Accuracy Metrics
    # ----------------------------------------
    print("\n" + "=" * 80)
    print("ACCURACY METRICS")
    print("-" * 80)
    print(f"{'Source':<15} {'Accuracy Score':<20} {'Accuracy Type':<25} {'Data Source'}")
    print("-" * 80)

    geo_acc = geocodio_result.get('accuracy') if geocodio_result else None
    geo_acc_type = geocodio_result.get('accuracy_type') if geocodio_result else None
    geo_source = geocodio_result.get('source') if geocodio_result else None

    cen_acc_type = census_result.get('accuracy_type') if census_result else None
    cen_tiger = census_result.get('tiger_line_id') if census_result else None

    print(f"{'Geocodio:':<15} {str(geo_acc) if geo_acc else 'N/A':<20} {str(geo_acc_type) if geo_acc_type else 'N/A':<25} {str(geo_source) if geo_source else 'N/A'}")
    print(f"{'Census:':<15} {'Not provided':<20} {str(cen_acc_type) if cen_acc_type else 'N/A':<25} {f'TIGER {cen_tiger}' if cen_tiger else 'N/A'}")

    # ----------------------------------------
    # Census Data
    # ----------------------------------------
    print("\n" + "=" * 80)
    print("CENSUS DATA")
    print("-" * 80)
    print(f"{'Source':<15} {'Tract':<15} {'Block':<15} {'County FIPS':<15}")
    print("-" * 80)

    geo_tract = geocodio_result.get('census_tract') if geocodio_result else None
    geo_block = geocodio_result.get('census_block') if geocodio_result else None
    geo_county = geocodio_result.get('county_fips') if geocodio_result else None

    cen_tract = census_result.get('census_tract') if census_result else None
    cen_block = census_result.get('census_block') if census_result else None
    cen_county = census_result.get('county_fips') if census_result else None

    print(f"{'Geocodio:':<15} {str(geo_tract) if geo_tract else 'N/A':<15} {str(geo_block) if geo_block else 'N/A':<15} {str(geo_county) if geo_county else 'N/A':<15}")
    print(f"{'Census:':<15} {str(cen_tract) if cen_tract else 'N/A':<15} {str(cen_block) if cen_block else 'N/A':<15} {str(cen_county) if cen_county else 'N/A':<15}")

    # Comparison
    print("-" * 80)
    tract_match = "✓ MATCH" if geo_tract == cen_tract and geo_tract else "✗ DIFFER" if geo_tract and cen_tract else "- N/A"
    block_match = "✓ MATCH" if geo_block == cen_block and geo_block else "✗ DIFFER" if geo_block and cen_block else "- N/A"
    county_match = "✓ MATCH" if geo_county == cen_county and geo_county else "✗ DIFFER" if geo_county and cen_county else "- N/A"

    print(f"{'Comparison:':<15} Tract: {tract_match:<10} Block: {block_match:<10} County: {county_match}")

    # ----------------------------------------
    # School Districts
    # ----------------------------------------
    print("\n" + "=" * 80)
    print("SCHOOL DISTRICTS")
    print("-" * 80)

    # Unified Districts
    print("\nUnified School Districts:")
    print(f"{'Source':<15} {'District Name':<50} {'LEA Code'}")
    print("-" * 80)

    geo_unified = geocodio_result.get('school_district_unified') if geocodio_result else None
    geo_lea = geocodio_result.get('school_lea_code') if geocodio_result else None
    cen_unified = census_result.get('school_district_unified') if census_result else None
    cen_lea_unified = census_result.get('school_lea_unified') if census_result else None

    print(f"{'Geocodio:':<15} {str(geo_unified) if geo_unified else 'N/A':<50} {str(geo_lea) if geo_lea else 'N/A'}")
    print(f"{'Census:':<15} {str(cen_unified) if cen_unified else 'N/A':<50} {str(cen_lea_unified) if cen_lea_unified else 'N/A'}")

    # Elementary Districts
    geo_elem = geocodio_result.get('school_district_elementary') if geocodio_result else None
    cen_elem = census_result.get('school_district_elementary') if census_result else None
    cen_lea_elem = census_result.get('school_lea_elementary') if census_result else None

    if geo_elem or cen_elem:
        print("\nElementary School Districts:")
        print(f"{'Source':<15} {'District Name':<50} {'LEA Code'}")
        print("-" * 80)
        print(f"{'Geocodio:':<15} {str(geo_elem) if geo_elem else 'N/A':<50} {'N/A'}")
        print(f"{'Census:':<15} {str(cen_elem) if cen_elem else 'N/A':<50} {str(cen_lea_elem) if cen_lea_elem else 'N/A'}")

    # Secondary Districts
    geo_sec = geocodio_result.get('school_district_secondary') if geocodio_result else None
    cen_sec = census_result.get('school_district_secondary') if census_result else None
    cen_lea_sec = census_result.get('school_lea_secondary') if census_result else None

    if geo_sec or cen_sec:
        print("\nSecondary School Districts:")
        print(f"{'Source':<15} {'District Name':<50} {'LEA Code'}")
        print("-" * 80)
        print(f"{'Geocodio:':<15} {str(geo_sec) if geo_sec else 'N/A':<50} {'N/A'}")
        print(f"{'Census:':<15} {str(cen_sec) if cen_sec else 'N/A':<50} {str(cen_lea_sec) if cen_lea_sec else 'N/A'}")

    # ----------------------------------------
    # Additional Census Geography
    # ----------------------------------------
    if census_result:
        cen_state = census_result.get('state_name')
        cen_county_name = census_result.get('county_name')

        if cen_state or cen_county_name:
            print("\n" + "=" * 80)
            print("ADDITIONAL CENSUS GEOGRAPHY")
            print("-" * 80)
            if cen_state:
                print(f"State:          {cen_state} (FIPS: {census_result.get('state_fips', 'N/A')})")
            if cen_county_name:
                print(f"County:         {cen_county_name} (FIPS: {cen_county})")

    # ----------------------------------------
    # Summary
    # ----------------------------------------
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)

    print("\nGeocoding Success:")
    print(f"  Geocodio:     {'✓ Success' if geocodio_result and geocodio_result.get('success') else '✗ Failed'}")
    print(f"  Census:       {'✓ Success' if census_result and census_result.get('success') else '✗ Failed'}")

    print("\nData Completeness:")

    # Count available fields
    if geocodio_result:
        geo_fields = sum([
            1 if geocodio_result.get('latitude') else 0,
            1 if geocodio_result.get('census_tract') else 0,
            1 if geocodio_result.get('census_block') else 0,
            1 if geocodio_result.get('school_district_unified') else 0
        ])
        print(f"  Geocodio:     {geo_fields}/4 key fields populated")

    if census_result:
        cen_fields = sum([
            1 if census_result.get('latitude') else 0,
            1 if census_result.get('census_tract') else 0,
            1 if census_result.get('census_block') else 0,
            1 if census_result.get('school_district_unified') else 0
        ])
        print(f"  Census:       {cen_fields}/4 key fields populated")

    print("\n" + "=" * 80)


def display_overall_summary(results: List[Dict[str, Any]]):
    """
    Display an overall summary of all geocoding comparisons.

    Args:
        results: List of comparison results from process_single_address
    """
    print("\n\n" + "=" * 80)
    print("OVERALL SUMMARY - ALL ADDRESSES")
    print("=" * 80)

    total = len(results)
    geocodio_success = sum(1 for r in results if r['geocodio_success'])
    census_success = sum(1 for r in results if r['census_success'])
    both_success = sum(1 for r in results if r['geocodio_success'] and r['census_success'])

    print(f"\nTotal Addresses Processed: {total}")
    print(f"\nSuccess Rates:")
    print(f"  Geocodio:              {geocodio_success}/{total} ({geocodio_success/total*100:.1f}%)")
    print(f"  Census:                {census_success}/{total} ({census_success/total*100:.1f}%)")
    print(f"  Both APIs Successful:  {both_success}/{total} ({both_success/total*100:.1f}%)")

    # Calculate match statistics for addresses where both succeeded
    if both_success > 0:
        tract_matches = sum(1 for r in results if r['census_tract_match'])
        block_matches = sum(1 for r in results if r['census_block_match'])
        county_matches = sum(1 for r in results if r['county_fips_match'])

        distances = [r['coordinate_distance'] for r in results if r['coordinate_distance'] is not None]

        print(f"\nData Agreement (when both APIs succeeded):")
        print(f"  Census Tract Match:    {tract_matches}/{both_success} ({tract_matches/both_success*100:.1f}%)")
        print(f"  Census Block Match:    {block_matches}/{both_success} ({block_matches/both_success*100:.1f}%)")
        print(f"  County FIPS Match:     {county_matches}/{both_success} ({county_matches/both_success*100:.1f}%)")

        if distances:
            avg_distance = sum(distances) / len(distances)
            min_distance = min(distances)
            max_distance = max(distances)

            print(f"\nCoordinate Distance Statistics:")
            print(f"  Average:               {avg_distance:.2f} meters")
            print(f"  Minimum:               {min_distance:.2f} meters")
            print(f"  Maximum:               {max_distance:.2f} meters")

            # Distance categories
            under_10m = sum(1 for d in distances if d < 10)
            under_50m = sum(1 for d in distances if d < 50)
            under_100m = sum(1 for d in distances if d < 100)

            print(f"\nDistance Distribution:")
            print(f"  < 10m (Excellent):     {under_10m}/{len(distances)} ({under_10m/len(distances)*100:.1f}%)")
            print(f"  < 50m (Good):          {under_50m}/{len(distances)} ({under_50m/len(distances)*100:.1f}%)")
            print(f"  < 100m (Fair):         {under_100m}/{len(distances)} ({under_100m/len(distances)*100:.1f}%)")
            print(f"  >= 100m (Poor):        {len(distances)-under_100m}/{len(distances)} ({(len(distances)-under_100m)/len(distances)*100:.1f}%)")

    # Show addresses where APIs disagreed significantly
    print(f"\n" + "=" * 80)
    print("ADDRESSES WITH SIGNIFICANT DIFFERENCES")
    print("=" * 80)

    has_differences = False
    for result in results:
        issues = []

        if result['geocodio_success'] != result['census_success']:
            if result['geocodio_success']:
                issues.append("Geocodio succeeded, Census failed")
            else:
                issues.append("Census succeeded, Geocodio failed")

        if result['coordinate_distance'] and result['coordinate_distance'] >= 100:
            issues.append(f"Coordinates differ by {result['coordinate_distance']:.1f}m")

        if result['geocodio_success'] and result['census_success']:
            if not result['census_tract_match']:
                issues.append("Census tract mismatch")
            if not result['census_block_match']:
                issues.append("Census block mismatch")

        if issues:
            has_differences = True
            print(f"\nAddress ID: {result.get('address_id', 'N/A')}")
            print(f"Address:    {result['address']}")
            print(f"Issues:     {', '.join(issues)}")

    if not has_differences:
        print("\nNo significant differences found! Both APIs produced consistent results.")

    print("\n" + "=" * 80)


# ============================================
# Main Execution
# ============================================

def process_single_address(address: str) -> Dict[str, Any]:
    """
    Process a single address with both APIs and return comparison data.

    Args:
        address: Address string to geocode

    Returns:
        Dict containing both API results and comparison metrics
    """
    # Geocode with both APIs
    geocodio_result = geocode_with_geocodio(address)

    # Add delay to be respectful to APIs
    time.sleep(0.5)

    census_result = geocode_with_census(address)

    # Calculate comparison metrics
    comparison = {
        'address': address,
        'geocodio_result': geocodio_result,
        'census_result': census_result,
        'geocodio_success': geocodio_result is not None and geocodio_result.get('success', False),
        'census_success': census_result is not None and census_result.get('success', False),
        'coordinate_distance': None,
        'census_tract_match': False,
        'census_block_match': False,
        'county_fips_match': False
    }

    # Calculate coordinate distance
    if geocodio_result and census_result:
        geo_lat = geocodio_result.get('latitude')
        geo_lng = geocodio_result.get('longitude')
        cen_lat = census_result.get('latitude')
        cen_lng = census_result.get('longitude')

        if all([geo_lat, geo_lng, cen_lat, cen_lng]):
            comparison['coordinate_distance'] = calculate_coordinate_difference(
                geo_lat, geo_lng, cen_lat, cen_lng
            )

        # Check census data matches
        geo_tract = geocodio_result.get('census_tract')
        cen_tract = census_result.get('census_tract')
        comparison['census_tract_match'] = (geo_tract == cen_tract and geo_tract is not None)

        geo_block = geocodio_result.get('census_block')
        cen_block = census_result.get('census_block')
        comparison['census_block_match'] = (geo_block == cen_block and geo_block is not None)

        geo_county = geocodio_result.get('county_fips')
        cen_county = census_result.get('county_fips')
        comparison['county_fips_match'] = (geo_county == cen_county and geo_county is not None)

    return comparison


def main():
    """
    Main execution function.

    Checks for Snowflake credentials and either:
    1. Fetches addresses from Snowflake and compares both APIs for each
    2. Falls back to single address mode (from command line or default)
    """
    start_time = time.time()

    logger.info("=" * 80)
    logger.info("Geocoding API Comparison Tool")
    logger.info("=" * 80)
    logger.info("")

    try:
        # Check if we should use Snowflake
        use_snowflake = SNOWFLAKE_USER and SNOWFLAKE_PASSWORD

        if use_snowflake:
            logger.info("Snowflake credentials found - fetching addresses from database")

            # Connect to Snowflake
            logger.info("Step 1: Connecting to Snowflake...")
            with create_snowflake_connection() as sf_conn:

                # Fetch addresses
                logger.info("Step 2: Fetching addresses to geocode...")
                addresses_df = fetch_addresses_to_geocode(sf_conn, BATCH_SIZE)

                if len(addresses_df) == 0:
                    logger.info("No addresses to process. Exiting.")
                    return 0

                logger.info(f"Will process {len(addresses_df)} addresses")
                logger.info(f"Delay between addresses: {DELAY_BETWEEN_ADDRESSES} seconds")
                logger.info("")

                # Process each address
                results = []
                for idx, row in addresses_df.iterrows():
                    address_id = row['ADDRESSID']
                    address = row['ADDRESSLINE1']

                    logger.info("=" * 80)
                    logger.info(f"Processing address {idx + 1}/{len(addresses_df)}")
                    logger.info(f"Address ID: {address_id}")
                    logger.info(f"Address: {address}")
                    logger.info("=" * 80)

                    # Process this address
                    comparison = process_single_address(address)
                    comparison['address_id'] = address_id
                    results.append(comparison)

                    # Display individual comparison
                    display_comparison(
                        address,
                        comparison['geocodio_result'],
                        comparison['census_result']
                    )

                    # Delay between addresses (except for last one)
                    if idx < len(addresses_df) - 1:
                        logger.info(f"Waiting {DELAY_BETWEEN_ADDRESSES} seconds before next address...")
                        time.sleep(DELAY_BETWEEN_ADDRESSES)

                # Display overall summary
                display_overall_summary(results)

                duration = time.time() - start_time
                logger.info(f"\nTotal processing time: {duration:.1f} seconds")

        else:
            # Single address mode
            logger.info("No Snowflake credentials - using single address mode")

            # Get address from command line or use default
            if len(sys.argv) > 1:
                test_address = ' '.join(sys.argv[1:])
            else:
                test_address = DEFAULT_TEST_ADDRESS

            logger.info(f"Test address: {test_address}")
            logger.info("")

            # Process single address
            comparison = process_single_address(test_address)

            # Display comparison
            display_comparison(
                test_address,
                comparison['geocodio_result'],
                comparison['census_result']
            )

        return 0

    except Exception as e:
        logger.error(f"Error during execution: {e}", exc_info=True)
        return 1


if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)
