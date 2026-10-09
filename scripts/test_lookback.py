import sys
import os
sys.path.insert(0, os.path.abspath("."))
import asyncio
from datetime import date
from app.dhan.historical import historical_manager

async def test_dhan_lookback():
    for test_d in [
        date(2026, 9, 1),
        date(2026, 8, 1),
        date(2026, 7, 1),
        date(2026, 6, 1),
        date(2026, 5, 4),
        date(2026, 4, 1),
    ]:
        c = await historical_manager.fetch_intraday_candles("13", "NIFTY", test_d, interval=5)
        print(f"Date {test_d}: got {len(c)} candles")

asyncio.run(test_dhan_lookback())
