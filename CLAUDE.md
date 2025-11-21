# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a Python-based geocoding application that processes student and parent addresses from Infinite Campus (IC) using the Geocode.io API. The script geocodes addresses, enriches them with census and school district data, and writes the results back to both Infinite Campus and Snowflake databases.

## Architecture

### Main Script: GeocodeICAddresses.py

The application follows a single-script architecture with three primary database connections:

1. **Infinite Campus (IC) Database** - Microsoft SQL Server connection via pyodbc
   - Source of addresses to geocode (queried via Snowflake view)
   - Target for updating latitude, longitude, tract, block, and district information

2. **Snowflake Data Warehouse** - Primary data source and logging
   - Queries `MPS_WAREHOUSE.DATALAKE.VIEW_ADDRESSESNEEDINGGEOCODED` for addresses needing geocoding (limited to top 500)
   - Queries `MPS_WAREHOUSE.IC.DISTRICT` for district ID lookups
   - Logs all Geocode.io API responses to `LOGGING_GEOCODIOAPI_ADDRESSLOOKUPS` as JSON

3. **Geocode.io API** - External geocoding service
   - Requests include fields: school districts, zip+4, census 2023 data
   - Returns lat/lng coordinates, census tract/block, county FIPS, and school district LEA codes

### Data Flow

1. Fetch up to 500 addresses from Snowflake view
2. For each address:
   - Call Geocode.io API with address and requested fields
   - Log full JSON response to Snowflake
   - If accuracy >= 0.8, update IC database with coordinates and census data
   - Match LEA code to district ID using the district lookup dataframe

### Configuration: config/IC_config.py

Contains credentials for all three services:
- `IC_DB_Credentials`: Infinite Campus database username/password
- `SnowflakeConnection`: Snowflake account, warehouse, database, schema
- `Geocodeio`: API key for Geocode.io

**Important**: The actual config file (`config/IC_config.py`) is gitignored. Use `config/IC_config_blank.py` as a template.

## Running the Application

```bash
python GeocodeICAddresses.py
```

The script has no command-line arguments. It processes up to 500 addresses per run.

## Key Implementation Details

- **Accuracy threshold**: Only addresses with accuracy >= 0.8 are written to IC database
- **District ID matching**: LEA codes from Geocode.io are matched against `NCESDISTRICTID` in the district table
- **Error handling**: Addresses that fail geocoding are logged to console but don't stop execution
- **Batch size**: Hardcoded to 500 addresses per run (line 42)
- **Census data**: Uses 2023 census tract, block, and county FIPS codes

## Database Connections

### Infinite Campus Connection
- Server: `icampus.mapleton.us:7771`
- Database: `mapleton`
- Driver: ODBC Driver 18 for SQL Server
- Trust server certificate: Yes

### Snowflake Connection
- Account: `dv56305.west-us-2.azure`
- Warehouse: `dataloadingonly`
- Database: `MPS_Warehouse` (note: IC_config_blank.py shows different database name)
- Schema: `datalake`

## Dependencies

The project requires:
- geocodio
- pyodbc
- snowflake-connector-python
- pandas
- sqlalchemy

Note: No requirements.txt file exists in the repository.
