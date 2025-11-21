"""
Test script for US Census Geocoder API
Demonstrates lookup of address data including:
- Latitude and longitude
- Census tract and block
- School districts
"""

import requests
import json

# Sample address from test.py
address = "638 E 13th Ave, Denver, CO"

# Census Geocoder API endpoint
# Using 'geographies' returntype to get all geographic data
# Using 'onelineaddress' searchtype for single-line address format
base_url = "https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress"

# Parameters for the API request
params = {
    'address': address,
    'benchmark': 'Public_AR_Current',  # Current address ranges
    'vintage': 'Current_Current',       # Current geography vintage
    'layers': 'all',                    # Request all available layers including school districts
    'format': 'json'                    # Return JSON format
}

print(f"Geocoding address: {address}\n")
print("Making request to US Census Geocoder API...")
print(f"URL: {base_url}")
print(f"Parameters: {json.dumps(params, indent=2)}\n")

try:
    # Make the API request
    response = requests.get(base_url, params=params)
    response.raise_for_status()  # Raise an error for bad status codes

    # Parse the JSON response
    data = response.json()

    print("=" * 80)
    print("FULL API RESPONSE")
    print("=" * 80)
    print(json.dumps(data, indent=2))
    print("\n")

    # Extract and display specific data points
    if 'result' in data and 'addressMatches' in data['result']:
        matches = data['result']['addressMatches']

        if matches:
            print("=" * 80)
            print("EXTRACTED DATA SUMMARY")
            print("=" * 80)

            for idx, match in enumerate(matches, 1):
                print(f"\n--- Match #{idx} ---")

                # Address information
                if 'matchedAddress' in match:
                    print(f"\nMatched Address: {match['matchedAddress']}")

                # Coordinates (Latitude/Longitude)
                if 'coordinates' in match:
                    coords = match['coordinates']
                    print(f"\nCoordinates:")
                    print(f"  Longitude (X): {coords.get('x')}")
                    print(f"  Latitude (Y): {coords.get('y')}")

                # Tiger Line information
                if 'tigerLine' in match:
                    tiger = match['tigerLine']
                    print(f"\nTIGER Line ID: {tiger.get('tigerLineId')}")
                    print(f"  Side: {tiger.get('side')}")

                # Address components
                if 'addressComponents' in match:
                    components = match['addressComponents']
                    print(f"\nAddress Components:")
                    print(f"  Street: {components.get('fromAddress')} {components.get('preQualifier', '')} {components.get('preDirection', '')} {components.get('streetName', '')} {components.get('suffixType', '')}")
                    print(f"  City: {components.get('city')}")
                    print(f"  State: {components.get('state')}")
                    print(f"  ZIP: {components.get('zip')}")

                # Geographic information
                if 'geographies' in match:
                    geographies = match['geographies']

                    # Census Blocks
                    if 'Census Blocks' in geographies:
                        print(f"\n{'=' * 40}")
                        print("CENSUS BLOCKS")
                        print('=' * 40)
                        for block in geographies['Census Blocks']:
                            print(f"  GEOID: {block.get('GEOID')}")
                            print(f"  Name: {block.get('NAME')}")
                            print(f"  Block: {block.get('BLOCK')}")
                            print(f"  Block Group: {block.get('BLKGRP')}")
                            print(f"  FIPS: {block.get('FIPS')}")
                            print(f"  Functional Status: {block.get('FUNCSTAT')}")
                            print()

                    # Census Tracts
                    if 'Census Tracts' in geographies:
                        print(f"{'=' * 40}")
                        print("CENSUS TRACTS")
                        print('=' * 40)
                        for tract in geographies['Census Tracts']:
                            print(f"  GEOID: {tract.get('GEOID')}")
                            print(f"  Name: {tract.get('NAME')}")
                            print(f"  Tract: {tract.get('TRACT')}")
                            print(f"  County: {tract.get('COUNTY')}")
                            print(f"  State: {tract.get('STATE')}")
                            print(f"  Basin Code: {tract.get('BASENAME')}")
                            print()

                    # Counties
                    if 'Counties' in geographies:
                        print(f"{'=' * 40}")
                        print("COUNTIES")
                        print('=' * 40)
                        for county in geographies['Counties']:
                            print(f"  GEOID: {county.get('GEOID')}")
                            print(f"  Name: {county.get('NAME')}")
                            print(f"  County FIPS: {county.get('COUNTY')}")
                            print(f"  State: {county.get('STATE')}")
                            print()

                    # States
                    if 'States' in geographies:
                        print(f"{'=' * 40}")
                        print("STATES")
                        print('=' * 40)
                        for state in geographies['States']:
                            print(f"  GEOID: {state.get('GEOID')}")
                            print(f"  Name: {state.get('NAME')}")
                            print(f"  State FIPS: {state.get('STATE')}")
                            print()

                    # Unified School Districts
                    if 'Unified School Districts' in geographies:
                        print(f"{'=' * 40}")
                        print("UNIFIED SCHOOL DISTRICTS")
                        print('=' * 40)
                        for district in geographies['Unified School Districts']:
                            print(f"  GEOID: {district.get('GEOID')}")
                            print(f"  Name: {district.get('NAME')}")
                            print(f"  School District LEA: {district.get('SDLEA')}")
                            print(f"  State: {district.get('STATE')}")
                            print(f"  Lowest Grade: {district.get('LOGRADE')}")
                            print(f"  Highest Grade: {district.get('HIGRADE')}")
                            print()

                    # Secondary School Districts
                    if 'Secondary School Districts' in geographies:
                        print(f"{'=' * 40}")
                        print("SECONDARY SCHOOL DISTRICTS")
                        print('=' * 40)
                        for district in geographies['Secondary School Districts']:
                            print(f"  GEOID: {district.get('GEOID')}")
                            print(f"  Name: {district.get('NAME')}")
                            print(f"  School District LEA: {district.get('SDLEA')}")
                            print(f"  State: {district.get('STATE')}")
                            print()

                    # Elementary School Districts
                    if 'Elementary School Districts' in geographies:
                        print(f"{'=' * 40}")
                        print("ELEMENTARY SCHOOL DISTRICTS")
                        print('=' * 40)
                        for district in geographies['Elementary School Districts']:
                            print(f"  GEOID: {district.get('GEOID')}")
                            print(f"  Name: {district.get('NAME')}")
                            print(f"  School District LEA: {district.get('SDLEA')}")
                            print(f"  State: {district.get('STATE')}")
                            print()

                    # Display all other geography layers
                    print(f"{'=' * 40}")
                    print("OTHER GEOGRAPHY LAYERS")
                    print('=' * 40)
                    other_layers = [key for key in geographies.keys() if key not in [
                        'Census Blocks', 'Census Tracts', 'Counties', 'States',
                        'Unified School Districts', 'Secondary School Districts',
                        'Elementary School Districts'
                    ]]

                    for layer_name in other_layers:
                        print(f"\n{layer_name}:")
                        for item in geographies[layer_name]:
                            print(f"  {json.dumps(item, indent=4)}")
        else:
            print("No address matches found.")
    else:
        print("Unexpected API response format.")
        print(json.dumps(data, indent=2))

except requests.exceptions.RequestException as e:
    print(f"Error making API request: {e}")
except json.JSONDecodeError as e:
    print(f"Error parsing JSON response: {e}")
except Exception as e:
    print(f"Unexpected error: {e}")
