#!/Users/fullerb/anaconda3/envs/geo

from geocodio import GeocodioClient
import json
import pyodbc
import snowflake.connector
import pandas as pd
from sqlalchemy import create_engine
import config.IC_config as ic_cfg


def main() -> None:
    #Get API Connection to Geocode.io
    client = GeocodioClient(ic_cfg.Geocodeio["apikey"])

    #get local IC database Credentials
    username = ic_cfg.IC_DB_Credentials["user"]
    pwd = ic_cfg.IC_DB_Credentials["passwd"]

    #create Sqlalchemy engine
    #engine = create_engine("mssql+pyodbc://" + username + ":" + pwd + "@icampus.mapleton.us:7771/mapleton?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes")

    #Create Connection to Infinite Campus database
    conn2 = pyodbc.connect('DRIVER={ODBC Driver 18 for SQL Server};'
                        'TrustServerCertificate=yes;'
                        'Server=icampus.mapleton.us,7771;'
                        'Database=mapleton;'
                        'UID='+ username + ';'
                        'PWD=' + pwd )

    # Get Snowflake Connection  (used for multiple data loads)
    ctx = snowflake.connector.connect(
        user=ic_cfg.SnowflakeConnection["user"],
        password=ic_cfg.SnowflakeConnection["password"],
        account=ic_cfg.SnowflakeConnection["account"],
        warehouse=ic_cfg.SnowflakeConnection["warehouse"],
        database=ic_cfg.SnowflakeConnection["database"],
        schema=ic_cfg.SnowflakeConnection["schema"],
        #authenticator=ic_cfg.SnowflakeConnection["authenticator"]
    )

    querystring = "select top 500 * from MPS_WAREHOUSE.DATALAKE.VIEW_ADDRESSESNEEDINGGEOCODED order by addressid desc"
    qry_district = "select DISTRICTID, NCESDISTRICTID from MPS_WAREHOUSE.IC.DISTRICT"

    # get pandas dataframe from snowflake query
    cs = ctx.cursor()
    try:
        cs.execute(querystring) 
        df = cs.fetch_pandas_all()
        cs.execute(qry_district)
        df_dist = cs.fetch_pandas_all()
        # Set ncesdistrictid as the index
        df_dist['NCESDISTRICTID'] = pd.to_numeric(df_dist['NCESDISTRICTID'], errors='coerce').astype('Int64')
        df_dist.set_index('NCESDISTRICTID', inplace=True)
    finally:
        cs.close()


    updatedGeocodeCount = 0

    try:

        #Create Connection to Infinite Campus database
        
        cupdate = conn2.cursor()
        cs = ctx.cursor()


        for index, row in df.iterrows():
            # Call Geocode.io API
            try:
                location = client.geocode(row['ADDRESSLINE1'], fields=["school","zip4","census2023"])
            except:
                print("Error geocoding address: " + row['ADDRESSLINE1'])

            #convert results into a dictionary
            try:
                resultsDict = dict(location)
                # get the number of results returned for this address
                len_results = len(resultsDict["results"])
            except:
                print("Error:  nothing returned when running dict(location)")
                len_results=0

        

            # get the data needed from the API call
            if len_results > 0:
                addressid = row['ADDRESSID']
                loc_accuracy = resultsDict["results"][0]['accuracy']
                loc_accuracy_type = resultsDict["results"][0]['accuracy_type']
                loc_lat = resultsDict["results"][0]['location']['lat']
                loc_lng = resultsDict["results"][0]['location']['lng']
                loc_source = resultsDict["results"][0]['source']
                block_code = resultsDict["results"][0]['fields']['census']['2023']['block_code']
                tract_code = resultsDict["results"][0]['fields']['census']['2023']['tract_code']
                county_fips = resultsDict["results"][0]['fields']['census']['2023']['county_fips']
                try:
                    loc_district = resultsDict["results"][0]['fields']['school_districts']['unified']['name']
                    loc_leacode = resultsDict["results"][0]['fields']['school_districts']['unified']['lea_code']
                except:
                    loc_leacode = '0000'
                    loc_district = 'none'
                    print(resultsDict)

                #look up the districtID given the ncesdistrictid
                ncesid = int(loc_leacode)

                if ncesid in df_dist.index:
                    districtID = df_dist.loc[ncesid, 'DISTRICTID']
                else:
                    districtID = 0
                    print("AddressID: ", addressid)
                    print("Address: ", row['ADDRESSLINE1'])
                    print("Number of Results: ", len_results)
                    print("Accuracy: ", loc_accuracy, "   Accuracy Type:  " + loc_accuracy_type)
                    print("District: ",loc_district)
                    print("Lea_Code: ", str(int(loc_leacode)))
                    print("DistrictID: ", districtID)
                    print("Latitude: ", loc_lat, "    Longitude: ", loc_lng)
                    print("Source: ",loc_source)
                    print("block_code: ", block_code)
                    print("tract: ", tract_code)
                    print("county_fips: ", county_fips)
                    print("")
            


                # Write results as JSON file to Snowflake
            if len_results >= 0:
                try:
                    jsontext = json.dumps(location)
                    jsontext = jsontext.replace("'", "''")
                    snow = "insert into LOGGING_GEOCODIOAPI_ADDRESSLOOKUPS (IC_ADDRESSID, GEOCODIO_JSON)  (select " + str(row['ADDRESSID']) + ", PARSE_JSON('%s'))" % jsontext
                    #print(snow)
                    cs.execute(snow)
                except snowflake.connector.errors.ProgrammingError as e:
                    print(e)  
                    print('Error {0} ({1}): {2} ({3})'.format(e.errno, e.sqlstate, e.msg, e.sfqid))

            # If results exist and accuracy of first row is .95 or greater then update lat and lng in IC
            if len_results > 0:
                if loc_accuracy >= .8:
                    if districtID == 0:
                        sqlUpdate = """ UPDATE ADDRESS SET latitude = ?, longitude = ?, tract = ?, block = ?, location_code = ? where addressid = ? """ 
                        args = loc_lat, loc_lng, tract_code, block_code, county_fips, addressid
                    else:
                        #sqlUpdate = 'Update address set latitude = ' +  str(loc_lat) + ', longitude = ' + str(loc_lng) + ', tract = ' + str(tract_code) + ', location_code =' + county_fips + ' , block = ' + str(block_code) + ', districtid = ' + str(districtID) + ' where addressid = ' + str(row['ADDRESSID'])
                        sqlUpdate = """ UPDATE ADDRESS SET latitude = ?, longitude = ?, tract = ?, block = ?, location_code = ?, districtID = ? where addressid = ? """ 
                        args = loc_lat, loc_lng, tract_code, block_code, county_fips, int(districtID), addressid
                    
                    try:
                        cupdate.execute(sqlUpdate, args)
                        cupdate.commit()
                    except:
                        print(sqlUpdate)
                        print(args)
                        
                    
                    updatedGeocodeCount = updatedGeocodeCount + 1
                

    finally:
        ctx.close()
        cs.close()
        cupdate.close()


if __name__ == "__main__":
    main()