import sys
import os
sys.path.insert(0, os.path.abspath("."))
import asyncio
from datetime import date
from app.dhan.historical import historical_manager

async def test_fetch():
    for sec_id, sym in [("13", "NIFTY"), ("25", "BANKNIFTY"), ("51", "SENSEX")]:
        c = await historical_manager.fetch_intraday_candles(
            security_id=sec_id,
            symbol=sym,
            trade_date=date(2026, 10, 7),
            interval=5,
        )
        print(f"{sym} ({sec_id}) on 2026-10-07: got {len(c)} 5m candles")
        if c:
            print(f"  First: {c[0].timestamp} O={c[0].open} C={c[0].close}")
            print(f"  Last:  {c[-1].timestamp} O={c[-1].open} C={c[-1].close}")

asyncio.run(test_fetch())
