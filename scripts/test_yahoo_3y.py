import httpx
import json

url = "https://query1.finance.yahoo.com/v8/finance/chart/%5ENSEI?interval=1d&range=3y"
headers = {"User-Agent": "Mozilla/5.0"}
resp = httpx.get(url, headers=headers)
print("HTTP:", resp.status_code)
if resp.status_code == 200:
    data = resp.json()
    result = data["chart"]["result"][0]
    timestamps = result["timestamp"]
    print("Total daily candles for 3 years:", len(timestamps))
    print("Start:", timestamps[0], "End:", timestamps[-1])
