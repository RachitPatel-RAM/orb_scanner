"""
Real-World Account Simulation: ₹10,000 Starting Capital over 1 Month
Using Real Historical Dhan Trade Logs with 1-Lot Index Options & Brokerage/Taxes.
"""

from __future__ import annotations
import asyncio
from datetime import date
import os
import sys

sys.path.insert(0, os.path.abspath("."))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from scripts.run_index_pivot_backtest import IndexPivotBacktester, TradeResult
from app.storage.models import Direction

# Standard Lot Sizes & ATM Option Delta
LOT_SIZES = {
    "NIFTY": 75,
    "BANKNIFTY": 30,
    "SENSEX": 20,
}
ATM_DELTA = 0.50  # Average ATM option delta
ROUNDTRIP_BROKERAGE_AND_TAXES = 50.0  # ₹20 buy + ₹20 sell + STT/GST/Exchange fee


async def run_10k_simulation():
    tester = IndexPivotBacktester()
    all_trades = await tester.run_backtest()

    # Filter to 1 trade per day (the 1st breakout of the day, as a ₹10,000 trader trades 1 lot at a time)
    daily_trades = {}
    for t in all_trades:
        if t.trade_date not in daily_trades:
            daily_trades[t.trade_date] = t

    trades_to_simulate = list(daily_trades.values())
    trades_to_simulate.sort(key=lambda x: x.trade_date)

    starting_capital = 10000.0
    current_balance = starting_capital
    peak_balance = starting_capital
    max_drawdown = 0.0

    trade_records = []

    for t in trades_to_simulate:
        lot_size = LOT_SIZES.get(t.symbol, 50)
        
        # Spot points to Option premium points (ATM option moves ~50% of spot points)
        # For small spot movements, gamma/theta also play a role, so we model delta=0.50
        spot_pts = t.pnl_points
        opt_pts = spot_pts * ATM_DELTA
        
        gross_pnl_inr = opt_pts * lot_size
        net_pnl_inr = gross_pnl_inr - ROUNDTRIP_BROKERAGE_AND_TAXES
        
        # If trade exited at Break-Even (T1_BE), net loss is only brokerage (-₹50)
        if t.exit_reason == "T1_BE":
            gross_pnl_inr = 0.0
            net_pnl_inr = -ROUNDTRIP_BROKERAGE_AND_TAXES
            
        current_balance += net_pnl_inr
        peak_balance = max(peak_balance, current_balance)
        dd = peak_balance - current_balance
        max_drawdown = max(max_drawdown, dd)

        trade_records.append({
            "date": t.trade_date,
            "symbol": t.symbol,
            "direction": "BUY (CALL)" if t.direction == Direction.LONG else "SELL (PUT)",
            "spot_pts": spot_pts,
            "opt_pts": opt_pts,
            "exit_reason": t.exit_reason,
            "gross_inr": gross_pnl_inr,
            "net_inr": net_pnl_inr,
            "balance": current_balance,
        })

    total_net_profit = current_balance - starting_capital
    total_roi_pct = (total_net_profit / starting_capital) * 100.0
    max_dd_pct = (max_drawdown / starting_capital) * 100.0

    print("\n" + "=" * 90)
    print("        REAL Rs. 10,000 ACCOUNT SIMULATION: 1 MONTH REAL MARKET DATA        ")
    print("=" * 90)
    print(f"{'Date':<12} {'Trade':<15} {'Spot Pts':<10} {'Exit':<10} {'Gross P&L':<12} {'Brokerage':<10} {'Net P&L':<12} {'Account Balance':<15}")
    print("-" * 90)

    for r in trade_records:
        sign = "+" if r["net_inr"] >= 0 else ""
        print(
            f"{str(r['date']):<12} {r['symbol'] + ' ' + ('CE' if 'CALL' in r['direction'] else 'PE'):<15} "
            f"{r['spot_pts']:<+10.2f} {r['exit_reason']:<10} "
            f"Rs.{r['gross_inr']:<+10.2f}  -Rs.{ROUNDTRIP_BROKERAGE_AND_TAXES:<6.2f} "
            f"{sign}Rs.{r['net_inr']:<10.2f} Rs.{r['balance']:<15.2f}"
        )

    print("\n" + "=" * 65)
    print("                    1-MONTH ACCOUNT OUTCOME                     ")
    print("=" * 65)
    print(f"  Starting Capital        : Rs. {starting_capital:,.2f}")
    print(f"  Ending Balance (1 Mo)   : Rs. {current_balance:,.2f}")
    print(f"  Net Total Profit (ROI)  : +Rs. {total_net_profit:,.2f} ({total_roi_pct:+.1f}%)")
    print(f"  Total Trades Taken      : {len(trade_records)} trades")
    wins = [r for r in trade_records if r['net_inr'] > 0]
    print(f"  Winning Trades          : {len(wins)} / {len(trade_records)} ({round(len(wins)/len(trade_records)*100, 1)}%)")
    print(f"  Max Account Drawdown    : -Rs. {max_drawdown:,.2f} ({max_dd_pct:.1f}%)")
    print(f"  Profit Factor           : {round(sum(r['net_inr'] for r in wins) / max(1.0, abs(sum(r['net_inr'] for r in trade_records if r['net_inr'] < 0))), 2)}")
    print("=" * 65)


if __name__ == "__main__":
    asyncio.run(run_10k_simulation())
