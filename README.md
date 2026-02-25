# Address Geocoding Application

An internal application that geocodes student and parent addresses from Infinite Campus using the Geocod.io API. The application enriches addresses with census data and school district information, then updates both Infinite Campus and Snowflake databases.

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [Configuration](#configuration)
- [Running with Docker](#running-with-docker)
- [Running Locally](#running-locally)
- [Application Workflow](#application-workflow)
- [Database Updates](#database-updates)
- [Logging](#logging)
- [Troubleshooting](#troubleshooting)

## Overview

This application:

1. Fetches addresses needing geocoding from Snowflake (`VIEW_ADDRESSESNEEDINGGEOCODED`)
2. Geocodes each address using the Geocod.io API
3. Enriches results with census tract/block data and school district LEA codes
4. Logs all API responses to Snowflake for auditing
5. Updates the Infinite Campus `ADDRESS` table with geocoded data

## Architecture

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│                 │     │                 │     │                 │
│   Snowflake     │◄────│  GeocodeApp     │────►│  Geocod.io API  │
│   (read/write)  │     │  (Python)       │     │  (external)     │
│                 │     │                 │     │                 │
└─────────────────┘     └────────┬────────┘     └─────────────────┘
                                 │
                                 │
                                 ▼
                        ┌─────────────────┐
                        │                 │
                        │ Infinite Campus │
                        │ (SQL Server)    │
                        │                 │
                        └─────────────────┘
```

### Data Sources

| System | Purpose | Connection |
|--------|---------|------------|
| Snowflake | Source of addresses, API response logging, district lookup | snowflake-connector-python |
| Infinite Campus | Target for geocoded address updates | pyodbc (ODBC Driver 18) |
| Geocod.io | External geocoding API | REST API (requests) |

### Key Tables/Views

| Object | Database | Purpose |
|--------|----------|---------|
| `VIEW_ADDRESSESNEEDINGGEOCODED` | Snowflake | Addresses requiring geocoding |
| `LOGGING_GEOCODIOAPI_ADDRESSLOOKUPS` | Snowflake | Audit log of all API responses |
| `DISTRICT` | Snowflake | LEA code to District ID mapping |
| `ADDRESS` | Infinite Campus | Target table for updates |

## Prerequisites

### For Docker (Recommended)

- Docker Desktop installed and running
- `.env` file configured (see [Configuration](#configuration))

### For Local Development

- Python 3.11+
- Microsoft ODBC Driver 18 for SQL Server
- Access to Snowflake and Infinite Campus databases

## Configuration

1. Copy the example environment file:
   ```bash
   cp .env.example .env
   ```

2. Edit `.env` with your credentials:

   ```ini
   # Snowflake Data Warehouse (Key Pair Auth)
   SNOWFLAKE_USER=your_username
   SNOWFLAKE_PRIVATE_KEY_B64=your_base64_encoded_private_key
   # SNOWFLAKE_PRIVATE_KEY_PASSPHRASE=optional_passphrase
   SNOWFLAKE_ACCOUNT=dv56305.west-us-2.azure
   SNOWFLAKE_WAREHOUSE=dataloadingonly
   SNOWFLAKE_DATABASE=MPS_Warehouse
   SNOWFLAKE_SCHEMA=datalake

   # Infinite Campus Database (SQL Server)
   IC_DB_USER=your_username
   IC_DB_PASSWORD=your_password
   IC_DB_SERVER=icampus.mapleton.us,7771
   IC_DB_DATABASE=mapleton
   IC_DB_DRIVER=ODBC Driver 18 for SQL Server

   # Geocod.io API
   GEOCODIO_API_KEY=your_api_key

   # Application Settings
   BATCH_SIZE=500              # Addresses per run
   ACCURACY_THRESHOLD=0.8      # Minimum accuracy to update IC (0.0-1.0)
   LOG_LEVEL=INFO              # DEBUG, INFO, WARNING, ERROR
   ```

### Configuration Notes

- **BATCH_SIZE**: Controls how many addresses are processed per run. Default is 500. Lower this for testing.
- **ACCURACY_THRESHOLD**: Only addresses with geocoding accuracy >= this value are written to Infinite Campus. Default is 0.8 (80%).
- **Never commit `.env` to git** - it contains secrets.

## Running with Docker

Docker is the recommended way to run this application.

### Build the Image

```bash
docker-compose build
```

### Run the Application

**One-time execution (recommended):**
```bash
docker-compose run --rm geocoding
```

This runs the application, processes addresses, and removes the container when complete. Logs are persisted to `./logs/`.

**Run in foreground:**
```bash
docker-compose up
```

**Run in background:**
```bash
docker-compose up -d

# View logs
docker-compose logs -f
```

### Rebuild After Code Changes

```bash
docker-compose build --no-cache
docker-compose run --rm geocoding
```

## Running Locally

If you need to run outside Docker (for debugging):

### macOS Setup

1. Install ODBC driver:
   ```bash
   brew install microsoft/mssql-release/msodbcsql18
   brew install unixodbc
   ```

2. Create virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

3. Run:
   ```bash
   python GeocodeAddresses.py
   ```

### Windows Setup

1. Download and install [ODBC Driver 18 for SQL Server](https://docs.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server)

2. Create virtual environment:
   ```bash
   python -m venv venv
   venv\Scripts\activate
   pip install -r requirements.txt
   ```

3. Run:
   ```bash
   python GeocodeAddresses.py
   ```

## Application Workflow

The application executes the following steps:

| Step | Description |
|------|-------------|
| 1 | Validate configuration (check all required env vars) |
| 2 | Validate Geocod.io API key |
| 3 | Connect to Snowflake |
| 4 | Fetch addresses to geocode (up to BATCH_SIZE) |
| 5 | Fetch district lookup table (LEA code → District ID) |
| 6 | Geocode addresses via Geocod.io API |
| 7 | Parse geocoding results |
| 8 | Enrich with district IDs |
| 9 | Log results to Snowflake |
| 10 | Update Infinite Campus database |

### Geocod.io API Fields

The application requests these fields from Geocod.io:

- `school` - School district information (LEA codes)
- `zip4` - ZIP+4 codes
- `census2024` - Census tract, block, and county FIPS codes

## Database Updates

### Snowflake Logging

Every API response is logged to `LOGGING_GEOCODIOAPI_ADDRESSLOOKUPS`:

| Column | Type | Description |
|--------|------|-------------|
| IC_ADDRESSID | INTEGER | Address ID from Infinite Campus |
| GEOCODIO_JSON | VARIANT | Full JSON response from Geocod.io |

### Infinite Campus Updates

Addresses meeting the accuracy threshold are updated in the `ADDRESS` table:

| Column | Source |
|--------|--------|
| latitude | Geocod.io location.lat |
| longitude | Geocod.io location.lng |
| tract | Census 2024 tract_code |
| block | Census 2024 block_code |
| location_code | Census 2024 county_fips |
| districtID | Matched from LEA code (if found) |

**Note:** `districtID` is only updated if the LEA code matches a district in the lookup table.

## Logging

Logs are written to:

- **Console**: Always (visible in terminal/docker logs)
- **File**: `./logs/geocoding.log` (persisted via Docker volume mount)

### Log Levels

Set via `LOG_LEVEL` environment variable:

| Level | Description |
|-------|-------------|
| DEBUG | Detailed information for debugging |
| INFO | General operational messages (default) |
| WARNING | Unexpected situations that aren't errors |
| ERROR | Errors that prevent operation |

### Sample Log Output

```
2024-01-15 10:30:00 - geocoding - INFO - ============================================================
2024-01-15 10:30:00 - geocoding - INFO - Starting Address Geocoding Application v2.0
2024-01-15 10:30:00 - geocoding - INFO - ============================================================
2024-01-15 10:30:00 - geocoding - INFO - Step 1: Validating configuration...
2024-01-15 10:30:00 - geocoding - INFO - Configuration validated successfully
2024-01-15 10:30:01 - geocoding - INFO - Step 4: Fetching addresses to geocode...
2024-01-15 10:30:02 - geocoding - INFO - Successfully fetched 500 addresses
2024-01-15 10:30:45 - geocoding - INFO - Geocoding complete: 498 successful, 2 failed
2024-01-15 10:30:46 - geocoding - INFO - IC update complete: 485 updated, 0 errors, 15 skipped
```

## Troubleshooting

### ODBC Driver Not Found (macOS)

**Error:** `Can't open lib 'ODBC Driver 18 for SQL Server' : file not found`

**Solution:** The application auto-detects Homebrew's ODBC path on macOS. If running locally with Anaconda, ensure the driver is installed:
```bash
brew install microsoft/mssql-release/msodbcsql18
```

### Connection Timeouts

The application includes retry logic with exponential backoff for both Snowflake and Infinite Campus connections. If connections consistently fail:

1. Check network connectivity
2. Verify credentials in `.env`
3. Ensure you're on the correct network/VPN

### Low Accuracy Results

If many addresses are skipped due to low accuracy:

1. Check the address data quality in Infinite Campus
2. Review skipped addresses in the logs (DEBUG level)
3. Consider lowering `ACCURACY_THRESHOLD` (not recommended below 0.7)

### Geocod.io Rate Limits

The application makes individual API calls (not batch). If you hit rate limits:

1. Reduce `BATCH_SIZE`
2. Check your Geocod.io plan limits
3. Add delays between requests (requires code modification)

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Configuration error |
| 2 | Geocod.io authentication error |
| 3 | Geocod.io HTTP error |
| 4 | Geocod.io request error |
| 5 | Snowflake database error |
| 6 | Infinite Campus database error |
| 7 | Unexpected error |

## File Structure

```
Address-Geocoding/
├── GeocodeAddresses.py    # Main application
├── Dockerfile             # Docker image definition
├── docker-compose.yml     # Docker Compose configuration
├── requirements.txt       # Python dependencies
├── .env                   # Environment configuration (not in git)
├── .env.example           # Environment template
├── .dockerignore          # Docker build exclusions
├── .gitignore             # Git exclusions
├── logs/                  # Application logs (persisted)
│   └── geocoding.log
├── Deprecated/            # Old/unused files
└── README.md              # This file
```
