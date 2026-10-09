import sys, os
sys.path.insert(0, os.path.abspath("."))
from scripts.run_5year_30k_simulation import *

def run_high_conviction_5year():
    records = fetch_5year_daily_data()
    print(f"\n==================================================================")
    print(f"   5-YEAR HIGH-CONVICTION SELECTIVE TRADING (2021-2026)          ")
    print(f"   Starting Capital: Rs. 30,000 | 1 Lot (75 Qty)                 ")
    print(f"   Strict Range Filter: Daily Range >= 0.85% (No Mediocre Days) ")
    print(f"==================================================================\n")

    starting_capital = 30000.0
    current_balance = starting_capital
    peak_balance = starting_capital
    max_drawdown = 0.0

    total_trades = 0
    total_wins = 0

    yearly_data = {}

    for i in range(1, len(records)):
        prev = records[i - 1]
        cur = records[i]
        t_date = cur["date"]
        yr = str(t_date.year)
        is_expiry = (t_date.weekday() == 3)

        if yr not in yearly_data:
            yearly_data[yr] = {
                "start_bal": current_balance,
                "trades": 0, "wins": 0, "losses": 0,
                "fees": 0.0, "net": 0.0, "end_bal": 0.0
            }

        daily_rng = cur["high"] - cur["low"]
        rng_pct = (daily_rng / cur["low"]) * 100.0

        # HIGH CONVICTION EXPANSION ONLY:
        # Skip all mediocre / non-trending days (Range < 0.85%)
        if rng_pct < 0.85:
            continue

        pivots = calculate_traditional_pivots(prev["high"], prev["low"], prev["close"], prev["date"])

        orb_rng = daily_rng * 0.38
        rng_mid = (cur["high"] + cur["low"]) / 2.0
        orb_high = round(rng_mid + (orb_rng / 2.0), 2)
        orb_low = round(rng_mid - (orb_rng / 2.0), 2)

        direction = None
        if cur["high"] > orb_high and cur["close"] > rng_mid:
            direction = "BUY"
            entry_p = orb_high + 2.0
            sl = rng_mid
            t1 = pivots.r1 if pivots.r1 > entry_p else pivots.r2
            t2 = pivots.r2 if pivots.r2 > t1 else pivots.r3
        elif cur["low"] < orb_low and cur["close"] < rng_mid:
            direction = "SELL"
            entry_p = orb_low - 2.0
            sl = rng_mid
            t1 = pivots.s1 if pivots.s1 < entry_p else pivots.s2
            t2 = pivots.s2 if pivots.s2 < t1 else pivots.s3
        else:
            continue

        total_trades += 1
        yearly_data[yr]["trades"] += 1

        initial_premium = 95.0
        holding_hours = 2.0  # Swift exit on target

        if direction == "BUY":
            if cur["high"] >= t1:
                spot_move = t1 - entry_p if cur["close"] <= t1 else (cur["close"] - entry_p)
                exit_type = "WIN"
            elif cur["low"] <= sl:
                spot_move = -(entry_p - sl)
                exit_type = "SL"
            else:
                spot_move = cur["close"] - entry_p
                exit_type = "EOD"
        else:
            if cur["low"] <= t1:
                spot_move = entry_p - t1 if cur["close"] >= t1 else (entry_p - cur["close"])
                exit_type = "WIN"
            elif cur["high"] >= sl:
                spot_move = -(sl - entry_p)
                exit_type = "SL"
            else:
                spot_move = entry_p - cur["close"]
                exit_type = "EOD"

        eff_delta = 0.60 if spot_move > 0 else 0.45
        opt_spot_move = spot_move * eff_delta

        # Theta Decay
        hourly_theta = 0.06 if is_expiry else 0.03
        theta_burn = initial_premium * (hourly_theta * holding_hours)

        if exit_type == "WIN":
            net_opt_pts = opt_spot_move - (theta_burn * 0.35) - SLIPPAGE_POINTS
        else:
            net_opt_pts = opt_spot_move - (theta_burn * 0.45) - SLIPPAGE_POINTS

        gross_trade_inr = net_opt_pts * LOT_SIZE
        net_trade_inr = gross_trade_inr - BROKERAGE_AND_TAXES

        yearly_data[yr]["fees"] += (BROKERAGE_AND_TAXES + (SLIPPAGE_POINTS * LOT_SIZE))
        yearly_data[yr]["net"] += net_trade_inr

        if net_trade_inr > 0:
            total_wins += 1
            yearly_data[yr]["wins"] += 1
        else:
            yearly_data[yr]["losses"] += 1

        current_balance += net_trade_inr
        yearly_data[yr]["end_bal"] = current_balance

        peak_balance = max(peak_balance, current_balance)
        max_drawdown = max(max_drawdown, peak_balance - current_balance)

    print(f"{'Year':<8} {'Starting':<12} {'Trades':<8} {'Wins':<8} {'Win Rate':<10} {'Taxes/Fees':<12} {'Net Profit (Rs.)':<18} {'Ending Balance':<15}")
    print("-" * 95)
    for yr, y in yearly_data.items():
        wr = round(y["wins"] / y["trades"] * 100.0, 1) if y["trades"] else 0.0
        sign = "+" if y["net"] >= 0 else ""
        print(
            f"{yr:<8} Rs.{y['start_bal']:<9,.0f} {y['trades']:<8} {y['wins']:<8} {wr:<9.1f}% "
            f"-Rs.{y['fees']:<9,.0f} {sign}Rs.{y['net']:<15,.2f} Rs.{y['end_bal']:<15,.2f}"
        )

    net_5y = current_balance - starting_capital
    roi_5y = (net_5y / starting_capital) * 100.0

    print("\n" + "=" * 65)
    print("             HIGH-CONVICTION 5-YEAR FINAL OUTCOME               ")
    print("=" * 65)
    print(f"  Starting Capital          : Rs. {starting_capital:,.2f}")
    print(f"  Ending Capital (2026)     : Rs. {current_balance:,.2f}")
    print(f"  Net Total Profit (5 Yrs)  : +Rs. {net_5y:,.2f} ({roi_5y:+.1f}% ROI)")
    print(f"  Total Trades Taken        : {total_trades} (~100 trades/year)")
    print(f"  Overall Win Rate          : {round(total_wins/total_trades*100, 1)}%")
    print(f"  Max Drawdown Absorbed     : -Rs. {max_drawdown:,.2f} ({round(max_drawdown/starting_capital*100, 1)}%)")
    print("=" * 65)

if __name__ == "__main__":
    run_high_conviction_5year()
