import sys
import os
sys.path.insert(0, os.path.abspath("."))
import asyncio
import httpx
from app.dhan.auth import auth

async def test_sensex():
    headers = auth.get_headers()
    url = "https://api.dhan.co/v2/charts/intraday"
    for seg, inst in [("IDX_I", "INDEX"), ("BSE_INDEX", "INDEX"), ("BSE", "INDEX"), ("NSE_EQ", "EQUITY")]:
        payload = {
            "securityId": "51",
            "exchangeSegment": seg,
            "instrument": inst,
            "fromDate": "2026-10-07 09:15:00",
            "toDate": "2026-10-07 15:30:00",
            "interval": "5",
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload, headers=headers)
            print(f"Seg={seg}, Inst={inst} -> HTTP {resp.status_code}: {resp.text[:120]}")

asyncio.run(test_sensex())
