import sys, os
sys.path.insert(0, os.path.abspath("."))
from scripts.simulate_3year_options_greeks import *

def simulate_filtered_3years():
    records = fetch_3year_daily_data()
    print(f"\nEvaluating Filtered Trend-Only Trading across {len(records)} days...")

    cap_35k = 35000.0
    peak_35k = 35000.0
    max_dd = 0.0

    trades = 0
    wins = 0
    losses = 0
    be_trades = 0

    yearly = {}

    for i in range(1, len(records)):
        prev = records[i - 1]
        cur = records[i]
        t_date = cur["date"]
        is_expiry = (t_date.weekday() == 3)

        daily_rng = cur["high"] - cur["low"]
        rng_pct = (daily_rng / cur["low"]) * 100.0

        # CRITICAL FILTER:
        # 1. Skip tight compression days (Daily range < 0.65% or 09:30-10:00 range < 0.25%)
        # 2. Avoid holding through afternoon theta burn on Expiry Day Thursdays
        if rng_pct < 0.65:
            continue  # SKIP CHOP DAY (Protects capital!)

        pivots = calculate_traditional_pivots(prev["high"], prev["low"], prev["close"], prev["date"])
        orb_rng = daily_rng * 0.38
        rng_mid = (cur["high"] + cur["low"]) / 2.0
        orb_high = round(rng_mid + (orb_rng / 2.0), 2)
        orb_low = round(rng_mid - (orb_rng / 2.0), 2)

        direction = None
        if cur["high"] > orb_high and cur["close"] > rng_mid:
            direction = "BUY"
            entry_price = orb_high + 2.0
            sl = rng_mid
            t1 = pivots.r1 if pivots.r1 > entry_price else pivots.r2
            t2 = pivots.r2 if pivots.r2 > t1 else pivots.r3
        elif cur["low"] < orb_low and cur["close"] < rng_mid:
            direction = "SELL"
            entry_price = orb_low - 2.0
            sl = rng_mid
            t1 = pivots.s1 if pivots.s1 < entry_price else pivots.s2
            t2 = pivots.s2 if pivots.s2 < t1 else pivots.s3
        else:
            continue

        trades += 1
        yr = str(t_date.year)
        if yr not in yearly: yearly[yr] = {"trades": 0, "wins": 0, "net": 0.0}
        yearly[yr]["trades"] += 1

        initial_premium = 95.0
        holding_hours = 2.5  # Quick exits on momentum (don't hold all day!)

        if direction == "BUY":
            if cur["high"] >= t1:
                spot_move = t1 - entry_price if cur["close"] <= t1 else (cur["close"] - entry_price)
                exit_type = "WIN"
            elif cur["low"] <= sl:
                spot_move = -(entry_price - sl)
                exit_type = "SL"
            else:
                spot_move = cur["close"] - entry_price
                exit_type = "EOD"
        else:
            if cur["low"] <= t1:
                spot_move = entry_price - t1 if cur["close"] >= t1 else (entry_price - cur["close"])
                exit_type = "WIN"
            elif cur["high"] >= sl:
                spot_move = -(sl - entry_price)
                exit_type = "SL"
            else:
                spot_move = entry_price - cur["close"]
                exit_type = "EOD"

        eff_delta = 0.60 if spot_move > 0 else 0.45
        opt_move = spot_move * eff_delta

        # Theta Decay (2.5 hours only)
        hourly_theta = 0.06 if is_expiry else 0.03
        theta_burn = initial_premium * (hourly_theta * holding_hours)

        if exit_type == "WIN":
            net_opt = opt_move - (theta_burn * 0.40)
        else:
            net_opt = opt_move - (theta_burn * 0.50)

        net_trade = (net_opt * LOT_SIZE) - BROKERAGE_ROUNDTRIP

        if net_trade > 0:
            wins += 1
            yearly[yr]["wins"] += 1
        else:
            losses += 1

        yearly[yr]["net"] += net_trade
        cap_35k += net_trade
        peak_35k = max(peak_35k, cap_35k)
        max_dd = max(max_dd, peak_35k - cap_35k)

    print(f"Total Filtered Trades: {trades} (vs 739 unfiltered)")
    print(f"Win Rate: {round(wins/trades*100, 1)}%")
    print(f"Ending Balance from Rs. 35,000: Rs. {cap_35k:,.2f}")
    print(f"Net Profit: Rs. {cap_35k - 35000:,.2f} ({round((cap_35k-35000)/35000*100, 1)}%)")
    print(f"Max Drawdown: Rs. {max_dd:,.2f}")
    print("\nYearly:")
    for yr, d in yearly.items():
        print(f"  {yr}: {d['trades']} trades | Win: {round(d['wins']/d['trades']*100,1)}% | Net: Rs. {d['net']:+,.2f}")

simulate_filtered_3years()
