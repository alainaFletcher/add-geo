# Address Geocoding Application - Modernization & Improvement Plan

**Project**: Address-Geocoding
**Date**: November 10, 2025
**Author**: Senior Architecture Review
**Audience**: Development Team & Junior Developers

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Current State Analysis](#current-state-analysis)
3. [Proposed Architecture](#proposed-architecture)
4. [Docker Implementation](#docker-implementation)
5. [Detailed Improvements](#detailed-improvements)
6. [Implementation Plan](#implementation-plan)
7. [Testing Strategy](#testing-strategy)
8. [Deployment Guide](#deployment-guide)
9. [Maintenance & Support](#maintenance--support)

---

## Executive Summary

### Goals

Transform the current geocoding application into a maintainable, performant, and containerized solution that can be easily supported by junior developers.

### Key Improvements

| Area | Current State | Proposed State | Impact |
|------|--------------|----------------|---------|
| **API Calls** | 500 individual calls | 1 batch call | 99% reduction in API calls |
| **Execution Time** | 5-10 minutes | 30-60 seconds | 10x faster |
| **Code Structure** | 1 function (158 lines) | 8 functions (~20 lines each) | Much easier to maintain |
| **Database Ops** | 1000+ operations | 5 operations | 10x faster database work |
| **Deployment** | Manual Python setup | Docker container | Consistent, portable |
| **Configuration** | Hardcoded credentials | Environment variables | Secure, flexible |
| **Error Handling** | Silent failures | Explicit logging | Clear troubleshooting |
| **Logging** | Print statements | Structured logging | Professional, debuggable |

### Business Value

- **Cost Reduction**: Fewer API calls = lower Geocodio costs
- **Time Savings**: 90% faster execution frees up infrastructure
- **Maintainability**: Junior developers can confidently modify and troubleshoot
- **Security**: Credentials no longer in code/git
- **Reliability**: Better error handling reduces failed runs
- **Portability**: Runs identically on any machine with Docker

---

## Current State Analysis

### Critical Issues Identified

#### 1. Performance & Cost Issue (HIGHEST PRIORITY)

**Location**: `GeocodeICAddresses.py:69-160`

**Problem**:
```python
for index, row in df.iterrows():
    location = client.geocode(row['ADDRESSLINE1'], ...)  # 500 API calls!
```

**Impact**:
- 500 individual API calls (slow, expensive)
- 5-10 minute execution time
- Higher API costs
- More points of failure

**Solution**: Batch geocoding - 1 API call for all 500 addresses

---

#### 2. Security & Configuration Issue (CRITICAL)

**Location**: `config/IC_config.py:2-17`

**Problem**:
- Hardcoded passwords in Python file
- Credentials visible in plaintext
- Different environments require code changes
- Not suitable for containerization

**Current Code**:
```python
IC_DB_Credentials = {
    "user": "fullerb",
    "passwd": "M0unta1n"  # ← Password in code!
}
```

**Solution**: Environment variables with `.env` file

---

#### 3. Error Handling Issues

**Location**: Multiple locations (`GeocodeICAddresses.py:73, 82, 101, 155`)

**Problem**: Bare `except:` clauses hide errors

**Examples**:
```python
try:
    location = client.geocode(row['ADDRESSLINE1'], fields=["school","zip4","census2023"])
except:
    print("Error geocoding address: " + row['ADDRESSLINE1'])
    # Error hidden, execution continues, but what went wrong?
```

**Impact**:
- Silent failures in production
- No visibility into what's breaking
- Difficult to troubleshoot
- Cannot determine if issue is API, network, or data

**Solution**: Specific exception types with proper logging

---

#### 4. Code Organization Issues

**Location**: `GeocodeICAddresses.py:12-170`

**Problem**: Single 158-line `main()` function does everything

**Issues**:
- Hard to test individual components
- Hard to understand flow
- Hard to modify without breaking
- Cannot reuse logic

**Solution**: Break into 8 focused functions

---

#### 5. Database Connection Issues

**Location**: `GeocodeICAddresses.py:24-40, 154`

**Problems**:

1. **Individual commits per row** (line 154):
   ```python
   cupdate.execute(sqlUpdate, args)
   cupdate.commit()  # ← 500 separate commits!
   ```

2. **Cursor created twice** (lines 46, 66):
   ```python
   cs = ctx.cursor()  # Line 46
   # ...
   cs = ctx.cursor()  # Line 66 - overwrites first one
   ```

3. **No connection error recovery**
4. **Connections held open for entire execution**

**Solution**: Context managers, batch operations, retry logic

---

#### 6. Logging & Observability Issues

**Location**: Throughout file

**Problem**: Print statements instead of structured logging

**Current**:
```python
print("Error geocoding address: " + row['ADDRESSLINE1'])
print("District: ",loc_district)
```

**Issues**:
- No timestamps
- No log levels (info vs warning vs error)
- Can't filter or search logs
- Not suitable for production monitoring

**Solution**: Python `logging` module with proper formatting

---

#### 7. Data Flow Inefficiency

**Current Flow**:
```
For each of 500 addresses:
  1. Call Geocodio API        (500 API calls)
  2. Insert to Snowflake      (500 inserts)
  3. Update IC database       (500 updates)
Total: 1000+ database operations
```

**Better Flow**:
```
1. Fetch 500 addresses         (1 query)
2. Batch geocode all          (1 API call)
3. Batch insert to Snowflake  (1 insert)
4. Batch update IC            (1 update)
Total: 4 database operations
```

**Impact**: 250x reduction in operations

---

#### 8. Hardcoded Configuration Values

**Locations**: Throughout file

**Examples**:
- Line 42: `"select top 500 *"` - batch size hardcoded
- Line 143: `if loc_accuracy >= .8:` - threshold hardcoded
- Line 24: `'Server=icampus.mapleton.us,7771;'` - server hardcoded

**Impact**: Requires code changes to adjust configuration

**Solution**: Configuration constants at top of file

---

#### 9. No Containerization

**Current**: Runs directly on host machine

**Problems**:
- "Works on my machine" issues
- Dependency conflicts with other Python projects
- Manual Python environment setup required
- ODBC driver installation required
- Version inconsistencies across machines
- Cannot leverage container orchestration

**Solution**: Docker containerization

---

## Proposed Architecture

### High-Level Design

```
┌─────────────────────────────────────────────────────────────┐
│                    Docker Container                          │
│  ┌───────────────────────────────────────────────────────┐  │
│  │         GeocodeICAddresses.py (Refactored)            │  │
│  ├───────────────────────────────────────────────────────┤  │
│  │  Configuration (from environment variables)           │  │
│  │  - Database credentials                               │  │
│  │  - API keys                                           │  │
│  │  - Application settings                               │  │
│  ├───────────────────────────────────────────────────────┤  │
│  │  Connection Functions                                 │  │
│  │  - Snowflake (with retry logic)                      │  │
│  │  - Infinite Campus (with retry logic)                │  │
│  │  - Geocodio API client                               │  │
│  ├───────────────────────────────────────────────────────┤  │
│  │  Data Fetching                                        │  │
│  │  - fetch_addresses_to_geocode() → 500 addresses      │  │
│  │  - fetch_district_lookup() → district mapping        │  │
│  ├───────────────────────────────────────────────────────┤  │
│  │  Geocoding (Batch Processing)                        │  │
│  │  - batch_geocode_addresses() → 1 API call            │  │
│  │  - parse_geocode_results() → structured data         │  │
│  ├───────────────────────────────────────────────────────┤  │
│  │  Data Processing                                      │  │
│  │  - enrich_with_district_ids()                        │  │
│  │  - filter_by_accuracy()                              │  │
│  ├───────────────────────────────────────────────────────┤  │
│  │  Database Updates                                     │  │
│  │  - bulk_insert_snowflake() → 1 insert                │  │
│  │  - bulk_update_ic() → 1 update                       │  │
│  ├───────────────────────────────────────────────────────┤  │
│  │  Logging & Reporting                                  │  │
│  │  - Structured logs to stdout (docker logs)           │  │
│  │  - Summary statistics                                 │  │
│  │  - Exit codes (0=success, 1=error)                   │  │
│  └───────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
         │                    │                    │
         ↓                    ↓                    ↓
   ┌──────────┐        ┌──────────┐        ┌──────────┐
   │Snowflake │        │ IC (SQL) │        │Geocodio  │
   │ Database │        │ Database │        │   API    │
   └──────────┘        └──────────┘        └──────────┘
```

### Function Breakdown

The refactored application will consist of 8 focused functions:

| Function Name | Purpose | Lines | Input | Output |
|--------------|---------|-------|-------|--------|
| `validate_config()` | Check required env vars | ~15 | None | None (raises if invalid) |
| `create_snowflake_connection()` | Connect to Snowflake | ~20 | None | Connection object |
| `create_ic_connection()` | Connect to IC database | ~20 | None | Connection object |
| `fetch_addresses_to_geocode()` | Get addresses from Snowflake | ~15 | Connection, limit | DataFrame |
| `batch_geocode_addresses()` | Call Geocodio API | ~25 | Address list, client | API results |
| `parse_geocode_results()` | Convert API JSON to DataFrame | ~30 | API results | DataFrame |
| `bulk_insert_snowflake()` | Log results to Snowflake | ~20 | DataFrame, connection | Insert count |
| `bulk_update_ic()` | Update IC with geocoded data | ~25 | DataFrame, connection | Update count |

**Total**: ~170 lines (similar to current) but much more maintainable

---

### File Structure

```
Address-Geocoding/
│
├── .env.example                    # Template for environment variables
├── .env                            # Actual secrets (gitignored)
├── .gitignore                      # Excludes .env, config/, logs/
├── .dockerignore                   # Excludes from Docker build
│
├── Dockerfile                      # Container definition
├── docker-compose.yml              # Local development & deployment
├── requirements.txt                # Python dependencies
│
├── GeocodeICAddresses.py          # Main application (refactored)
│
├── config/
│   ├── IC_config_blank.py         # Template (kept for reference)
│   └── README.md                   # Setup documentation
│
├── logs/                           # Volume mount for container logs
│   └── geocoding.log               # Generated log file
│
├── planning/
│   └── initial_plan.md            # This document
│
├── CLAUDE.md                       # AI assistant guidance
├── README.md                       # Project documentation
└── LICENSE.txt                     # License file
```

---

## Docker Implementation

### Container Design Principles

1. **Single Responsibility**: Container runs one geocoding job, then exits
2. **Stateless**: No data stored in container (uses external databases)
3. **12-Factor App**: All configuration via environment variables
4. **Logging**: All logs to stdout/stderr for `docker logs`
5. **Security**: Runs as non-root user
6. **Minimal**: Only necessary packages installed

---

### Dockerfile

```dockerfile
# Use official Python slim image (smaller, more secure)
FROM python:3.11-slim

# Metadata
LABEL maintainer="Mapleton Public Schools"
LABEL description="Address Geocoding Service"
LABEL version="2.0"

# Create non-root user for security
RUN useradd -m -u 1000 geocoder

# Install system dependencies (ODBC drivers for SQL Server)
RUN apt-get update && apt-get install -y \
    curl \
    gnupg \
    unixodbc-dev \
    && curl https://packages.microsoft.com/keys/microsoft.asc | apt-key add - \
    && curl https://packages.microsoft.com/config/debian/11/prod.list > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y msodbcsql18 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Create logs directory
RUN mkdir -p /app/logs && chown geocoder:geocoder /app/logs

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY GeocodeICAddresses.py .

# Switch to non-root user
USER geocoder

# Run the application
CMD ["python", "-u", "GeocodeICAddresses.py"]
```

**Key Features**:
- Base image: `python:3.11-slim` (~200MB vs ~900MB for full Python)
- Non-root user `geocoder` for security
- ODBC Driver 18 for SQL Server connectivity
- Layer caching optimization (requirements installed before code copy)
- `-u` flag for unbuffered output (better for Docker logs)

---

### docker-compose.yml

```yaml
version: '3.8'

services:
  geocoder:
    build: .
    container_name: address-geocoder

    # Load environment variables from .env file
    env_file:
      - .env

    # Mount logs directory for persistence
    volumes:
      - ./logs:/app/logs

    # Restart policy (unless-stopped for cron compatibility)
    restart: unless-stopped

    # Resource limits (adjust based on server capacity)
    deploy:
      resources:
        limits:
          cpus: '1.0'
          memory: 512M
        reservations:
          cpus: '0.5'
          memory: 256M

    # Logging configuration (12-week retention)
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "84"  # 12 weeks × 7 days = 84 days (1 file per day)
```

**Configuration Notes**:
- **Resource Limits**: 512MB max memory, 1 CPU core
- **Log Retention**: 84 files × 10MB = 840MB max log storage
- **Restart Policy**: `unless-stopped` (won't auto-restart; suitable for cron jobs)

---

### Environment Configuration

#### .env.example (Template)

```bash
#################################################
# Address Geocoding - Environment Configuration
# Copy this file to .env and fill in real values
# NEVER commit .env to git!
#################################################

# ============================================
# Infinite Campus Database Credentials
# ============================================
IC_DB_USER=your_username_here
IC_DB_PASSWORD=your_password_here
IC_DB_HOST=icampus.mapleton.us
IC_DB_PORT=7771
IC_DB_NAME=mapleton

# ============================================
# Snowflake Data Warehouse Credentials
# ============================================
SNOWFLAKE_USER=your_username_here
SNOWFLAKE_PASSWORD=your_password_here
SNOWFLAKE_ACCOUNT=dv56305.west-us-2.azure
SNOWFLAKE_WAREHOUSE=dataloadingonly
SNOWFLAKE_DATABASE=MPS_Warehouse
SNOWFLAKE_SCHEMA=datalake

# ============================================
# Geocodio API
# ============================================
GEOCODIO_API_KEY=your_api_key_here

# ============================================
# Application Settings
# ============================================
# Number of addresses to process per run
BATCH_SIZE=500

# Minimum accuracy to update IC database (0.0 to 1.0)
ACCURACY_THRESHOLD=0.8

# Logging level (DEBUG, INFO, WARNING, ERROR)
LOG_LEVEL=INFO

# API fields to request from Geocodio
API_FIELDS=school,zip4,census2023
```

#### Setup Instructions

1. Copy template: `cp .env.example .env`
2. Edit `.env` with actual credentials
3. Verify `.env` is in `.gitignore` (already configured)

---

### Log Retention Strategy

**Requirement**: Keep logs for 12 weeks (84 days)

**Implementation**:

1. **Docker logging driver** (configured in `docker-compose.yml`):
   ```yaml
   logging:
     driver: "json-file"
     options:
       max-size: "10m"      # Rotate when file reaches 10MB
       max-file: "84"       # Keep 84 files (12 weeks)
   ```

2. **Application log file** (mounted volume at `./logs/geocoding.log`):
   ```bash
   # Add to crontab for weekly cleanup
   0 0 * * 0 find /path/to/Address-Geocoding/logs -name "*.log" -mtime +84 -delete
   ```

**Storage Calculation**:
- Docker logs: 84 files × 10MB = 840MB max
- Application logs: ~1MB per run × 84 runs = 84MB max
- **Total**: ~1GB for 12 weeks of logs

---

### Deployment: Cron Job Configuration

**Requirement**: Run daily via cron, with option for manual execution

#### Option 1: Simple Cron (Recommended)

Add to crontab (`crontab -e`):

```bash
# Run address geocoding daily at 2:00 AM
0 2 * * * cd /path/to/Address-Geocoding && /usr/local/bin/docker-compose up --abort-on-container-exit >> /path/to/Address-Geocoding/logs/cron.log 2>&1
```

**Flags explained**:
- `--abort-on-container-exit`: Stop Docker Compose when container finishes
- `>> /path/to/Address-Geocoding/logs/cron.log`: Log cron output
- `2>&1`: Capture both stdout and stderr

#### Option 2: Wrapper Script (More Robust)

Create `run-geocoder.sh`:

```bash
#!/bin/bash

# Address Geocoding - Cron Wrapper Script
# Runs Docker container and logs execution

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

# Timestamp for logging
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

echo "[$TIMESTAMP] Starting geocoding job..."

# Run Docker Compose
docker-compose up --abort-on-container-exit

EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    echo "[$TIMESTAMP] Geocoding job completed successfully"
else
    echo "[$TIMESTAMP] ERROR: Geocoding job failed with exit code $EXIT_CODE"
fi

# Clean up old logs (older than 84 days)
find "$SCRIPT_DIR/logs" -name "*.log" -mtime +84 -delete

exit $EXIT_CODE
```

Make executable:
```bash
chmod +x run-geocoder.sh
```

Add to crontab:
```bash
# Run address geocoding daily at 2:00 AM
0 2 * * * /path/to/Address-Geocoding/run-geocoder.sh >> /path/to/Address-Geocoding/logs/cron.log 2>&1
```

#### Manual Execution

```bash
# Navigate to project directory
cd /path/to/Address-Geocoding

# Run via Docker Compose
docker-compose up

# OR run via wrapper script
./run-geocoder.sh
```

---

## Detailed Improvements

### Phase 1: Library Migration & Batch Processing

**Priority**: HIGHEST
**Estimated Effort**: 1 week
**Impact**: 99% reduction in API calls

#### Changes Required

1. **Update requirements.txt**:
   ```txt
   # OLD
   geocodio

   # NEW
   geocodio-library-python>=2.0.0
   ```

2. **Update imports** in `GeocodeICAddresses.py`:
   ```python
   # OLD
   from geocodio import GeocodioClient

   # NEW
   from geocodio import Geocodio
   from geocodio.exceptions import AuthenticationError, InvalidRequestError
   ```

3. **Update client initialization**:
   ```python
   # OLD
   client = GeocodioClient(ic_cfg.Geocodeio["apikey"])

   # NEW
   client = Geocodio(GEOCODIO_API_KEY)  # From environment variable
   ```

4. **Implement batch geocoding function**:
   ```python
   def batch_geocode_addresses(addresses_df, client):
       """
       Geocode multiple addresses in a single API call.

       The new geocodio-library-python supports batch geocoding
       of up to 10,000 addresses at once.

       Args:
           addresses_df: DataFrame with ADDRESSID and ADDRESSLINE1 columns
           client: Geocodio client instance

       Returns:
           dict: API response containing results for all addresses

       Raises:
           AuthenticationError: If API key is invalid
           InvalidRequestError: If request is malformed
       """
       try:
           # Extract address list
           address_list = addresses_df['ADDRESSLINE1'].tolist()

           logger.info(f"Batch geocoding {len(address_list)} addresses...")

           # Single API call for all addresses
           results = client.geocode(
               address_list,
               fields=API_FIELDS.split(',')
           )

           logger.info(f"Successfully geocoded {len(address_list)} addresses")
           return results

       except AuthenticationError as e:
           logger.error(f"Geocodio API authentication failed: {e}")
           logger.error("Check GEOCODIO_API_KEY environment variable")
           raise

       except InvalidRequestError as e:
           logger.error(f"Invalid geocoding request: {e}")
           raise

       except Exception as e:
           logger.error(f"Unexpected error during batch geocoding: {e}")
           raise
   ```

#### API Response Format

The new library returns results in a slightly different format:

**OLD (geocodio)**:
```python
location = client.geocode("123 Main St")
lat = dict(location)["results"][0]["location"]["lat"]
```

**NEW (geocodio-library-python)**:
```python
results = client.geocode(["123 Main St"])
lat = results.results[0].location.lat  # Object notation
# OR
lat = results['results'][0]['location']['lat']  # Dict notation
```

Both notations are supported by the new library for backward compatibility.

#### Benefits

- **Performance**: 500 API calls → 1 API call
- **Time**: ~5-10 minutes → ~30-60 seconds
- **Cost**: 99% reduction in API usage charges
- **Reliability**: Single point of failure vs 500
- **Error Handling**: One try/except block vs 500

---

### Phase 2: Environment-Based Configuration

**Priority**: CRITICAL (for Docker)
**Estimated Effort**: 3 days
**Impact**: Security & portability

#### Replace config/IC_config.py

**Current approach** (INSECURE):
```python
# config/IC_config.py
IC_DB_Credentials = {
    "user": "fullerb",
    "passwd": "M0unta1n"  # ← Plaintext password in code!
}
```

**New approach** (SECURE):
```python
# GeocodeICAddresses.py
import os

# ============================================
# Configuration from Environment Variables
# ============================================

# Infinite Campus Database
IC_DB_USER = os.environ.get('IC_DB_USER')
IC_DB_PASSWORD = os.environ.get('IC_DB_PASSWORD')
IC_DB_HOST = os.getenv('IC_DB_HOST', 'icampus.mapleton.us')
IC_DB_PORT = os.getenv('IC_DB_PORT', '7771')
IC_DB_NAME = os.getenv('IC_DB_NAME', 'mapleton')

# Snowflake
SNOWFLAKE_USER = os.environ.get('SNOWFLAKE_USER')
SNOWFLAKE_PASSWORD = os.environ.get('SNOWFLAKE_PASSWORD')
SNOWFLAKE_ACCOUNT = os.getenv('SNOWFLAKE_ACCOUNT', 'dv56305.west-us-2.azure')
SNOWFLAKE_WAREHOUSE = os.getenv('SNOWFLAKE_WAREHOUSE', 'dataloadingonly')
SNOWFLAKE_DATABASE = os.getenv('SNOWFLAKE_DATABASE', 'MPS_Warehouse')
SNOWFLAKE_SCHEMA = os.getenv('SNOWFLAKE_SCHEMA', 'datalake')

# Geocodio API
GEOCODIO_API_KEY = os.environ.get('GEOCODIO_API_KEY')

# Application Settings
BATCH_SIZE = int(os.getenv('BATCH_SIZE', '500'))
ACCURACY_THRESHOLD = float(os.getenv('ACCURACY_THRESHOLD', '0.8'))
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
API_FIELDS = os.getenv('API_FIELDS', 'school,zip4,census2023')

# ============================================
# Configuration Validation
# ============================================

def validate_config():
    """
    Validate that all required environment variables are set.

    This function should be called at startup to ensure the
    application is properly configured before attempting to
    connect to databases or APIs.

    Raises:
        EnvironmentError: If any required variables are missing
    """
    required_vars = [
        'IC_DB_USER',
        'IC_DB_PASSWORD',
        'SNOWFLAKE_USER',
        'SNOWFLAKE_PASSWORD',
        'GEOCODIO_API_KEY'
    ]

    missing = [var for var in required_vars if not os.environ.get(var)]

    if missing:
        error_msg = f"Missing required environment variables: {', '.join(missing)}"
        logger.error(error_msg)
        logger.error("Please check your .env file or environment configuration")
        raise EnvironmentError(error_msg)

    logger.info("Configuration validated successfully")
    logger.info(f"Batch size: {BATCH_SIZE}")
    logger.info(f"Accuracy threshold: {ACCURACY_THRESHOLD}")
```

#### Migration Path

1. **Keep old config** (temporarily):
   ```python
   # Support both old and new configuration during transition
   IC_DB_USER = os.environ.get('IC_DB_USER') or ic_cfg.IC_DB_Credentials.get('user')
   ```

2. **Test with .env file**
3. **Remove config/IC_config.py** after verification

#### Benefits

- **Security**: No credentials in code or git
- **Flexibility**: Different credentials per environment
- **Docker-Ready**: Same image works everywhere
- **Auditable**: Configuration changes tracked separately from code

---

### Phase 3: Function Decomposition

**Priority**: HIGH
**Estimated Effort**: 1 week
**Impact**: Maintainability & testability

#### Current Structure

```
main() - 158 lines
  ├── Initialize connections
  ├── Query Snowflake
  ├── Loop through 500 addresses
  │   ├── Geocode address
  │   ├── Parse results
  │   ├── Insert to Snowflake
  │   └── Update IC database
  └── Close connections
```

#### Proposed Structure

```
main() - 30 lines (orchestration only)
  │
  ├── validate_config() - 15 lines
  │   └── Check environment variables
  │
  ├── create_snowflake_connection() - 20 lines
  │   └── Connect with retry logic
  │
  ├── create_ic_connection() - 20 lines
  │   └── Connect with retry logic
  │
  ├── fetch_addresses_to_geocode() - 15 lines
  │   └── Query Snowflake for addresses
  │
  ├── batch_geocode_addresses() - 25 lines
  │   └── Call Geocodio API (batch)
  │
  ├── parse_geocode_results() - 30 lines
  │   └── Convert JSON to DataFrame
  │
  ├── enrich_with_district_ids() - 20 lines
  │   └── Match LEA codes to district IDs
  │
  ├── bulk_insert_snowflake() - 20 lines
  │   └── Log results to Snowflake
  │
  └── bulk_update_ic() - 25 lines
      └── Update IC database
```

#### Example: Connection Function with Retry Logic

```python
import time
import snowflake.connector

def create_snowflake_connection():
    """
    Create Snowflake connection with retry logic.

    Containers may start before network is fully ready,
    so we retry connection attempts with exponential backoff.

    Returns:
        snowflake.connector.SnowflakeConnection: Active connection

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

        except Exception as e:
            logger.warning(f"Connection attempt {attempt} failed: {e}")

            if attempt == max_retries:
                logger.error("Failed to connect to Snowflake after all retries")
                raise

            logger.info(f"Retrying in {retry_delay} seconds...")
            time.sleep(retry_delay)
            retry_delay *= 2  # Exponential backoff
```

#### Example: Parsing Function

```python
def parse_geocode_results(api_results, addresses_df):
    """
    Parse Geocodio API results into structured DataFrame.

    Extracts all needed fields: lat, lng, accuracy, census data,
    school district, etc.

    Args:
        api_results: Raw results from Geocodio API
        addresses_df: Original DataFrame with ADDRESSID

    Returns:
        pd.DataFrame: Parsed results with all geocoding fields
    """
    parsed_rows = []

    for idx, result_set in enumerate(api_results.get('results', [])):
        if not result_set.get('results'):
            # No results for this address
            parsed_rows.append({
                'ADDRESSID': addresses_df.iloc[idx]['ADDRESSID'],
                'ADDRESSLINE1': addresses_df.iloc[idx]['ADDRESSLINE1'],
                'success': False
            })
            continue

        # Get first (best) result
        result = result_set['results'][0]

        # Extract fields
        parsed_rows.append({
            'ADDRESSID': addresses_df.iloc[idx]['ADDRESSID'],
            'ADDRESSLINE1': addresses_df.iloc[idx]['ADDRESSLINE1'],
            'success': True,
            'latitude': result['location']['lat'],
            'longitude': result['location']['lng'],
            'accuracy': result['accuracy'],
            'accuracy_type': result['accuracy_type'],
            'source': result['source'],
            'block_code': result['fields']['census']['2023']['block_code'],
            'tract_code': result['fields']['census']['2023']['tract_code'],
            'county_fips': result['fields']['census']['2023']['county_fips'],
            'lea_code': result['fields'].get('school_districts', {}).get('unified', {}).get('lea_code', '0000'),
            'district_name': result['fields'].get('school_districts', {}).get('unified', {}).get('name', 'none')
        })

    return pd.DataFrame(parsed_rows)
```

#### Benefits for Junior Developers

- **Easy to Understand**: Each function fits on one screen
- **Easy to Test**: Can test each function independently
- **Easy to Debug**: Clear flow, easy to add breakpoints
- **Easy to Modify**: Change one function without affecting others
- **Self-Documenting**: Function names explain what they do

---

### Phase 4: Enhanced Error Handling & Logging

**Priority**: HIGH
**Estimated Effort**: 3 days
**Impact**: Reliability & troubleshooting

#### Setup Logging System

```python
import logging
import sys
import os

def setup_logging():
    """
    Configure logging for both console (Docker) and file output.

    Docker best practice: log to stdout for 'docker logs' command.
    Also write to file for persistent debugging.
    """
    log_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    log_level = os.getenv('LOG_LEVEL', 'INFO')

    # Root logger
    logger = logging.getLogger()
    logger.setLevel(log_level)

    # Console handler (for Docker logs)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter(log_format))
    logger.addHandler(console_handler)

    # File handler (for persistent logs)
    file_handler = logging.FileHandler('/app/logs/geocoding.log')
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(log_format))
    logger.addHandler(file_handler)

    return logger

# Initialize at module level
logger = setup_logging()
```

#### Replace Print Statements

**Before**:
```python
print("Error geocoding address: " + row['ADDRESSLINE1'])
```

**After**:
```python
logger.error(f"Failed to geocode address: {row['ADDRESSLINE1']}")
```

#### Specific Exception Handling

**Before** (BAD):
```python
try:
    location = client.geocode(row['ADDRESSLINE1'])
except:
    print("Error")  # What error? Why? No information!
```

**After** (GOOD):
```python
from geocodio.exceptions import AuthenticationError, InvalidRequestError
import snowflake.connector.errors as sf_errors

try:
    results = batch_geocode_addresses(addresses_df, client)

except AuthenticationError as e:
    logger.error("Geocodio API authentication failed")
    logger.error(f"Error details: {e}")
    logger.error("Action required: Check GEOCODIO_API_KEY in .env file")
    sys.exit(1)

except InvalidRequestError as e:
    logger.error(f"Invalid geocoding request: {e}")
    logger.error("Action required: Check address data format")
    sys.exit(1)

except sf_errors.ProgrammingError as e:
    logger.error(f"Snowflake query error: {e}")
    logger.error("Action required: Check database permissions and SQL syntax")
    sys.exit(1)

except Exception as e:
    logger.error(f"Unexpected error: {e}", exc_info=True)
    sys.exit(1)
```

#### Exit Codes (Critical for Cron)

```python
def main():
    """Main execution function."""
    try:
        validate_config()
        # ... process addresses ...
        logger.info("Geocoding completed successfully")
        return 0  # Success

    except EnvironmentError as e:
        logger.error(f"Configuration error: {e}")
        return 1  # Configuration error

    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        return 2  # Runtime error

if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)
```

**Cron job monitoring**:
```bash
# Check exit code in wrapper script
if [ $EXIT_CODE -ne 0 ]; then
    # Send alert email
    echo "Geocoding failed!" | mail -s "Alert: Geocoding Job Failed" admin@mapleton.us
fi
```

#### Benefits

- **Troubleshooting**: Clear error messages tell you exactly what failed
- **Monitoring**: Exit codes indicate success/failure
- **Debugging**: Stack traces in log files
- **Professionalism**: Structured logs vs random print statements

---

### Phase 5: Database Optimization

**Priority**: MEDIUM
**Estimated Effort**: 4 days
**Impact**: Performance

#### Current Issues

1. **500 individual Snowflake inserts**:
   ```python
   for index, row in df.iterrows():
       cs.execute(snow_insert_query)  # ← 500 queries!
   ```

2. **500 individual IC updates with commits**:
   ```python
   for index, row in df.iterrows():
       cupdate.execute(sqlUpdate, args)
       cupdate.commit()  # ← 500 commits!
   ```

#### Optimized Approach

**1. Bulk Snowflake Insert**:

```python
def bulk_insert_snowflake(results_df, snowflake_cursor):
    """
    Insert all geocoding results to Snowflake in a single operation.

    Args:
        results_df: DataFrame with parsed geocoding results
        snowflake_cursor: Active Snowflake cursor

    Returns:
        int: Number of rows inserted
    """
    if len(results_df) == 0:
        logger.warning("No results to insert into Snowflake")
        return 0

    try:
        # Prepare JSON data
        insert_data = []
        for _, row in results_df.iterrows():
            json_data = json.dumps(row.to_dict())
            json_data = json_data.replace("'", "''")  # Escape quotes
            insert_data.append((row['ADDRESSID'], json_data))

        # Bulk insert using executemany
        insert_query = """
            INSERT INTO LOGGING_GEOCODIOAPI_ADDRESSLOOKUPS
            (IC_ADDRESSID, GEOCODIO_JSON)
            SELECT %s, PARSE_JSON(%s)
        """

        snowflake_cursor.executemany(insert_query, insert_data)

        logger.info(f"Successfully inserted {len(insert_data)} records to Snowflake")
        return len(insert_data)

    except Exception as e:
        logger.error(f"Error during Snowflake bulk insert: {e}")
        raise
```

**2. Bulk IC Update**:

```python
def bulk_update_ic(results_df, ic_cursor):
    """
    Update Infinite Campus addresses in batch.

    Only updates addresses that meet accuracy threshold.

    Args:
        results_df: DataFrame with parsed geocoding results
        ic_cursor: Active IC database cursor

    Returns:
        int: Number of addresses updated
    """
    # Filter by accuracy threshold
    updates_df = results_df[
        (results_df['success'] == True) &
        (results_df['accuracy'] >= ACCURACY_THRESHOLD)
    ]

    if len(updates_df) == 0:
        logger.warning("No addresses meet accuracy threshold for update")
        return 0

    try:
        # Prepare update data
        update_data = []
        for _, row in updates_df.iterrows():
            if row.get('DISTRICTID', 0) == 0:
                # No district ID - partial update
                update_data.append((
                    row['latitude'],
                    row['longitude'],
                    row['tract_code'],
                    row['block_code'],
                    row['county_fips'],
                    row['ADDRESSID']
                ))
            else:
                # Full update with district ID
                update_data.append((
                    row['latitude'],
                    row['longitude'],
                    row['tract_code'],
                    row['block_code'],
                    row['county_fips'],
                    row['DISTRICTID'],
                    row['ADDRESSID']
                ))

        # Bulk update using executemany
        update_query = """
            UPDATE ADDRESS
            SET latitude = ?,
                longitude = ?,
                tract = ?,
                block = ?,
                location_code = ?,
                districtID = ?
            WHERE addressid = ?
        """

        ic_cursor.executemany(update_query, update_data)

        logger.info(f"Successfully updated {len(update_data)} addresses in IC")
        return len(update_data)

    except Exception as e:
        logger.error(f"Error during IC bulk update: {e}")
        raise
```

**3. Context Managers for Automatic Cleanup**:

```python
def main():
    """Main execution with proper resource management."""
    try:
        # Snowflake work
        with create_snowflake_connection() as sf_conn:
            with sf_conn.cursor() as sf_cursor:
                addresses_df = fetch_addresses_to_geocode(sf_cursor)
                results_df = process_addresses(addresses_df)
                bulk_insert_snowflake(results_df, sf_cursor)
                sf_conn.commit()  # Single commit
        # Connection auto-closed

        # IC work
        with create_ic_connection() as ic_conn:
            with ic_conn.cursor() as ic_cursor:
                updated_count = bulk_update_ic(results_df, ic_cursor)
                ic_conn.commit()  # Single commit
        # Connection auto-closed

        return 0

    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        return 1
```

#### Performance Impact

| Operation | Current | Optimized | Improvement |
|-----------|---------|-----------|-------------|
| Snowflake inserts | 500 queries | 1 query | 500x faster |
| IC updates | 500 commits | 1 commit | 500x faster |
| Total DB ops | 1000+ | 4-5 | 200x+ reduction |
| Execution time (DB) | ~3-5 min | ~5-10 sec | 20-30x faster |

---

### Phase 6: Summary Reporting

**Priority**: LOW
**Estimated Effort**: 1 day
**Impact**: Observability

```python
def log_summary(total_addresses, geocoded_count, updated_count, failed_count, start_time):
    """
    Log comprehensive summary of geocoding run.

    Args:
        total_addresses: Total addresses attempted
        geocoded_count: Successfully geocoded
        updated_count: Updated in IC database
        failed_count: Failed to geocode
        start_time: Timestamp when processing started
    """
    duration = time.time() - start_time
    success_rate = (geocoded_count / total_addresses * 100) if total_addresses > 0 else 0

    logger.info("=" * 60)
    logger.info("GEOCODING SUMMARY")
    logger.info("=" * 60)
    logger.info(f"Total addresses processed:    {total_addresses}")
    logger.info(f"Successfully geocoded:        {geocoded_count}")
    logger.info(f"IC database updated:          {updated_count}")
    logger.info(f"Failed/skipped:               {failed_count}")
    logger.info(f"Success rate:                 {success_rate:.1f}%")
    logger.info(f"Execution time:               {duration:.1f} seconds")
    logger.info(f"Average time per address:     {(duration/total_addresses):.2f} seconds")
    logger.info("=" * 60)
```

**Sample output**:
```
2025-11-10 02:00:45 - INFO - ============================================================
2025-11-10 02:00:45 - INFO - GEOCODING SUMMARY
2025-11-10 02:00:45 - INFO - ============================================================
2025-11-10 02:00:45 - INFO - Total addresses processed:    500
2025-11-10 02:00:45 - INFO - Successfully geocoded:        487
2025-11-10 02:00:45 - INFO - IC database updated:          476
2025-11-10 02:00:45 - INFO - Failed/skipped:               13
2025-11-10 02:00:45 - INFO - Success rate:                 97.4%
2025-11-10 02:00:45 - INFO - Execution time:               45.3 seconds
2025-11-10 02:00:45 - INFO - Average time per address:     0.09 seconds
2025-11-10 02:00:45 - INFO - ============================================================
```

---

## Implementation Plan

### Timeline Overview

**Total Duration**: 5 weeks
**Effort**: 1 developer, part-time (~50% allocation)

### Sprint Breakdown

#### Sprint 1: Core Refactoring (Week 1)

**Goals**: Working refactored code (not Dockerized yet)

**Tasks**:
- [ ] Create `requirements.txt` with all dependencies
- [ ] Add environment variable configuration to code
- [ ] Create `.env.example` template
- [ ] Migrate from `geocodio` to `geocodio-library-python`
- [ ] Implement `batch_geocode_addresses()` function
- [ ] Add structured logging (replace print statements)
- [ ] Test locally with `.env` file

**Deliverable**: Refactored code that runs on host machine

**Testing**:
- Run with 10 addresses
- Verify batch API call works
- Check logs are properly formatted
- Verify Snowflake and IC updates work

---

#### Sprint 2: Containerization (Week 2)

**Goals**: Working Docker container

**Tasks**:
- [ ] Create `Dockerfile`
- [ ] Create `docker-compose.yml`
- [ ] Configure log retention (12 weeks)
- [ ] Create `.dockerignore`
- [ ] Build Docker image locally
- [ ] Test container with `.env` file
- [ ] Verify logs appear in `docker logs`
- [ ] Verify log files persist to `./logs/` directory

**Deliverable**: Docker container that processes addresses

**Testing**:
- Build image: `docker-compose build`
- Run container: `docker-compose up`
- Check logs: `docker-compose logs`
- Verify log files: `ls -l logs/`
- Test with 10 addresses, then 100, then 500

---

#### Sprint 3: Function Decomposition (Week 3)

**Goals**: Clean, maintainable code structure

**Tasks**:
- [ ] Create `validate_config()` function
- [ ] Create `create_snowflake_connection()` with retry logic
- [ ] Create `create_ic_connection()` with retry logic
- [ ] Create `fetch_addresses_to_geocode()` function
- [ ] Create `parse_geocode_results()` function
- [ ] Create `enrich_with_district_ids()` function
- [ ] Add comprehensive docstrings to all functions
- [ ] Update `main()` to orchestrate functions

**Deliverable**: Well-structured, documented code

**Testing**:
- Unit test each function independently (if possible)
- Integration test full flow
- Verify error handling works correctly

---

#### Sprint 4: Database Optimization (Week 4)

**Goals**: Fast, efficient database operations

**Tasks**:
- [ ] Implement `bulk_insert_snowflake()` function
- [ ] Implement `bulk_update_ic()` function
- [ ] Replace all individual DB operations with batch operations
- [ ] Add connection pooling if beneficial
- [ ] Use context managers (`with` statements) throughout
- [ ] Performance testing and benchmarking

**Deliverable**: Optimized database performance

**Testing**:
- Benchmark current vs optimized (time execution)
- Verify all 500 addresses processed correctly
- Check Snowflake logging table
- Check IC address updates

---

#### Sprint 5: Production Readiness (Week 5)

**Goals**: Production-ready, documented system

**Tasks**:
- [ ] Add summary reporting to `main()`
- [ ] Create cron wrapper script (`run-geocoder.sh`)
- [ ] Configure cron job on server
- [ ] Update `CLAUDE.md` with new architecture
- [ ] Create/update `README.md` with Docker usage
- [ ] Create `config/README.md` with setup instructions
- [ ] Test full production deployment
- [ ] Document troubleshooting procedures

**Deliverable**: Production-ready application

**Testing**:
- Test cron execution
- Test manual execution
- Verify log rotation works
- End-to-end production test

---

### Risk Management

#### Rollback Strategy

1. **Keep old version**:
   ```bash
   cp GeocodeICAddresses.py GeocodeICAddresses_v1_backup.py
   ```

2. **Tag Docker images**:
   ```bash
   docker tag address-geocoder:latest address-geocoder:v1
   docker tag address-geocoder:latest address-geocoder:v2
   ```

3. **Quick rollback**:
   ```bash
   # Restore old code
   cp GeocodeICAddresses_v1_backup.py GeocodeICAddresses.py

   # OR use old Docker image
   docker run address-geocoder:v1
   ```

#### Mitigation Checklist

**Before Implementation**:
- [ ] Backup production database
- [ ] Document current system behavior
- [ ] Create test environment
- [ ] Verify test environment mirrors production

**During Implementation**:
- [ ] Implement one sprint at a time
- [ ] Test thoroughly after each sprint
- [ ] Keep old code available
- [ ] Document any issues encountered

**Before Production**:
- [ ] Full end-to-end testing
- [ ] Verify cron job configuration
- [ ] Test rollback procedure
- [ ] Brief stakeholders on changes

---

## Testing Strategy

### Unit Testing (Optional but Recommended)

```python
# test_geocoding.py
import pytest
from GeocodeICAddresses import parse_geocode_results, enrich_with_district_ids

def test_parse_geocode_results():
    """Test parsing of API results."""
    # Mock API response
    sample_results = {
        'results': [{
            'results': [{
                'location': {'lat': 38.9, 'lng': -77.0},
                'accuracy': 0.95,
                'accuracy_type': 'rooftop',
                'source': 'TIGER/Line',
                'fields': {
                    'census': {
                        '2023': {
                            'block_code': '1234',
                            'tract_code': '5678',
                            'county_fips': '12345'
                        }
                    },
                    'school_districts': {
                        'unified': {
                            'lea_code': '0800123',
                            'name': 'Mapleton Public Schools'
                        }
                    }
                }
            }]
        }]
    }

    # Mock addresses DataFrame
    addresses = pd.DataFrame({
        'ADDRESSID': [123],
        'ADDRESSLINE1': ['123 Main St']
    })

    # Test
    result_df = parse_geocode_results(sample_results, addresses)

    # Assertions
    assert len(result_df) == 1
    assert result_df.iloc[0]['latitude'] == 38.9
    assert result_df.iloc[0]['accuracy'] == 0.95
    assert result_df.iloc[0]['lea_code'] == '0800123'
```

### Integration Testing

#### Test 1: Small Batch (10 addresses)

```bash
# Edit .env
BATCH_SIZE=10

# Run container
docker-compose up

# Verify results
docker-compose logs | grep "Total addresses processed: 10"
```

**Expected**:
- Container exits with code 0
- Logs show "Successfully geocoded X addresses"
- Snowflake table has 10 new rows
- IC database updated for addresses with accuracy >= 0.8

---

#### Test 2: Error Handling (Invalid API Key)

```bash
# Edit .env
GEOCODIO_API_KEY=invalid_key_test

# Run container
docker-compose up

# Check exit code
echo $?  # Should be 1 (error)
```

**Expected**:
- Container exits with code 1
- Logs show "Geocodio API authentication failed"
- Logs show "Check GEOCODIO_API_KEY in .env file"
- No database changes

---

#### Test 3: Database Connectivity

```bash
# Edit .env - use invalid Snowflake password
SNOWFLAKE_PASSWORD=wrong_password

# Run container
docker-compose up

# Check logs
docker-compose logs | grep "Snowflake"
```

**Expected**:
- Container exits with code 1
- Logs show connection error
- Clear error message about credentials

---

#### Test 4: Full Production Batch (500 addresses)

```bash
# Edit .env
BATCH_SIZE=500

# Run container
docker-compose up

# Monitor logs
docker-compose logs -f
```

**Expected**:
- Container completes successfully
- Execution time < 2 minutes
- Summary shows success rate
- Snowflake has 500 new log entries
- IC database updated appropriately

---

### Production Testing Checklist

**Pre-Deployment**:
- [ ] Test with 10 addresses
- [ ] Test with 100 addresses
- [ ] Test with 500 addresses
- [ ] Test with invalid API key
- [ ] Test with invalid database credentials
- [ ] Test with missing environment variables
- [ ] Verify log file rotation
- [ ] Verify Docker log retention

**Post-Deployment**:
- [ ] Monitor first 3 cron runs
- [ ] Verify data quality in IC database
- [ ] Check Snowflake logging
- [ ] Review log files for errors
- [ ] Confirm execution time improvements
- [ ] Validate success rates

---

## Deployment Guide

### Initial Server Setup

#### 1. Install Docker

```bash
# Install Docker (Ubuntu/Debian)
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh

# Install Docker Compose
sudo curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
sudo chmod +x /usr/local/bin/docker-compose

# Verify installation
docker --version
docker-compose --version

# Add current user to docker group (optional, avoids sudo)
sudo usermod -aG docker $USER
# Log out and back in for group change to take effect
```

---

#### 2. Clone Repository

```bash
# Clone to server
cd /opt  # Or your preferred location
git clone <repository-url> Address-Geocoding
cd Address-Geocoding
```

---

#### 3. Configure Environment

```bash
# Copy template
cp .env.example .env

# Edit with actual credentials
nano .env  # or vi .env

# Set proper permissions (important!)
chmod 600 .env

# Create logs directory
mkdir -p logs
```

---

#### 4. Build Docker Image

```bash
# Build image
docker-compose build

# Verify image created
docker images | grep address-geocoder
```

---

#### 5. Test Run

```bash
# Test with small batch first
# Edit .env and set BATCH_SIZE=10
nano .env

# Run container
docker-compose up

# Verify success
echo $?  # Should be 0

# Check logs
cat logs/geocoding.log
```

---

### Cron Configuration

#### Option 1: Simple Cron Entry

```bash
# Edit crontab
crontab -e

# Add this line (run daily at 2:00 AM)
0 2 * * * cd /opt/Address-Geocoding && /usr/local/bin/docker-compose up --abort-on-container-exit >> /opt/Address-Geocoding/logs/cron.log 2>&1
```

---

#### Option 2: Wrapper Script (Recommended)

Create `/opt/Address-Geocoding/run-geocoder.sh`:

```bash
#!/bin/bash

#################################################
# Address Geocoding - Cron Wrapper Script
#################################################

# Change to project directory
SCRIPT_DIR="/opt/Address-Geocoding"
cd "$SCRIPT_DIR"

# Timestamp
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

echo "[$TIMESTAMP] Starting geocoding job..."

# Run Docker Compose
/usr/local/bin/docker-compose up --abort-on-container-exit

# Capture exit code
EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    echo "[$TIMESTAMP] Geocoding job completed successfully"
else
    echo "[$TIMESTAMP] ERROR: Geocoding job failed with exit code $EXIT_CODE"

    # Optional: Send email alert
    # echo "Geocoding job failed. Check logs at $SCRIPT_DIR/logs/" | \
    #   mail -s "ALERT: Geocoding Failed" admin@mapleton.us
fi

# Clean up old logs (older than 84 days)
find "$SCRIPT_DIR/logs" -name "*.log" -mtime +84 -delete

# Clean up old Docker logs if needed
docker system prune -f --filter "until=84d"

exit $EXIT_CODE
```

Make executable:
```bash
chmod +x /opt/Address-Geocoding/run-geocoder.sh
```

Add to crontab:
```bash
crontab -e

# Run daily at 2:00 AM
0 2 * * * /opt/Address-Geocoding/run-geocoder.sh >> /opt/Address-Geocoding/logs/cron.log 2>&1
```

---

### Manual Execution

```bash
# Navigate to directory
cd /opt/Address-Geocoding

# Option 1: Docker Compose
docker-compose up

# Option 2: Wrapper script
./run-geocoder.sh

# View real-time logs
docker-compose logs -f

# View log file
tail -f logs/geocoding.log

# View recent cron output
tail -n 100 logs/cron.log
```

---

### Monitoring & Troubleshooting

#### View Container Status

```bash
# List running containers
docker ps

# List all containers (including exited)
docker ps -a | grep address-geocoder
```

---

#### View Logs

```bash
# Docker logs
docker logs address-geocoder

# Application logs
cat logs/geocoding.log

# Cron logs
cat logs/cron.log

# Recent errors only
grep ERROR logs/geocoding.log
```

---

#### Common Issues & Solutions

**Issue**: Container exits immediately with "Missing required environment variables"

**Solution**:
```bash
# Check .env file exists
ls -la .env

# Verify required variables are set
grep "GEOCODIO_API_KEY" .env
grep "IC_DB_USER" .env

# Check for typos in .env
cat .env
```

---

**Issue**: "Authentication failed" errors

**Solution**:
```bash
# Verify credentials are correct
# Test Snowflake connection manually
docker run --env-file .env -it --rm python:3.11-slim bash
pip install snowflake-connector-python
python
>>> import snowflake.connector
>>> conn = snowflake.connector.connect(
...     user='xxx',
...     password='xxx',
...     account='xxx'
... )
```

---

**Issue**: Cron job not running

**Solution**:
```bash
# Check cron service is running
sudo systemctl status cron

# Verify crontab entry
crontab -l

# Check cron logs
grep CRON /var/log/syslog

# Test wrapper script manually
/opt/Address-Geocoding/run-geocoder.sh
```

---

**Issue**: Out of disk space

**Solution**:
```bash
# Check disk usage
df -h

# Clean up old Docker images
docker system prune -a

# Check log sizes
du -sh /opt/Address-Geocoding/logs/*

# Clean old logs
find /opt/Address-Geocoding/logs -name "*.log" -mtime +84 -delete
```

---

## Maintenance & Support

### Routine Maintenance

#### Daily (Automated via Cron)
- Geocoding job runs at 2:00 AM
- Automatic log rotation (Docker handles this)

#### Weekly
- Review cron.log for any failures
- Check disk space usage
- Review geocoding success rates

```bash
# Weekly check script (run manually or via cron)
cd /opt/Address-Geocoding

# Check success rate from recent logs
grep "Success rate" logs/geocoding.log | tail -n 7

# Check disk usage
du -sh logs/

# Check Docker image size
docker images | grep address-geocoder
```

---

#### Monthly
- Review overall system performance
- Check for updates to Python packages
- Verify database connections are healthy

---

### Updating the Application

#### Code Changes

```bash
cd /opt/Address-Geocoding

# Pull latest code
git pull origin main

# Rebuild Docker image
docker-compose build

# Test with small batch
# (Edit .env: BATCH_SIZE=10)
docker-compose up

# If successful, restore BATCH_SIZE=500
```

---

#### Dependency Updates

```bash
# Update requirements.txt
# Then rebuild image
docker-compose build --no-cache

# Test thoroughly before production use
```

---

### Credential Rotation

```bash
# Edit .env file
nano .env

# Update password(s)
SNOWFLAKE_PASSWORD=new_password_here

# Save and exit

# Test with small batch
docker-compose up

# If successful, no rebuild needed (env vars only)
```

---

### Backup & Recovery

#### Backup Strategy

```bash
# Backup .env file (encrypted)
tar -czf env-backup-$(date +%Y%m%d).tar.gz .env
openssl enc -aes-256-cbc -salt -in env-backup-*.tar.gz -out env-backup-*.tar.gz.enc
rm env-backup-*.tar.gz  # Keep only encrypted version

# Backup logs (if needed)
tar -czf logs-backup-$(date +%Y%m%d).tar.gz logs/
```

---

#### Recovery

```bash
# Restore .env
openssl enc -aes-256-cbc -d -in env-backup-YYYYMMDD.tar.gz.enc -out env-backup.tar.gz
tar -xzf env-backup.tar.gz

# Rebuild and run
docker-compose build
docker-compose up
```

---

### Support Contact

**For Issues**:
1. Check `logs/geocoding.log` for error details
2. Check `logs/cron.log` for cron execution issues
3. Review this document's troubleshooting section
4. Contact: IT Department / Database Team

**For Enhancements**:
- Submit feature request via issue tracker
- Consult with development team

---

## Appendix

### Complete File Listing

**requirements.txt**:
```txt
# Geocoding library
geocodio-library-python>=2.0.0

# Database connectivity
pyodbc>=5.0.0
snowflake-connector-python>=3.0.0

# Data processing
pandas>=2.0.0
sqlalchemy>=2.0.0
```

---

**Updated .gitignore** (ensure these lines exist):
```
# Secrets and configuration
.env
.env.*
config/IC_config.py

# Logs
logs/
*.log

# Docker
.dockerignore

# Python
__pycache__/
*.pyc
*.pyo
```

---

### Key Metrics to Track

| Metric | Current | Target | How to Measure |
|--------|---------|--------|----------------|
| Execution Time | 5-10 min | <2 min | Check logs: "Execution time: X seconds" |
| API Calls | 500 | 1 | Review Geocodio dashboard |
| Success Rate | Unknown | >95% | Check logs: "Success rate: X%" |
| Database Ops | 1000+ | <10 | Code inspection + profiling |
| Container Size | N/A | <500MB | `docker images` |
| Log Storage | N/A | <1GB | `du -sh logs/` |

---

### Glossary

- **Batch Geocoding**: Geocoding multiple addresses in a single API call
- **Container**: Isolated environment that packages application with dependencies
- **Context Manager**: Python `with` statement for automatic resource cleanup
- **Cron**: Linux scheduler for running jobs at specified times
- **Docker Compose**: Tool for defining multi-container Docker applications
- **Environment Variable**: Configuration value stored outside code
- **Exit Code**: Number returned by program indicating success (0) or failure (non-zero)
- **LEA Code**: Local Education Agency code (school district identifier)
- **Logging Level**: Severity of log message (DEBUG, INFO, WARNING, ERROR)
- **Volume Mount**: Making host directory available inside container

---

## Summary

This plan transforms the current geocoding application from a functional but maintenance-heavy script into a modern, containerized, and easily maintainable system.

**Key Improvements**:
1. **99% reduction in API calls** (500 → 1) via batch processing
2. **Secure configuration** via environment variables
3. **10x performance improvement** via database optimization
4. **Easy deployment** via Docker containerization
5. **Better reliability** via error handling and logging
6. **Junior developer friendly** via function decomposition and documentation

**Implementation Timeline**: 5 weeks

**Deployment Method**: Docker container triggered by daily cron job, with manual execution option

**Maintenance Overhead**: Minimal - automated logs, clear error messages, simple troubleshooting

---

**Questions or clarifications needed before proceeding?**
