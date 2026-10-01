"""
Quantitative Backtest Metrics Calculator.

Computes comprehensive risk, return, streak, and attribution metrics:
- Trading days, trade counts (long/short, win/loss)
- Win rate, gross/net PnL, profit factor, expectancy, average R
- Max Drawdown, max consecutive wins/losses
- Stock-wise, monthly, and daily breakdowns
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional
import pandas as pd
import numpy as np


@dataclass
class BacktestSummaryMetrics:
    trading_days: int = 0
    total_trades: int = 0
    long_trades: int = 0
    short_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    breakeven_trades: int = 0
    win_rate: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    net_pnl: float = 0.0
    total_charges: float = 0.0
    average_win: float = 0.0
    average_loss: float = 0.0
    profit_factor: float = 0.0
    expectancy: float = 0.0
    average_r: float = 0.0
    max_drawdown_amount: float = 0.0
    max_drawdown_pct: float = 0.0
    consecutive_wins: int = 0
    consecutive_losses: int = 0
    monthly_pnl: Dict[str, float] = field(default_factory=dict)
    stock_pnl: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    daily_pnl: Dict[str, float] = field(default_factory=dict)


def calculate_metrics(trades: List[Dict[str, Any]], initial_capital: float = 100000.0) -> BacktestSummaryMetrics:
    """Computes comprehensive quantitative performance analytics from executed trade records."""
    if not trades:
        return BacktestSummaryMetrics()

    df = pd.DataFrame(trades)

    # Ensure required columns
    for col in ["pnl", "net_pnl", "r_multiple", "charges"]:
        if col not in df.columns:
            df[col] = df.get("pnl", 0.0)

    total_trades = len(df)
    long_trades = int((df["direction"] == "LONG").sum()) if "direction" in df.columns else 0
    short_trades = int((df["direction"] == "SHORT").sum()) if "direction" in df.columns else 0

    net_pnls = df["net_pnl"].values
    wins = df[df["net_pnl"] > 0]
    losses = df[df["net_pnl"] < 0]
    breakevens = df[df["net_pnl"] == 0]

    winning_trades = len(wins)
    losing_trades = len(losses)
    breakeven_trades = len(breakevens)

    win_rate = (winning_trades / total_trades * 100.0) if total_trades > 0 else 0.0

    gross_profit = float(wins["pnl"].sum()) if winning_trades > 0 else 0.0
    gross_loss = float(abs(losses["pnl"].sum())) if losing_trades > 0 else 0.0
    total_charges = float(df["charges"].sum()) if "charges" in df.columns else 0.0
    net_pnl = float(df["net_pnl"].sum())

    avg_win = float(wins["net_pnl"].mean()) if winning_trades > 0 else 0.0
    avg_loss = float(abs(losses["net_pnl"].mean())) if losing_trades > 0 else 0.0

    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (gross_profit if gross_profit > 0 else 0.0)
    avg_r = float(df["r_multiple"].mean()) if "r_multiple" in df.columns else 0.0

    # Expectancy: (Win% * AvgWin) - (Loss% * AvgLoss)
    p_win = winning_trades / total_trades if total_trades > 0 else 0.0
    p_loss = losing_trades / total_trades if total_trades > 0 else 0.0
    expectancy = (p_win * avg_win) - (p_loss * avg_loss)

    # Max Drawdown calculation
    equity_curve = initial_capital + np.cumsum(net_pnls)
    peak = np.maximum.accumulate(equity_curve)
    drawdowns = peak - equity_curve
    max_dd_amount = float(np.max(drawdowns)) if len(drawdowns) > 0 else 0.0
    max_dd_pct = float(np.max(drawdowns / peak * 100.0)) if len(drawdowns) > 0 else 0.0

    # Streaks calculation
    max_consec_wins = 0
    max_consec_losses = 0
    cur_wins = 0
    cur_losses = 0

    for p in net_pnls:
        if p > 0:
            cur_wins += 1
            cur_losses = 0
            max_consec_wins = max(max_consec_wins, cur_wins)
        elif p < 0:
            cur_losses += 1
            cur_wins = 0
            max_consec_losses = max(max_consec_losses, cur_losses)
        else:
            cur_wins = 0
            cur_losses = 0

    # Dates and Daily PnL
    trading_days = 0
    daily_pnl_map: Dict[str, float] = {}
    monthly_pnl_map: Dict[str, float] = {}

    if "trade_date" in df.columns:
        df["trade_date_str"] = df["trade_date"].astype(str)
        trading_days = df["trade_date_str"].nunique()
        day_grouped = df.groupby("trade_date_str")["net_pnl"].sum()
        daily_pnl_map = {str(k): round(float(v), 2) for k, v in day_grouped.items()}

        df["month_str"] = df["trade_date_str"].str.slice(0, 7)
        month_grouped = df.groupby("month_str")["net_pnl"].sum()
        monthly_pnl_map = {str(k): round(float(v), 2) for k, v in month_grouped.items()}

    # Stock-wise attribution
    stock_pnl_map: Dict[str, Dict[str, Any]] = {}
    if "symbol" in df.columns:
        for sym, grp in df.groupby("symbol"):
            sym_total = len(grp)
            sym_wins = int((grp["net_pnl"] > 0).sum())
            sym_win_rate = (sym_wins / sym_total * 100.0) if sym_total > 0 else 0.0
            sym_net = float(grp["net_pnl"].sum())
            stock_pnl_map[sym] = {
                "trades": sym_total,
                "win_rate": round(sym_win_rate, 1),
                "net_pnl": round(sym_net, 2),
                "avg_r": round(float(grp["r_multiple"].mean()), 2) if "r_multiple" in grp.columns else 0.0,
            }

    return BacktestSummaryMetrics(
        trading_days=trading_days,
        total_trades=total_trades,
        long_trades=long_trades,
        short_trades=short_trades,
        winning_trades=winning_trades,
        losing_trades=losing_trades,
        breakeven_trades=breakeven_trades,
        win_rate=round(win_rate, 2),
        gross_profit=round(gross_profit, 2),
        gross_loss=round(gross_loss, 2),
        net_pnl=round(net_pnl, 2),
        total_charges=round(total_charges, 2),
        average_win=round(avg_win, 2),
        average_loss=round(avg_loss, 2),
        profit_factor=round(profit_factor, 2),
        expectancy=round(expectancy, 2),
        average_r=round(avg_r, 2),
        max_drawdown_amount=round(max_dd_amount, 2),
        max_drawdown_pct=round(max_dd_pct, 2),
        consecutive_wins=max_consec_wins,
        consecutive_losses=max_consec_losses,
        monthly_pnl=monthly_pnl_map,
        stock_pnl=stock_pnl_map,
        daily_pnl=daily_pnl_map,
    )
