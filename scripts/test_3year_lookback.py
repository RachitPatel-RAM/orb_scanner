import sys
import os
sys.path.insert(0, os.path.abspath("."))
import asyncio
from datetime import date
from app.dhan.historical import historical_manager

async def test_3year_lookback():
    for test_d in [
        date(2026, 1, 5),
        date(2025, 10, 1),
        date(2025, 5, 2),
        date(2025, 1, 6),
        date(2024, 10, 1),
        date(2024, 5, 2),
        date(2024, 1, 5),
        date(2023, 10, 3),
        date(2023, 5, 2),
    ]:
        c = await historical_manager.fetch_intraday_candles("13", "NIFTY", test_d, interval=5)
        print(f"Date {test_d}: got {len(c)} candles")

asyncio.run(test_3year_lookback())
