"""
6-Month Full Historical Backtest & Account Simulation:
Starting Capital: Rs. 10,000
Universe: NIFTY 50 (1 Lot = 75 Qty, Delta = 0.50, Brokerage/Tax = Rs. 50/trade)
Period: April 2026 to October 2026 (~6 Months, 125+ Trading Days)
With Exact Historical Prior-Day Traditional Floor Pivots (H, L, C)
"""

from __future__ import annotations
import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import os
import sys
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.abspath("."))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from app.analysis.pivot_points import (
    calculate_traditional_pivots,
    pivot_engine,
    TraditionalPivotPoints,
)
from app.dhan.historical import historical_manager
from app.storage.models import Candle, Direction

LOT_SIZE = 75
ATM_DELTA = 0.50
ROUNDTRIP_FEES = 50.0  # Rs. 20 Buy + Rs. 20 Sell + STT/GST/Exchange charges


@dataclass
class MonthTradeRecord:
    trade_date: date
    symbol: str
    direction: Direction
    entry_time: datetime
    entry_price: float
    initial_sl: float
    target_1: float
    target_2: float
    target_3: float
    exit_time: datetime
    exit_price: float
    exit_reason: str
    spot_pnl: float
    gross_inr: float
    net_inr: float
    balance_after: float
    target_1_hit: bool
    target_2_hit: bool
    target_3_hit: bool


async def run_6month_backtest():
    start_date = date(2026, 4, 1)
    end_date = date(2026, 10, 8)

    # 1. Discover all trading dates
    cur = start_date
    candidate_dates = []
    while cur <= end_date:
        if cur.weekday() < 5:
            candidate_dates.append(cur)
        cur += timedelta(days=1)

    print(f"\n==================================================================")
    print(f"       6-MONTH INDEX PIVOT BACKTEST & RS. 10,000 SIMULATION       ")
    print(f"       Date Range: {start_date} to {end_date}                     ")
    print(f"       Candidate Weekdays: {len(candidate_dates)} days             ")
    print(f"==================================================================\n")

    # 2. Pre-fetch and cache all daily candles to compute exact prior-day HLC
    print("Loading historical 5m candles and computing exact daily pivots...")
    day_candles_map: Dict[date, List[Candle]] = {}
    day_hlc_map: Dict[date, Tuple[float, float, float]] = {}

    for t_date in candidate_dates:
        candles = await historical_manager.fetch_intraday_candles("13", "NIFTY", t_date, interval=5)
        if len(candles) >= 30:
            day_candles_map[t_date] = candles
            d_high = max(c.high for c in candles)
            d_low = min(c.low for c in candles)
            d_close = candles[-1].close
            day_hlc_map[t_date] = (d_high, d_low, d_close)

    valid_dates = sorted(day_candles_map.keys())
    print(f"Loaded {len(valid_dates)} full trading sessions.\n")

    starting_capital = 10000.0
    current_balance = starting_capital
    peak_balance = starting_capital
    max_drawdown = 0.0

    all_trades: List[MonthTradeRecord] = []

    for idx, t_date in enumerate(valid_dates):
        candles = day_candles_map[t_date]

        # Calculate exact prior day pivots
        if idx == 0:
            # First day has no prior in set, use fallback
            pivots = await pivot_engine.get_index_pivots("13", "NIFTY", t_date)
        else:
            prev_date = valid_dates[idx - 1]
            prev_h, prev_l, prev_c = day_hlc_map[prev_date]
            pivots = calculate_traditional_pivots(prev_h, prev_l, prev_c, prev_date)

        if not pivots:
            continue

        # Extract benchmark range from Candles 2 & 3 (09:30 to 10:00 IST)
        range_candles = []
        post_10am_candles = []
        for c in candles:
            t = c.timestamp.time()
            if datetime.strptime("09:30:00", "%H:%M:%S").time() <= t < datetime.strptime("10:00:00", "%H:%M:%S").time():
                range_candles.append(c)
            elif t >= datetime.strptime("10:00:00", "%H:%M:%S").time():
                post_10am_candles.append(c)

        if len(range_candles) < 4 or not post_10am_candles:
            continue

        range_high = max(c.high for c in range_candles)
        range_low = min(c.low for c in range_candles)
        range_mid = round((range_high + range_low) / 2.0, 2)

        # Scan for 2-Candle Breakout + Confirmation
        trade_taken = False
        for i in range(len(post_10am_candles) - 1):
            if trade_taken:
                break
            c_break = post_10am_candles[i]
            c_next = post_10am_candles[i + 1]

            if c_break.timestamp.time() > datetime.strptime("14:30:00", "%H:%M:%S").time():
                break

            # Bullish Breakout
            if c_break.close > range_high:
                same_dir_open = c_next.open >= (range_high * 0.9995)
                confirmed_break = c_next.high > c_break.high

                if same_dir_open and confirmed_break:
                    entry_p = max(c_next.open, c_break.high + 0.5)
                    is_ok, _, sl, t1, t2, t3 = pivot_engine.determine_targets_and_sl(
                        symbol="NIFTY",
                        direction=Direction.LONG,
                        entry_price=entry_p,
                        orb_high=range_high,
                        orb_low=range_low,
                        orb_mid=range_mid,
                        pivots=pivots,
                    )
                    if not is_ok:
                        continue

                    trade = _simulate_single_day(
                        t_date, Direction.LONG, c_next.timestamp, entry_p, sl, t1, t2, t3,
                        post_10am_candles[i + 1:], current_balance
                    )
                    if trade:
                        current_balance = trade.balance_after
                        peak_balance = max(peak_balance, current_balance)
                        max_drawdown = max(max_drawdown, peak_balance - current_balance)
                        all_trades.append(trade)
                        trade_taken = True

            # Bearish Breakdown
            elif c_break.close < range_low:
                same_dir_open = c_next.open <= (range_low * 1.0005)
                confirmed_break = c_next.low < c_break.low

                if same_dir_open and confirmed_break:
                    entry_p = min(c_next.open, c_break.low - 0.5)
                    is_ok, _, sl, t1, t2, t3 = pivot_engine.determine_targets_and_sl(
                        symbol="NIFTY",
                        direction=Direction.SHORT,
                        entry_price=entry_p,
                        orb_high=range_high,
                        orb_low=range_low,
                        orb_mid=range_mid,
                        pivots=pivots,
                    )
                    if not is_ok:
                        continue

                    trade = _simulate_single_day(
                        t_date, Direction.SHORT, c_next.timestamp, entry_p, sl, t1, t2, t3,
                        post_10am_candles[i + 1:], current_balance
                    )
                    if trade:
                        current_balance = trade.balance_after
                        peak_balance = max(peak_balance, current_balance)
                        max_drawdown = max(max_drawdown, peak_balance - current_balance)
                        all_trades.append(trade)
                        trade_taken = True

    _print_results(starting_capital, current_balance, max_drawdown, all_trades, len(valid_dates))


