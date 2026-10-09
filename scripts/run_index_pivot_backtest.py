"""
Comprehensive Backtest: 15-Minute Benchmark (Candles 2 & 3: 09:30-10:00)
with 5-Minute Breakout + Next Candle Confirmation & Traditional Floor Pivots.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import os
import sys
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.abspath("."))

from app.analysis.pivot_points import pivot_engine, TraditionalPivotPoints
from app.dhan.historical import historical_manager
from app.storage.models import Candle, Direction


@dataclass
class TradeResult:
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
    exit_reason: str  # T1_BE, T2_LOCK, T3_FULL, SL, EOD
    pnl_points: float
    pnl_pct: float
    target_1_hit: bool
    target_2_hit: bool
    target_3_hit: bool
    max_favorable_points: float
    max_adverse_points: float


class IndexPivotBacktester:
    """Deterministic Backtester for the exact strategy specified by the user."""

    def __init__(self):
        self.indices = [
            ("13", "NIFTY"),
            ("25", "BANKNIFTY"),
            ("51", "SENSEX"),
        ]

    async def get_trading_dates(self, lookback_days: int = 25) -> List[date]:
        """Discovers available trading dates from historical API."""
        dates = []
        cur = date(2026, 10, 8)
        while len(dates) < lookback_days and cur > date(2026, 8, 1):
            if cur.weekday() < 5:
                dates.append(cur)
            cur -= timedelta(days=1)

        valid_dates = []
        for d in dates:
            c = await historical_manager.fetch_intraday_candles("13", "NIFTY", d, interval=5)
            if len(c) >= 50:
                valid_dates.append(d)
        valid_dates.sort()
        return valid_dates

    async def run_backtest(self) -> List[TradeResult]:
        trading_dates = await self.get_trading_dates(lookback_days=25)
        print(f"\n=================================================================")
        print(f"  RUNNING INDEX ORB (09:30-10:00) + TRADITIONAL PIVOTS BACKTEST  ")
        print(f"  Total Trading Sessions: {len(trading_dates)} days                ")
        print(f"  Date Range: {trading_dates[0]} to {trading_dates[-1]}          ")
        print(f"=================================================================\n")

        all_trades: List[TradeResult] = []

        for t_date in trading_dates:
            for sec_id, sym in self.indices:
                trade = await self._evaluate_day_symbol(sec_id, sym, t_date)
                if trade:
                    all_trades.append(trade)

        return all_trades

    async def _evaluate_day_symbol(
        self, sec_id: str, sym: str, t_date: date
    ) -> Optional[TradeResult]:
        candles = await historical_manager.fetch_intraday_candles(
            security_id=sec_id, symbol=sym, trade_date=t_date, interval=5
        )
        if len(candles) < 30:
            return None

        # 1. Traditional Floor Pivots calculation from prior day
        pivots = await pivot_engine.get_index_pivots(sec_id, sym, t_date)
        if not pivots:
            return None

        # 2. Extract benchmark range from Candle 2 & 3 (09:30 to 10:00 IST)
        # 5m candle start times: 09:30, 09:35, 09:40, 09:45, 09:50, 09:55
        range_candles = []
        post_10am_candles = []

        for c in candles:
            t = c.timestamp.time()
            if t >= datetime.strptime("09:30:00", "%H:%M:%S").time() and t < datetime.strptime("10:00:00", "%H:%M:%S").time():
                range_candles.append(c)
            elif t >= datetime.strptime("10:00:00", "%H:%M:%S").time():
                post_10am_candles.append(c)

        if len(range_candles) < 4:
            return None

        range_high = max(c.high for c in range_candles)
        range_low = min(c.low for c in range_candles)
        range_mid = round((range_high + range_low) / 2.0, 2)

        # 3. Scan for Breakout + Next Candle Confirmation
        for i in range(len(post_10am_candles) - 1):
            c_break = post_10am_candles[i]
            c_next = post_10am_candles[i + 1]

            # Don't take fresh entries after 14:30
            if c_break.timestamp.time() > datetime.strptime("14:30:00", "%H:%M:%S").time():
                break

            # Check Bullish (LONG) Breakout
            if c_break.close > range_high:
                # Next candle opens in same direction and breaks high of breakout candle
                same_dir_open = c_next.open >= (range_high * 0.9995)
                confirmed_break = c_next.high > c_break.high

                if same_dir_open and confirmed_break:
                    entry_price = max(c_next.open, c_break.high + 0.5)
                    sl = range_mid  # Midpoint Support
                    is_ok, _, actual_sl, t1, t2, t3 = pivot_engine.determine_targets_and_sl(
                        symbol=sym,
                        direction=Direction.LONG,
                        entry_price=entry_price,
                        orb_high=range_high,
                        orb_low=range_low,
                        orb_mid=range_mid,
                        pivots=pivots,
                    )
                    if not is_ok:
                        continue

                    # Simulate trade execution forward
                    remaining_candles = post_10am_candles[i + 1:]
                    return self._simulate_trade(
                        t_date, sym, Direction.LONG, c_next.timestamp,
                        entry_price, actual_sl, t1, t2, t3, remaining_candles
                    )

            # Check Bearish (SHORT) Breakdown
            elif c_break.close < range_low:
                # Next candle opens in same direction and breaks low of breakout candle
                same_dir_open = c_next.open <= (range_low * 1.0005)
                confirmed_break = c_next.low < c_break.low

                if same_dir_open and confirmed_break:
                    entry_price = min(c_next.open, c_break.low - 0.5)
                    sl = range_mid  # Midpoint Resistance
                    is_ok, _, actual_sl, t1, t2, t3 = pivot_engine.determine_targets_and_sl(
                        symbol=sym,
                        direction=Direction.SHORT,
                        entry_price=entry_price,
                        orb_high=range_high,
                        orb_low=range_low,
                        orb_mid=range_mid,
                        pivots=pivots,
                    )
                    if not is_ok:
                        continue

                    # Simulate trade execution forward
                    remaining_candles = post_10am_candles[i + 1:]
                    return self._simulate_trade(
                        t_date, sym, Direction.SHORT, c_next.timestamp,
                        entry_price, actual_sl, t1, t2, t3, remaining_candles
                    )

        return None

    def _simulate_trade(
        self,
        t_date: date,
        symbol: str,
        direction: Direction,
        entry_time: datetime,
        entry_price: float,
        initial_sl: float,
        t1: float,
        t2: float,
        t3: float,
        candles: List[Candle],
    ) -> TradeResult:
        current_sl = initial_sl
        t1_hit = False
        t2_hit = False
        t3_hit = False
        mfe = 0.0
        mae = 0.0

        for c in candles:
            # Update MFE / MAE
            if direction == Direction.LONG:
                mfe = max(mfe, c.high - entry_price)
                mae = max(mae, entry_price - c.low)
            else:
                mfe = max(mfe, entry_price - c.low)
                mae = max(mae, c.high - entry_price)

            # 1. Stop Loss check first (Conservative worst-case)
            if direction == Direction.LONG and c.low <= current_sl:
                exit_price = current_sl
                pnl = exit_price - entry_price
                reason = "T2_LOCK" if t2_hit else ("T1_BE" if t1_hit else "SL")
                return TradeResult(
                    t_date, symbol, direction, entry_time, entry_price, initial_sl,
                    t1, t2, t3, c.timestamp, exit_price, reason, pnl,
                    round((pnl / entry_price) * 100, 2), t1_hit, t2_hit, t3_hit, mfe, mae
                )
            elif direction == Direction.SHORT and c.high >= current_sl:
                exit_price = current_sl
                pnl = entry_price - exit_price
                reason = "T2_LOCK" if t2_hit else ("T1_BE" if t1_hit else "SL")
                return TradeResult(
                    t_date, symbol, direction, entry_time, entry_price, initial_sl,
                    t1, t2, t3, c.timestamp, exit_price, reason, pnl,
                    round((pnl / entry_price) * 100, 2), t1_hit, t2_hit, t3_hit, mfe, mae
                )

            # 2. Target 3 check (Full Target Exit)
            if direction == Direction.LONG and c.high >= t3:
                t3_hit = True
                pnl = t3 - entry_price
                return TradeResult(
                    t_date, symbol, direction, entry_time, entry_price, initial_sl,
                    t1, t2, t3, c.timestamp, t3, "T3_FULL", pnl,
                    round((pnl / entry_price) * 100, 2), True, True, True, mfe, mae
                )
            elif direction == Direction.SHORT and c.low <= t3:
                t3_hit = True
                pnl = entry_price - t3
                return TradeResult(
                    t_date, symbol, direction, entry_time, entry_price, initial_sl,
                    t1, t2, t3, c.timestamp, t3, "T3_FULL", pnl,
                    round((pnl / entry_price) * 100, 2), True, True, True, mfe, mae
                )

            # 3. Target 2 check (Trail SL to T1)
            if not t2_hit:
                if (direction == Direction.LONG and c.high >= t2) or (direction == Direction.SHORT and c.low <= t2):
                    t2_hit = True
                    t1_hit = True
                    current_sl = t1

            # 4. Target 1 check (Trail SL to Break-Even Entry)
            if not t1_hit:
                if (direction == Direction.LONG and c.high >= t1) or (direction == Direction.SHORT and c.low <= t1):
                    t1_hit = True
                    current_sl = entry_price

            # 5. EOD Market Close (15:15 IST)
            if c.timestamp.time() >= datetime.strptime("15:15:00", "%H:%M:%S").time():
                exit_price = c.close
                pnl = (exit_price - entry_price) if direction == Direction.LONG else (entry_price - exit_price)
                return TradeResult(
                    t_date, symbol, direction, entry_time, entry_price, initial_sl,
                    t1, t2, t3, c.timestamp, exit_price, "EOD", pnl,
                    round((pnl / entry_price) * 100, 2), t1_hit, t2_hit, t3_hit, mfe, mae
                )

        # Default close at last candle
        last_c = candles[-1]
        pnl = (last_c.close - entry_price) if direction == Direction.LONG else (entry_price - last_c.close)
        return TradeResult(
            t_date, symbol, direction, entry_time, entry_price, initial_sl,
            t1, t2, t3, last_c.timestamp, last_c.close, "EOD", pnl,
            round((pnl / entry_price) * 100, 2), t1_hit, t2_hit, t3_hit, mfe, mae
        )


async def main():
    tester = IndexPivotBacktester()
    results = await tester.run_backtest()

    if not results:
        print("No trades generated during the backtest period.")
        return

    # Aggregate Statistics
    total_trades = len(results)
    wins = [r for r in results if r.pnl_points > 0]
    be_trades = [r for r in results if r.pnl_points == 0]
    losses = [r for r in results if r.pnl_points < 0]

    t1_hits = [r for r in results if r.target_1_hit]
    t2_hits = [r for r in results if r.target_2_hit]
    t3_hits = [r for r in results if r.target_3_hit]

    gross_profit = sum(r.pnl_points for r in wins)
    gross_loss = abs(sum(r.pnl_points for r in losses))
    net_pnl = gross_profit - gross_loss
    profit_factor = round(gross_profit / max(1.0, gross_loss), 2)
    win_rate = round((len(wins) / total_trades) * 100.0, 1)

    # Print Detailed Trade Log
    print(f"{'Date':<12} {'Symbol':<10} {'Dir':<6} {'Entry':<10} {'SL':<10} {'T1':<10} {'T2':<10} {'Exit':<10} {'Exit Reason':<12} {'P&L (pts)':<12} {'P&L %':<8}")
    print("-" * 115)
    for r in results:
        dir_str = "BUY" if r.direction == Direction.LONG else "SELL"
        print(
            f"{str(r.trade_date):<12} {r.symbol:<10} {dir_str:<6} "
            f"{r.entry_price:<10.2f} {r.initial_sl:<10.2f} {r.target_1:<10.2f} {r.target_2:<10.2f} "
            f"{r.exit_price:<10.2f} {r.exit_reason:<12} {r.pnl_points:<+12.2f} {r.pnl_pct:<+8.2f}%"
        )

    print("\n" + "=" * 65)
    print("                 FINAL PERFORMANCE METRICS                       ")
    print("=" * 65)
    print(f"  Total Trades Executed   : {total_trades}")
    print(f"  Winning Trades          : {len(wins)} ({win_rate}%)")
    print(f"  Break-Even / Zero-Loss  : {len(be_trades)} ({round(len(be_trades)/total_trades*100, 1)}%)")
    print(f"  Losing Trades           : {len(losses)} ({round(len(losses)/total_trades*100, 1)}%)")
    print(f"  -------------------------------------------------------------")
    print(f"  Target 1 Hit Rate       : {len(t1_hits)} / {total_trades} ({round(len(t1_hits)/total_trades*100, 1)}%)")
    print(f"  Target 2 Hit Rate       : {len(t2_hits)} / {total_trades} ({round(len(t2_hits)/total_trades*100, 1)}%)")
    print(f"  Target 3 Hit Rate       : {len(t3_hits)} / {total_trades} ({round(len(t3_hits)/total_trades*100, 1)}%)")
    print(f"  -------------------------------------------------------------")
    print(f"  Gross Profit (Points)   : +{gross_profit:,.2f} pts")
    print(f"  Gross Loss (Points)     : -{gross_loss:,.2f} pts")
    print(f"  Net P&L (Points)        : {net_pnl:+,.2f} pts")
    print(f"  Profit Factor           : {profit_factor}")
    print(f"  Capital Protection Rate : {round((len(wins) + len(be_trades))/total_trades*100, 1)}% (Zero Capital Loss)")
    print("=" * 65)

    # Per-Symbol breakdown
    print("\nPER-INDEX BREAKDOWN:")
    for _, sym in [("13", "NIFTY"), ("25", "BANKNIFTY"), ("51", "SENSEX")]:
        sym_trades = [r for r in results if r.symbol == sym]
        if sym_trades:
            s_wins = [r for r in sym_trades if r.pnl_points > 0]
            s_losses = [r for r in sym_trades if r.pnl_points < 0]
            s_net = sum(r.pnl_points for r in sym_trades)
            s_wr = round(len(s_wins) / len(sym_trades) * 100, 1)
            print(f"  • {sym:<10}: {len(sym_trades)} trades | Win Rate: {s_wr}% | Net P&L: {s_net:+,.2f} pts")


if __name__ == "__main__":
    asyncio.run(main())
