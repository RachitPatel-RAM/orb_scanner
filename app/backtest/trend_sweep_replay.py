"""
Deterministic Historical Replay and Performance Evaluation for TREND_SWEEP_FVG_V1.

Evaluates and compares:
1. FIXED_2R: Structural stop and net-2R target; full exit.
2. BE_TRAIL_2R: Move to BE at +1R, trail behind confirmed 5m swing pivots.

Maintains strict event-driven sequencing, no lookahead bias, conservative fill modeling,
and full statutory cost deductions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from app.config import logger
from app.strategies.trend_sweep_fvg import (
    Candle,
    ExitVersion,
    PositionState,
    SetupCandidate,
    SetupStatus,
    TrendSweepFVGStrategy,
)
from app.trading.risk_manager_v2 import RiskManagerV2, StatutoryCosts


@dataclass
class ReplayTradeRecord:
    trade_id: str
    signal_id: str
    symbol: str
    direction: str
    exit_version: str
    entry_price: float
    entry_time: str
    initial_stop: float
    target_price: float
    quantity: int
    exit_price: float
    exit_time: str
    exit_reason: str
    gross_pnl: float
    total_charges: float
    net_pnl: float
    realized_r: float
    target_hit: bool
    win: bool


@dataclass
class StrategyComparisonSummary:
    exit_version: str
    total_setups_detected: int = 0
    armed_setups: int = 0
    expired_setups: int = 0
    filled_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    breakeven_trades: int = 0
    net_win_rate_pct: float = 0.0
    target_hit_rate_pct: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    total_charges: float = 0.0
    net_pnl: float = 0.0
    average_win: float = 0.0
    average_loss: float = 0.0
    profit_factor: float = 0.0
    expectancy_per_trade: float = 0.0
    max_drawdown_amount: float = 0.0
    max_drawdown_pct: float = 0.0
    consecutive_losses: int = 0
    trades: List[ReplayTradeRecord] = field(default_factory=list)


class TrendSweepReplayRunner:
    """
    Simulates event-driven candle-by-candle replay of 1m/5m/15m/60m bars across historical sessions.
    Compares FIXED_2R vs BE_TRAIL_2R on identical candidate entries.
    """

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        initial_capital: float = 50000.0,
        risk_per_trade_pct: float = 0.25,
    ):
        self.initial_capital = initial_capital
        self.risk_per_trade_pct = risk_per_trade_pct
        self.config = config or {}

    def run_replay(
        self,
        symbol: str,
        security_id: str,
        candles_5m: List[Dict[str, Any]],
        candles_15m: List[Dict[str, Any]],
        candles_60m: List[Dict[str, Any]],
        instrument_meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, StrategyComparisonSummary]:
        """
        Runs replay simultaneously for FIXED_2R and BE_TRAIL_2R.
        """
        meta = instrument_meta or {
            "symbol": symbol,
            "security_id": security_id,
            "lot_size": 1,
            "tick_size": 0.05,
            "product_type": "INTRADAY",
        }

        # Run two separate strategy instances with respective exit policies
        strategy_fixed = TrendSweepFVGStrategy(
            symbol=symbol,
            security_id=security_id,
            config=self.config,
            exit_version=ExitVersion.FIXED_2R,
        )
        strategy_trail = TrendSweepFVGStrategy(
            symbol=symbol,
            security_id=security_id,
            config=self.config,
            exit_version=ExitVersion.BE_TRAIL_2R,
        )

        # Convert dict candles to Candle objects
        c5 = [Candle.from_dict(c) for c in candles_5m]
        c15 = [Candle.from_dict(c) for c in candles_15m]
        c60 = [Candle.from_dict(c) for c in candles_60m]

        trades_fixed: List[ReplayTradeRecord] = []
        trades_trail: List[ReplayTradeRecord] = []

        # Stream 5m candles chronologically
        for i, bar in enumerate(c5):
            curr_time = bar.timestamp
            # Slice completed 15m and 60m bars strictly up to curr_time
            hist_15 = [c for c in c15 if c.timestamp <= curr_time]
            hist_60 = [c for c in c60 if c.timestamp <= curr_time]
            hist_5 = c5[: i + 1]

            # Process bar for Fixed strategy
            self._step_strategy(strategy_fixed, bar, hist_5, hist_15, hist_60, trades_fixed, meta)

            # Process bar for Trail strategy
            self._step_strategy(strategy_trail, bar, hist_5, hist_15, hist_60, trades_trail, meta)

        # Compute summary metrics for both
        summary_fixed = self._compute_summary("FIXED_2R", strategy_fixed, trades_fixed)
        summary_trail = self._compute_summary("BE_TRAIL_2R", strategy_trail, trades_trail)

        return {
            "FIXED_2R": summary_fixed,
            "BE_TRAIL_2R": summary_trail,
        }

    def _step_strategy(
        self,
        strategy: TrendSweepFVGStrategy,
        bar: Candle,
        hist_5: List[Candle],
        hist_15: List[Candle],
        hist_60: List[Candle],
        trade_log: List[ReplayTradeRecord],
        meta: Dict[str, Any],
    ) -> None:
        """Executes one step in the event-driven strategy and records closed trades."""
        pos_before = strategy.position
        cand_before = strategy.active_candidate

        # Advance strategy state
        strategy.on_5m_candle(bar, hist_5, hist_15, hist_60)

        # If a position just closed
        if pos_before is not None and strategy.position is None:
            # Position was closed in this step
            p = pos_before
            gross_pnl = (p.exit_price - p.entry_price) * p.quantity if p.direction == "LONG" else (p.entry_price - p.exit_price) * p.quantity
            charges = RiskManagerV2.calculate_statutory_charges(
                entry_price=p.entry_price,
                exit_price=p.exit_price or p.entry_price,
                quantity=p.quantity,
                product_type=meta.get("product_type", "INTRADAY"),
            ).total_charges
            net_pnl = gross_pnl - charges
            risk_amt = abs(p.entry_price - p.initial_stop) * p.quantity
            realized_r = (net_pnl / risk_amt) if risk_amt > 0 else 0.0

            target_hit = p.exit_reason == "TARGET"
            trade_log.append(
                ReplayTradeRecord(
                    trade_id=f"TR_{p.signal_id}",
                    signal_id=p.signal_id,
                    symbol=strategy.symbol,
                    direction=p.direction,
                    exit_version=strategy.exit_version.value,
                    entry_price=p.entry_price,
                    entry_time=p.entry_time,
                    initial_stop=p.initial_stop,
                    target_price=p.target_price,
                    quantity=p.quantity,
                    exit_price=p.exit_price or p.entry_price,
                    exit_time=p.exit_time or bar.timestamp,
                    exit_reason=p.exit_reason or "UNKNOWN",
                    gross_pnl=round(gross_pnl, 2),
                    total_charges=round(charges, 2),
                    net_pnl=round(net_pnl, 2),
                    realized_r=round(realized_r, 2),
                    target_hit=target_hit,
                    win=net_pnl > 0,
                )
            )

    def _compute_summary(
        self,
        exit_version: str,
        strategy: TrendSweepFVGStrategy,
        trades: List[ReplayTradeRecord],
    ) -> StrategyComparisonSummary:
        """Calculates comprehensive quantitative backtest metrics."""
        summary = StrategyComparisonSummary(exit_version=exit_version, trades=trades)

        # Funnel counts from strategy history
        summary.total_setups_detected = len(strategy.all_setups)
        summary.armed_setups = sum(1 for s in strategy.all_setups if s.status in [SetupStatus.ARMED_WAIT_RETEST, SetupStatus.TRIGGER_OBSERVED, SetupStatus.FILLED, SetupStatus.CLOSED])
        summary.expired_setups = sum(1 for s in strategy.all_setups if s.status == SetupStatus.EXPIRED)
        summary.filled_trades = len(trades)

        if not trades:
            return summary

        wins = [t for t in trades if t.net_pnl > 0]
        losses = [t for t in trades if t.net_pnl < 0]
        breakevens = [t for t in trades if t.net_pnl == 0]

        summary.winning_trades = len(wins)
        summary.losing_trades = len(losses)
        summary.breakeven_trades = len(breakevens)

        summary.net_win_rate_pct = round((len(wins) / len(trades)) * 100.0, 2)
        summary.target_hit_rate_pct = round((sum(1 for t in trades if t.target_hit) / len(trades)) * 100.0, 2)

        summary.gross_profit = round(sum(t.gross_pnl for t in wins), 2)
        summary.gross_loss = round(abs(sum(t.gross_pnl for t in losses)), 2)
        summary.total_charges = round(sum(t.total_charges for t in trades), 2)
        summary.net_pnl = round(sum(t.net_pnl for t in trades), 2)

        summary.average_win = round(float(np.mean([t.net_pnl for t in wins])), 2) if wins else 0.0
        summary.average_loss = round(float(abs(np.mean([t.net_pnl for t in losses]))), 2) if losses else 0.0

        if summary.gross_loss > 0:
            summary.profit_factor = round(summary.gross_profit / summary.gross_loss, 2)
        else:
            summary.profit_factor = round(summary.gross_profit, 2) if summary.gross_profit > 0 else 0.0

        p_win = len(wins) / len(trades)
        p_loss = len(losses) / len(trades)
        summary.expectancy_per_trade = round((p_win * summary.average_win) - (p_loss * summary.average_loss), 2)

        # Max drawdown
        cum_pnl = np.cumsum([t.net_pnl for t in trades])
        equity = self.initial_capital + cum_pnl
        peak = np.maximum.accumulate(equity)
        drawdown = peak - equity
        summary.max_drawdown_amount = round(float(np.max(drawdown)), 2) if len(drawdown) > 0 else 0.0
        summary.max_drawdown_pct = round(float(np.max((drawdown / peak) * 100.0)), 2) if len(drawdown) > 0 else 0.0

        # Max consecutive losses
        curr_streak = 0
        max_streak = 0
        for t in trades:
            if t.net_pnl < 0:
                curr_streak += 1
                max_streak = max(max_streak, curr_streak)
            else:
                curr_streak = 0
        summary.consecutive_losses = max_streak

        return summary
