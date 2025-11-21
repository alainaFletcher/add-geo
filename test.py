from geocodio import Geocodio
import json

client = Geocodio('355251ff0125773161eddd12ddf656150300e7f')

data = client.geocode(
    "638 E 13th Ave, Denver, CO",
    fields=["school"]
)

print(data)