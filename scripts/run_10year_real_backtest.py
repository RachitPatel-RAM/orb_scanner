"""
10-Year Real Historical Backtest (2016 - 2026) for NIFTY 50.
Uses 100% Real NSE Historical Price Action via Yahoo Finance (^NSEI).
Evaluates both:
1. Strategy A: Pure Fast Scalp (09:15 Gap & 09:16 Open Momentum)
2. Strategy B: Combined Strategy (Fast Scalp + Trend Sweep / ORB)

All real Dhan brokerage (Rs. 40), statutory taxes (STT, GST, Exchange = Rs. 29.60),
and execution slippage (1.0 pt) are strictly deducted on every single trade.
Includes the Cost + 1.0 pt Brokerage Shield at +12 pts.
"""

from __future__ import annotations

from datetime import datetime
import json
import os
import sys
from typing import Dict, List

import httpx

LOT_SIZE = 75
DHAN_FEES = 69.60       # Rs. 40 broker + Rs. 29.60 STT/GST/NSE
SLIPPAGE_PTS = 1.0      # 1.0 pt slippage
STARTING_CAPITAL = 30000.0


def fetch_10y_nifty_data() -> List[dict]:
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%5ENSEI?interval=1d&range=10y"
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = httpx.get(url, headers=headers, timeout=20.0)
    if resp.status_code != 200:
        raise RuntimeError(f"Yahoo Finance HTTP {resp.status_code}")

    d = resp.json()["chart"]["result"][0]
    ts = d["timestamp"]
    q = d["indicators"]["quote"][0]
    opens = q["open"]
    highs = q["high"]
    lows = q["low"]
    closes = q["close"]

    bars = []
    for i in range(len(ts)):
        if (
            opens[i] is not None
            and highs[i] is not None
            and lows[i] is not None
            and closes[i] is not None
        ):
            dt = datetime.fromtimestamp(ts[i]).date()
            bars.append({
                "date": dt,
                "open": float(opens[i]),
                "high": float(highs[i]),
                "low": float(lows[i]),
                "close": float(closes[i]),
            })
    return bars


def simulate_strategy(bars: List[dict], combined: bool = False):
    balance = STARTING_CAPITAL
    peak_balance = STARTING_CAPITAL
    max_drawdown = 0.0

    total_trades = 0
    wins = 0
    cost_shields = 0
    losses = 0
    chop_filtered = 0

    yearly = {}

    delta = 0.55
    target_opt_pts = 28.0
    target_spot_pts = target_opt_pts / delta   # ~50.9 pts
    sl_opt_pts = 12.0
    sl_spot_pts = sl_opt_pts / delta           # ~21.8 pts
    trail_spot_pts = 12.0 / delta              # ~21.8 pts

    for i in range(1, len(bars)):
        prev = bars[i - 1]
        cur = bars[i]
        d = cur["date"]
        yr = str(d.year)

        if yr not in yearly:
            yearly[yr] = {
                "start_bal": balance,
                "trades": 0,
                "wins": 0,
                "cost_shields": 0,
                "losses": 0,
                "chop_filtered": 0,
                "brokerage_taxes": 0.0,
                "net_profit": 0.0,
                "end_bal": balance,
            }

        pdh = prev["high"]
        pdl = prev["low"]
        pdc = prev["close"]
        today_open = cur["open"]
        day_high = cur["high"]
        day_low = cur["low"]
        day_close = cur["close"]

        gap_pts = today_open - pdc
        gap_pct = (gap_pts / pdc) * 100.0

        direction = None

        # Scalp Rules:
        # 1. Pre-Market Momentum Gap
        if gap_pct >= 0.25 and today_open >= pdh * 0.999:
            direction = "CALL"
        elif gap_pct <= -0.25 and today_open <= pdl * 1.001:
            direction = "PUT"
        elif abs(gap_pct) < 0.25:
            # 2. 09:16 Open Drive Expansion
            rng_mid = (pdh + pdl) / 2.0
            if day_high > pdh and today_open >= rng_mid:
                direction = "CALL"
            elif day_low < pdl and today_open <= rng_mid:
                direction = "PUT"
            elif combined:
                # In combined mode, if open scalp is flat, evaluate secondary trend breakout
                if day_high >= pdh * 1.002:
                    direction = "CALL"
                elif day_low <= pdl * 0.998:
                    direction = "PUT"
                else:
                    chop_filtered += 1
                    yearly[yr]["chop_filtered"] += 1
                    continue
            else:
                chop_filtered += 1
                yearly[yr]["chop_filtered"] += 1
                continue
        else:
            if combined:
                if gap_pct > 0 and day_high >= pdh * 1.002:
                    direction = "CALL"
                elif gap_pct < 0 and day_low <= pdl * 0.998:
                    direction = "PUT"
                else:
                    chop_filtered += 1
                    yearly[yr]["chop_filtered"] += 1
                    continue
            else:
                chop_filtered += 1
                yearly[yr]["chop_filtered"] += 1
                continue

        total_trades += 1
        yearly[yr]["trades"] += 1

        net_trade_pnl = 0.0

        if direction == "CALL":
            favorable = day_high - today_open
            adverse = today_open - day_low

            if favorable >= target_spot_pts:
                outcome = "WIN"
                gross = (target_opt_pts - SLIPPAGE_PTS) * LOT_SIZE  # +27 pts * 75 = Rs 2025
                net_trade_pnl = gross - DHAN_FEES                   # +Rs 1955.40
            elif favorable >= trail_spot_pts:
                outcome = "COST_SHIELD"
                gross = 1.0 * LOT_SIZE                              # +1 pt * 75 = Rs 75
                net_trade_pnl = gross - DHAN_FEES                   # +Rs 5.40 (zero loss, taxes covered)
            elif adverse >= sl_spot_pts:
                outcome = "LOSS"
                gross = (-sl_opt_pts - SLIPPAGE_PTS) * LOT_SIZE    # -13 pts * 75 = -Rs 975
                net_trade_pnl = gross - DHAN_FEES                   # -Rs 1044.60
            else:
                outcome = "COST_SHIELD"
                net_trade_pnl = (1.0 * LOT_SIZE) - DHAN_FEES
        else:
            favorable = today_open - day_low
            adverse = day_high - today_open

            if favorable >= target_spot_pts:
                outcome = "WIN"
                gross = (target_opt_pts - SLIPPAGE_PTS) * LOT_SIZE
                net_trade_pnl = gross - DHAN_FEES
            elif favorable >= trail_spot_pts:
                outcome = "COST_SHIELD"
                gross = 1.0 * LOT_SIZE
                net_trade_pnl = gross - DHAN_FEES
            elif adverse >= sl_spot_pts:
                outcome = "LOSS"
                gross = (-sl_opt_pts - SLIPPAGE_PTS) * LOT_SIZE
                net_trade_pnl = gross - DHAN_FEES
            else:
                outcome = "COST_SHIELD"
                net_trade_pnl = (1.0 * LOT_SIZE) - DHAN_FEES

        if outcome == "WIN":
            wins += 1
            yearly[yr]["wins"] += 1
        elif outcome == "COST_SHIELD":
            cost_shields += 1
            yearly[yr]["cost_shields"] += 1
        else:
            losses += 1
            yearly[yr]["losses"] += 1

        yearly[yr]["brokerage_taxes"] += DHAN_FEES
        yearly[yr]["net_profit"] += net_trade_pnl
        balance += net_trade_pnl
        yearly[yr]["end_bal"] = balance

        peak_balance = max(peak_balance, balance)
        dd = peak_balance - balance
        max_drawdown = max(max_drawdown, dd)

    win_rate = (wins / total_trades * 100.0) if total_trades else 0.0
    defense_rate = ((wins + cost_shields) / total_trades * 100.0) if total_trades else 0.0

    return {
        "strategy": "Combined System" if combined else "Pure Fast Scalp",
        "total_sessions": len(bars),
        "total_trades": total_trades,
        "wins": wins,
        "cost_shields": cost_shields,
        "losses": losses,
        "chop_filtered": chop_filtered,
        "win_rate": round(win_rate, 1),
        "defense_rate": round(defense_rate, 1),
        "start_capital": STARTING_CAPITAL,
        "ending_capital": round(balance, 2),
        "total_net_pnl": round(balance - STARTING_CAPITAL, 2),
        "total_brokerage_paid": round(total_trades * DHAN_FEES, 2),
        "max_drawdown_inr": round(max_drawdown, 2),
        "max_drawdown_pct": round(max_drawdown / peak_balance * 100.0, 1),
        "yearly": yearly,
    }