def _simulate_single_day(
    t_date: date,
    direction: Direction,
    entry_time: datetime,
    entry_price: float,
    sl: float,
    t1: float,
    t2: float,
    t3: float,
    candles: List[Candle],
    current_balance: float,
) -> MonthTradeRecord:
    current_sl = sl
    t1_hit = False
    t2_hit = False
    t3_hit = False

    for c in candles:
        # Check Stop Loss first (conservative)
        if direction == Direction.LONG and c.low <= current_sl:
            exit_p = current_sl
            spot_pnl = exit_p - entry_price
            reason = "T2_LOCK" if t2_hit else ("T1_BE" if t1_hit else "SL")
            gross = 0.0 if t1_hit and not t2_hit else (spot_pnl * ATM_DELTA * LOT_SIZE)
            net = gross - ROUNDTRIP_FEES
            return MonthTradeRecord(
                t_date, "NIFTY", direction, entry_time, entry_price, sl, t1, t2, t3,
                c.timestamp, exit_p, reason, spot_pnl, gross, net, current_balance + net,
                t1_hit, t2_hit, t3_hit
            )
        elif direction == Direction.SHORT and c.high >= current_sl:
            exit_p = current_sl
            spot_pnl = entry_price - exit_p
            reason = "T2_LOCK" if t2_hit else ("T1_BE" if t1_hit else "SL")
            gross = 0.0 if t1_hit and not t2_hit else (spot_pnl * ATM_DELTA * LOT_SIZE)
            net = gross - ROUNDTRIP_FEES
            return MonthTradeRecord(
                t_date, "NIFTY", direction, entry_time, entry_price, sl, t1, t2, t3,
                c.timestamp, exit_p, reason, spot_pnl, gross, net, current_balance + net,
                t1_hit, t2_hit, t3_hit
            )

        # Check Target 3 (Full Target)
        if (direction == Direction.LONG and c.high >= t3) or (direction == Direction.SHORT and c.low <= t3):
            spot_pnl = (t3 - entry_price) if direction == Direction.LONG else (entry_price - t3)
            gross = spot_pnl * ATM_DELTA * LOT_SIZE
            net = gross - ROUNDTRIP_FEES
            return MonthTradeRecord(
                t_date, "NIFTY", direction, entry_time, entry_price, sl, t1, t2, t3,
                c.timestamp, t3, "T3_FULL", spot_pnl, gross, net, current_balance + net,
                True, True, True
            )

        # Target 2 Hit -> Trail SL to T1
        if not t2_hit:
            if (direction == Direction.LONG and c.high >= t2) or (direction == Direction.SHORT and c.low <= t2):
                t2_hit = True
                t1_hit = True
                current_sl = t1

        # Target 1 Hit -> Trail SL to Entry Price (Break-Even)
        if not t1_hit:
            if (direction == Direction.LONG and c.high >= t1) or (direction == Direction.SHORT and c.low <= t1):
                t1_hit = True
                current_sl = entry_price

        # EOD Close (15:15)
        if c.timestamp.time() >= datetime.strptime("15:15:00", "%H:%M:%S").time():
            exit_p = c.close
            spot_pnl = (exit_p - entry_price) if direction == Direction.LONG else (entry_price - exit_p)
            gross = spot_pnl * ATM_DELTA * LOT_SIZE
            net = gross - ROUNDTRIP_FEES
            return MonthTradeRecord(
                t_date, "NIFTY", direction, entry_time, entry_price, sl, t1, t2, t3,
                c.timestamp, exit_p, "EOD", spot_pnl, gross, net, current_balance + net,
                t1_hit, t2_hit, t3_hit
            )

    last_c = candles[-1]
    spot_pnl = (last_c.close - entry_price) if direction == Direction.LONG else (entry_price - last_c.close)
    gross = spot_pnl * ATM_DELTA * LOT_SIZE
    net = gross - ROUNDTRIP_FEES
    return MonthTradeRecord(
        t_date, "NIFTY", direction, entry_time, entry_price, sl, t1, t2, t3,
        last_c.timestamp, last_c.close, "EOD", spot_pnl, gross, net, current_balance + net,
        t1_hit, t2_hit, t3_hit
    )


