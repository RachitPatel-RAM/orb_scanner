"""
Historical Backtest Simulation Engine.

Simulates historical days with:
- ZERO look-ahead bias (strict candle-close sequencing)
- Exact shared ORBStrategy rules
- Realistic Indian regulatory costs (brokerage, STT, turnover, GST, stamp duty, slippage)
- Conservative intra-candle tie-breaking policy
"""

from __future__ import annotations

from datetime import datetime, date, timedelta
from typing import Any, Dict, List, Optional
import pandas as pd

from app.config import BacktestCosts, StrategyConfig, logger, settings
from app.market.candle_builder import CandleBuilder
from app.market.session import default_session, IST_TZ
from app.storage.models import Candle, Direction, ExitReason, Signal
from app.strategies.orb import ORBStrategy
from app.trading.paper_tracker import PaperTracker, calculate_trade_costs


class BacktestEngine:
    """Runs high-fidelity event-driven backtest across historical candles."""

    def __init__(
        self,
        strategy_config: Optional[StrategyConfig] = None,
        costs: Optional[BacktestCosts] = None,
        debug_mode: bool = False,
    ):
        self.strategy_config = strategy_config or settings.strategy
        self.costs = costs or self.strategy_config.costs
        self.debug_mode = debug_mode

    def run_day(
        self,
        trade_date: date,
        day_candles_by_symbol: Dict[str, List[Candle]],
    ) -> List[Dict[str, Any]]:
        """
        Simulates a single trading day across all symbols.
        Guarantees strict temporal ordering without future leakage.
        """
        # Create fresh strategy and tracker for the day
        strategy = ORBStrategy(config=self.strategy_config, debug_mode=self.debug_mode)
        strategy.reset_day(trade_date)

        tracker = PaperTracker(costs=self.costs)
        executed_trades: List[Dict[str, Any]] = []

        def on_trade_closed(trade, exit_reason: ExitReason):
            # Calculate regulatory costs
            cost_details = calculate_trade_costs(
                entry_price=trade.entry_price,
                exit_price=trade.exit_price or trade.entry_price,
                quantity=1,
                direction=trade.direction,
                costs=self.costs,
            )
            net_pnl = (trade.pnl or 0.0) - cost_details.total_charges

            record = {
                "trade_date": trade.trade_date.isoformat(),
                "security_id": trade.security_id,
                "symbol": trade.symbol,
                "direction": trade.direction.value,
                "entry_time": trade.entry_time.strftime("%H:%M"),
                "entry_price": round(trade.entry_price, 2),
                "stop_loss": round(trade.stop_loss, 2),
                "target": round(trade.target, 2),
                "exit_time": trade.exit_time.strftime("%H:%M") if trade.exit_time else "EOD",
                "exit_price": round(trade.exit_price or 0.0, 2),
                "exit_reason": exit_reason.value,
                "pnl": round(trade.pnl or 0.0, 2),
                "charges": round(cost_details.total_charges, 2),
                "net_pnl": round(net_pnl, 2),
                "r_multiple": round(trade.r_multiple or 0.0, 2),
            }
            executed_trades.append(record)

        tracker.on_target_hit = lambda t: on_trade_closed(t, ExitReason.TARGET)
        tracker.on_stop_hit = lambda t: on_trade_closed(t, ExitReason.STOP_LOSS)
        tracker.on_eod_squareoff = lambda t: on_trade_closed(t, ExitReason.EOD)

        # Merge all 1m candles across symbols in chronological order
        all_1m_events: List[Candle] = []
        for sym, candles in day_candles_by_symbol.items():
            all_1m_events.extend(candles)

        all_1m_events.sort(key=lambda c: (c.timestamp, c.symbol))

        # We build 5m candles deterministically from 1m candles
        symbol_1m_accum: Dict[str, List[Candle]] = {}

        for c_1m in all_1m_events:
            sec_id = c_1m.security_id
            c_time = default_session.localize(c_1m.timestamp)

            # 1. Update existing open trades with this 1m bar (evaluates SL/target/EOD)
            tracker.update_with_candle(c_1m)

            # 2. Accumulate 1m candle into 5m bucket
            if sec_id not in symbol_1m_accum:
                symbol_1m_accum[sec_id] = []
            symbol_1m_accum[sec_id].append(c_1m)

            # 3. Check if 5m boundary is reached (minutes ending in 4 or 9)
            if c_1m.timestamp.minute % 5 == 4:
                bucket = symbol_1m_accum[sec_id]
                symbol_1m_accum[sec_id] = []

                if bucket:
                    five_m_start = bucket[0].timestamp.replace(second=0, microsecond=0)
                    # Floor to 5 min
                    m = (five_m_start.minute // 5) * 5
                    five_m_start = five_m_start.replace(minute=m)

                    c_5m = Candle(
                        security_id=sec_id,
                        symbol=bucket[0].symbol,
                        timestamp=five_m_start,
                        open=bucket[0].open,
                        high=max(c.high for c in bucket),
                        low=min(c.low for c in bucket),
                        close=bucket[-1].close,
                        volume=sum(c.volume for c in bucket),
                        is_closed=True,
                    )

                    # Check for ORB Signal on closed 5m candle
                    signal = strategy.on_5m_candle_closed(c_5m)
                    if signal:
                        # Open new paper trade
                        tracker.open_trade_from_signal(signal)

        # Force EOD close for any remaining open trades at market close price
        if tracker.open_trades:
            close_time = datetime.combine(trade_date, default_session.market_close_time, tzinfo=IST_TZ)
            for trade in list(tracker.open_trades.values()):
                # Find last available price
                last_candles = day_candles_by_symbol.get(trade.symbol, [])
                last_price = last_candles[-1].close if last_candles else trade.entry_price
                tracker._close_trade(trade, last_price, close_time, ExitReason.EOD)
                on_trade_closed(trade, ExitReason.EOD)

        return executed_trades