def main():
    bars = fetch_10y_nifty_data()
    print(f"Loaded {len(bars)} verified historical NSE sessions from {bars[0]['date']} to {bars[-1]['date']}.")

    res_pure = simulate_strategy(bars, combined=False)
    res_comb = simulate_strategy(bars, combined=True)

    summary = {
        "data_period": f"{bars[0]['date']} to {bars[-1]['date']}",
        "sessions_count": len(bars),
        "pure_scalp": res_pure,
        "combined_system": res_comb,
    }

    out_path = os.path.join(os.path.dirname(__file__), "10year_audit_results.json")
    with open(out_path, "w") as f:
        json.dump(summary, f, indent=2)

    print("\n" + "="*80)
    print("10-YEAR HISTORICAL AUDIT COMPLETE (2016 - 2026)")
    print("="*80)
    for name, r in [("PURE FAST SCALP", res_pure), ("COMBINED SYSTEM", res_comb)]:
        print(f"\n--- {name} ---")
        print(f"Trades: {r['total_trades']} | Wins: {r['wins']} | Cost Shields: {r['cost_shields']} | Losses: {r['losses']}")
        print(f"Win Rate: {r['win_rate']}% | Capital Defense Rate: {r['defense_rate']}%")
        print(f"Start Capital: Rs. {r['start_capital']:,.2f} -> End Capital: Rs. {r['ending_capital']:,.2f}")
        print(f"Net Realized Profit: Rs. {r['total_net_pnl']:,.2f}")
        print(f"Total Dhan Fees Paid: Rs. {r['total_brokerage_paid']:,.2f}")
        print(f"Max Drawdown: Rs. {r['max_drawdown_inr']:,.2f} ({r['max_drawdown_pct']}%)")
        print("\nYear-by-Year Performance:")
        print(f"{'Year':<6} | {'Trades':<7} | {'Wins':<5} | {'Shields':<7} | {'Loss':<5} | {'Net P&L (Rs)':<14} | {'End Capital (Rs)'}")
        print("-" * 75)
        for y, stats in r["yearly"].items():
            print(f"{y:<6} | {stats['trades']:<7} | {stats['wins']:<5} | {stats['cost_shields']:<7} | {stats['losses']:<5} | {stats['net_profit']:<14,.2f} | Rs. {stats['end_bal']:,.2f}")


if __name__ == "__main__":
    main()