def _print_results(
    starting_capital: float,
    ending_balance: float,
    max_dd: float,
    trades: List[MonthTradeRecord],
    total_days: int,
):
    total_trades = len(trades)
    wins = [t for t in trades if t.net_inr > 0]
    be_trades = [t for t in trades if t.exit_reason == "T1_BE"]
    losses = [t for t in trades if t.net_inr < 0 and t.exit_reason != "T1_BE"]

    total_net = ending_balance - starting_capital
    total_roi = (total_net / starting_capital) * 100.0
    win_rate = (len(wins) / total_trades * 100.0) if total_trades else 0.0

    gross_profit = sum(t.gross_inr for t in wins)
    gross_loss = abs(sum(t.gross_inr for t in trades if t.gross_inr < 0))
    profit_factor = round(gross_profit / max(1.0, gross_loss), 2)

    # Monthly breakdown
    monthly_stats = {}
    for t in trades:
        m_key = t.trade_date.strftime("%B %Y")
        if m_key not in monthly_stats:
            monthly_stats[m_key] = {"trades": 0, "wins": 0, "net": 0.0}
        monthly_stats[m_key]["trades"] += 1
        if t.net_inr > 0:
            monthly_stats[m_key]["wins"] += 1
        monthly_stats[m_key]["net"] += t.net_inr

    print("\n" + "=" * 65)
    print("               6-MONTH COMPREHENSIVE PERFORMANCE               ")
    print("=" * 65)
    print(f"  Starting Capital          : Rs. {starting_capital:,.2f}")
    print(f"  Ending Balance (6 Months) : Rs. {ending_balance:,.2f}")
    print(f"  Net Total Profit (ROI)    : +Rs. {total_net:,.2f} ({total_roi:+.1f}%)")
    print(f"  Total Trading Sessions    : {total_days} days")
    print(f"  Total Trades Triggered    : {total_trades}")
    print(f"  Winning Trades            : {len(wins)} ({round(win_rate, 1)}%)")
    print(f"  Zero-Loss Trailed (T1_BE) : {len(be_trades)} ({round(len(be_trades)/total_trades*100, 1)}%)")
    print(f"  Stopped Out Trades (Loss) : {len(losses)} ({round(len(losses)/total_trades*100, 1)}%)")
    print(f"  Capital Protection Rate   : {round((len(wins) + len(be_trades))/total_trades*100, 1)}%")
    print(f"  Profit Factor             : {profit_factor}")
    print(f"  Max Drawdown              : -Rs. {max_dd:,.2f} ({round(max_dd/starting_capital*100, 1)}%)")
    print("=" * 65)

    print("\nMONTH-BY-MONTH BREAKDOWN:")
    print(f"{'Month':<18} {'Trades':<10} {'Wins':<10} {'Win Rate':<12} {'Net Profit (Rs.)':<18}")
    print("-" * 65)
    for m_key, s in monthly_stats.items():
        wr = round((s["wins"] / s["trades"]) * 100.0, 1) if s["trades"] else 0.0
        sign = "+" if s["net"] >= 0 else ""
        print(f"{m_key:<18} {s['trades']:<10} {s['wins']:<10} {wr:<11.1f}% {sign}Rs. {s['net']:<15,.2f}")
    print("=" * 65)


if __name__ == "__main__":
    asyncio.run(run_6month_backtest())
