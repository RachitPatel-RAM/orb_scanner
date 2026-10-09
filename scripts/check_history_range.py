import sys
import os
sys.path.insert(0, os.path.abspath("."))
import asyncio
from datetime import date, timedelta
from app.dhan.historical import historical_manager

async def check_history_range():
    today = date(2026, 10, 8)
    # Check past 30 days
    days = []
    d = today - timedelta(days=1)
    while len(days) < 20 and d > date(2026, 8, 1):
        if d.weekday() < 5:  # Monday to Friday
            days.append(d)
        d -= timedelta(days=1)

    print(f"Testing {len(days)} trading days...")
    available_days = []
    for test_d in days:
        c = await historical_manager.fetch_intraday_candles("13", "NIFTY", test_d, interval=5)
        if len(c) > 30:
            available_days.append(test_d)
            print(f"  {test_d}: Available ({len(c)} candles)")
        else:
            print(f"  {test_d}: No data")

    print(f"\nTotal available days for NIFTY: {len(available_days)}")

asyncio.run(check_history_range())
