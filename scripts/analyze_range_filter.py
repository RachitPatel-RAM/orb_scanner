import sys, os
sys.path.insert(0, os.path.abspath("."))
import asyncio
from datetime import date
from scripts.run_6month_10k_backtest import *

# Let's inspect the correlation between ORB Range Size % and trade outcome
async def analyze_range_filter():
    # Run simulation and inspect each trade's range %
    start_date = date(2026, 4, 1)
    end_date = date(2026, 10, 8)
    cur = start_date
    dates = []
    while cur <= end_date:
        if cur.weekday() < 5: dates.append(cur)
        cur += timedelta(days=1)
        
    day_candles_map = {}
    for d in dates:
        c = await historical_manager.fetch_intraday_candles("13", "NIFTY", d, 5)
        if len(c) >= 30:
            day_candles_map[d] = c
            
    valid_dates = sorted(day_candles_map.keys())
    
    narrow_wins, narrow_losses = 0, 0
    wide_wins, wide_losses = 0, 0
    
    for idx, t_date in enumerate(valid_dates):
        candles = day_candles_map[t_date]
        range_c = [c for c in candles if datetime.strptime("09:30:00", "%H:%M:%S").time() <= c.timestamp.time() < datetime.strptime("10:00:00", "%H:%M:%S").time()]
        if len(range_c) < 4: continue
        h = max(c.high for c in range_c)
        l = min(c.low for c in range_c)
        rng_pct = ((h - l) / l) * 100
        
        # Check day's net move
        day_open = candles[0].open
        day_close = candles[-1].close
        day_move_pct = abs(day_close - day_open) / day_open * 100
        
        if rng_pct < 0.25:
            # narrow range
            if day_move_pct > 0.5: narrow_wins += 1
            else: narrow_losses += 1
        else:
            if day_move_pct > 0.5: wide_wins += 1
            else: wide_losses += 1
            
    print(f"Narrow Range (<0.25%): Trend Days={narrow_wins}, Chop Days={narrow_losses}")
    print(f"Healthy Range (>=0.25%): Trend Days={wide_wins}, Chop Days={wide_losses}")

asyncio.run(analyze_range_filter())
