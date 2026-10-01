"""
Opening Range Breakout (ORB) Backtest Orchestrator.

Runs historical simulation across universes, computes performance metrics,
generates CSV and HTML reports, and logs results to SQLite.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, date, timedelta
import json
from pathlib import Path
from typing import Any, Dict, List, Optional
import pandas as pd
from tabulate import tabulate

from app.backtest.downloader import batch_downloader
from app.backtest.engine import BacktestEngine
from app.backtest.metrics import BacktestSummaryMetrics, calculate_metrics
from app.config import BacktestCosts, StrategyConfig, logger, settings, REPORTS_DIR
from app.dhan.historical import historical_manager
from app.dhan.instruments import InstrumentInfo, instrument_manager
from app.storage.database import db
from app.storage.models import Candle


class ORBBacktester:
    """End-to-end backtest workflow manager."""

    def __init__(
        self,
        strategy_config: Optional[StrategyConfig] = None,
        costs: Optional[BacktestCosts] = None,
        debug_mode: bool = False,
    ):
        self.strategy_config = strategy_config or settings.strategy
        self.costs = costs or self.strategy_config.costs
        self.debug_mode = debug_mode
        self.engine = BacktestEngine(
            strategy_config=self.strategy_config,
            costs=self.costs,
            debug_mode=self.debug_mode,
        )

    async def run(
        self,
        from_date: date,
        to_date: date,
        universe_mode: str = "nifty50",
        custom_symbols: Optional[List[str]] = None,
        force_download: bool = False,
    ) -> BacktestSummaryMetrics:
        """Executes full backtest workflow across date range and universe."""
        logger.info(
            f"Initializing Backtest: Strategy={self.strategy_config.name}, "
            f"Range={from_date} to {to_date}, Universe={universe_mode}"
        )

        # 1. Resolve instruments
        if custom_symbols:
            instruments: List[InstrumentInfo] = []
            for s in custom_symbols:
                sec_id = instrument_manager.get_security_id(s)
                if sec_id:
                    info = instrument_manager.get_instrument_info(sec_id)
                    if info:
                        instruments.append(info)
                else:
                    logger.warning(f"Could not resolve symbol '{s}' in instrument master.")
        else:
            instruments = instrument_manager.resolve_universe(universe_mode)

        if not instruments:
            logger.error("No valid instruments resolved for backtest. Aborting.")
            return BacktestSummaryMetrics()

        # 2. Download / load historical candles
        await batch_downloader.download_universe_data(
            instruments=instruments,
            start_date=from_date,
            end_date=to_date,
            interval=1,
            force=force_download,
        )

        # 3. Iterate day by day
        trading_days = batch_downloader.get_trading_days(from_date, to_date)
        all_executed_trades: List[Dict[str, Any]] = []

        for t_day in trading_days:
            # Group 1m candles for today by symbol
            day_candles_by_symbol: Dict[str, List[Candle]] = {}
            for inst in instruments:
                candles = await historical_manager.fetch_intraday_candles(
                    security_id=inst.security_id,
                    symbol=inst.symbol,
                    trade_date=t_day,
                    interval=1,
                    force_download=False,
                )
                if candles:
                    day_candles_by_symbol[inst.symbol] = candles

            if not day_candles_by_symbol:
                continue

            day_trades = self.engine.run_day(t_day, day_candles_by_symbol)
            all_executed_trades.extend(day_trades)

        # 4. Compute metrics
        metrics = calculate_metrics(all_executed_trades)

        # 5. Export Reports
        self._export_reports(all_executed_trades, metrics, from_date, to_date, universe_mode)

        # 6. Save backtest run to database
        self._persist_run(from_date, to_date, universe_mode, metrics)

        # 7. Print Console Summary
        self._print_console_summary(metrics)

        # 8. Send Telegram Report
        try:
            await self.send_telegram_report(metrics, from_date, to_date, universe_mode)
        except Exception as e:
            logger.debug(f"Failed to dispatch Telegram backtest report: {e}")

        return metrics

    async def send_telegram_report(
        self,
        metrics: BacktestSummaryMetrics,
        from_date: date,
        to_date: date,
        universe: str,
        capital: float = 5000.0,
    ) -> bool:
        """Sends rich Telegram backtest report with capital simulation and top performers."""
        from app.notifications.telegram import notifier
        if not notifier.is_configured:
            return False

        # Capital simulation: assuming 1% risk per trade on given capital
        risk_per_trade = capital * 0.01
        sim_pnl = metrics.total_trades * metrics.average_r * risk_per_trade if metrics.total_trades else 0.0
        sim_roi = (sim_pnl / capital) * 100.0 if capital > 0 else 0.0
        sim_dd = (metrics.max_drawdown_pct / 100.0) * capital

        # Top performers
        top_stocks = sorted(metrics.stock_pnl.items(), key=lambda x: x[1]["net_pnl"], reverse=True)[:3]
        top_str = ""
        for i, (sym, d) in enumerate(top_stocks, 1):
            top_str += f"{i}. <b>{sym}</b>: +₹{d['net_pnl']:,.2f} ({d['win_rate']:.0f}% win)\n"

        if not top_str:
            top_str = "None\n"

        text = (
            "📊 <b>ORB HISTORICAL BACKTEST REPORT</b>\n\n"
            f"<b>Strategy:</b> {self.strategy_config.name}\n"
            f"<b>Period:</b> {from_date} to {to_date}\n"
            f"<b>Universe:</b> {universe.upper()} ({metrics.trading_days} Trading Days)\n\n"
            f"<b>Total Trades:</b> {metrics.total_trades}\n"
            f"<b>Win Rate:</b> <b>{metrics.win_rate:.1f}%</b> ({metrics.winning_trades}W / {metrics.losing_trades}L)\n"
            f"<b>Profit Factor:</b> {metrics.profit_factor:.2f}\n"
            f"<b>Average R-Multiple:</b> {metrics.average_r:+.2f}R\n"
            f"<b>Total Net P&L:</b> ₹{metrics.net_pnl:+,.2f}\n\n"
            f"💰 <b>₹{capital:,.0f} Capital Simulation:</b>\n"
            f"• <b>Simulated P&L:</b> +₹{sim_pnl:,.2f} ({sim_roi:+.1f}% ROI)\n"
            f"• <b>Max Drawdown:</b> -₹{sim_dd:,.2f} ({metrics.max_drawdown_pct:.1f}%)\n\n"
            f"🏆 <b>Top Performers:</b>\n"
            f"{top_str}\n"
            f"<i>Full CSV & interactive HTML report generated in reports/</i>"
        )
        return await notifier.send_message(text)

    async def compare_timeframes(
        self,
        from_date: date,
        to_date: date,
        universe_mode: str = "nifty50",
        timeframes: Optional[List[int]] = None,
    ) -> Dict[int, BacktestSummaryMetrics]:
        """
        Runs backtests across 15m, 30m, and 60m candles to compare win rates.
        Dispatches multi-timeframe comparison matrix to Telegram.
        """
        timeframes = timeframes or [15, 30, 60]
        results = {}
        for tf in timeframes:
            cfg = self.strategy_config.model_copy(deep=True)
            cfg.signal_timeframe = tf
            cfg.name = f"ORB-{tf}"
            tester = ORBBacktester(strategy_config=cfg, costs=self.costs, debug_mode=self.debug_mode)
            res = await tester.run(from_date, to_date, universe_mode)
            results[tf] = res

        from app.notifications.telegram import notifier
        if notifier.is_configured:
            rows = ""
            for tf, m in results.items():
                rows += (
                    f"• <b>{tf}m Candle</b>: Win Rate: <b>{m.win_rate:.1f}%</b> | "
                    f"Trades: {m.total_trades} | Profit Factor: {m.profit_factor:.2f} | Avg R: {m.average_r:+.2f}R\n"
                )
            msg = (
                "🔬 <b>ORB TIMEFRAME COMPARISON ANALYSIS</b>\n\n"
                f"<b>Period:</b> {from_date} to {to_date}\n"
                f"<b>Universe:</b> {universe_mode.upper()}\n\n"
                f"{rows}\n"
                "<i>Higher timeframe (30m/60m) produces fewer false breakouts, while 15m gives earlier entries.</i>"
            )
            await notifier.send_message(msg)

        return results

    def _export_reports(
        self,
        trades: List[Dict[str, Any]],
        metrics: BacktestSummaryMetrics,
        from_date: date,
        to_date: date,
        universe: str,
    ) -> None:
        """Exports trades.csv, backtest_summary.csv, monthly.csv, and by_symbol.csv."""
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)

        # 1. trades.csv
        trades_df = pd.DataFrame(trades)
        trades_file = REPORTS_DIR / "trades.csv"
        trades_df.to_csv(trades_file, index=False)

        # 2. backtest_summary.csv
        summary_rows = [
            ("Strategy Name", self.strategy_config.name),
            ("Period", f"{from_date} to {to_date}"),
            ("Universe", universe),
            ("Trading Days", metrics.trading_days),
            ("Total Trades", metrics.total_trades),
            ("Long Trades", metrics.long_trades),
            ("Short Trades", metrics.short_trades),
            ("Winning Trades", metrics.winning_trades),
            ("Losing Trades", metrics.losing_trades),
            ("Breakeven Trades", metrics.breakeven_trades),
            ("Win Rate (%)", metrics.win_rate),
            ("Gross Profit (₹)", metrics.gross_profit),
            ("Gross Loss (₹)", metrics.gross_loss),
            ("Total Regulatory Charges (₹)", metrics.total_charges),
            ("Net P&L (₹)", metrics.net_pnl),
            ("Average Win (₹)", metrics.average_win),
            ("Average Loss (₹)", metrics.average_loss),
            ("Profit Factor", metrics.profit_factor),
            ("Expectancy (₹/trade)", metrics.expectancy),
            ("Average R-Multiple", metrics.average_r),
            ("Max Drawdown (₹)", metrics.max_drawdown_amount),
            ("Max Drawdown (%)", metrics.max_drawdown_pct),
            ("Max Consecutive Wins", metrics.consecutive_wins),
            ("Max Consecutive Losses", metrics.consecutive_losses),
        ]
        summary_df = pd.DataFrame(summary_rows, columns=["Metric", "Value"])
        summary_df.to_csv(REPORTS_DIR / "backtest_summary.csv", index=False)

        # 3. monthly.csv
        if metrics.monthly_pnl:
            monthly_rows = [{"Month": k, "Net P&L": v} for k, v in sorted(metrics.monthly_pnl.items())]
            pd.DataFrame(monthly_rows).to_csv(REPORTS_DIR / "monthly.csv", index=False)

        # 4. by_symbol.csv
        if metrics.stock_pnl:
            stock_rows = [
                {
                    "Symbol": sym,
                    "Trades": data["trades"],
                    "Win Rate (%)": data["win_rate"],
                    "Net P&L (₹)": data["net_pnl"],
                    "Avg R": data["avg_r"],
                }
                for sym, data in sorted(metrics.stock_pnl.items(), key=lambda x: x[1]["net_pnl"], reverse=True)
            ]
            pd.DataFrame(stock_rows).to_csv(REPORTS_DIR / "by_symbol.csv", index=False)

        # 5. report.html (Rich interactive HTML dashboard)
        self._export_html_report(trades_df, summary_df, metrics)

        logger.info(f"Reports successfully generated in: {REPORTS_DIR}")

    def _export_html_report(
        self,
        trades_df: pd.DataFrame,
        summary_df: pd.DataFrame,
        metrics: BacktestSummaryMetrics,
    ) -> None:
        """Generates a modern, clean HTML report for visual backtest review."""
        html_file = REPORTS_DIR / "report.html"
        summary_table_html = summary_df.to_html(classes="styled-table", index=False)
        trades_table_html = (
            trades_df.head(100).to_html(classes="styled-table", index=False) if not trades_df.empty else "<p>No trades executed</p>"
        )

        html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>ORB Strategy Backtest Report - {self.strategy_config.name}</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0f172a; color: #f8fafc; margin: 0; padding: 24px; }}
        h1, h2 {{ color: #38bdf8; }}
        .card {{ background: #1e293b; border-radius: 8px; padding: 20px; margin-bottom: 24px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.2); }}
        .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 24px; }}
        .metric-card {{ background: #334155; border-radius: 6px; padding: 16px; text-align: center; }}
        .metric-title {{ font-size: 0.85rem; color: #94a3b8; text-transform: uppercase; }}
        .metric-val {{ font-size: 1.5rem; font-weight: bold; margin-top: 8px; }}
        .pnl-pos {{ color: #4ade80; }}
        .pnl-neg {{ color: #f87171; }}
        .styled-table {{ width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 0.9rem; }}
        .styled-table th, .styled-table td {{ padding: 10px 14px; text-align: left; border-bottom: 1px solid #334155; }}
        .styled-table th {{ background: #334155; color: #e2e8f0; }}
        .styled-table tr:hover {{ background: #243248; }}
    </style>
</head>
<body>
    <h1>🚀 Opening Range Breakout (ORB) Backtest Report</h1>
    <p>Strategy: <b>{self.strategy_config.name}</b> | Timeframe: <b>{self.strategy_config.signal_timeframe}m</b> | R:R: <b>1:{self.strategy_config.risk.risk_reward}</b></p>
    
    <div class="grid">
        <div class="metric-card">
            <div class="metric-title">Net P&L</div>
            <div class="metric-val {'pnl-pos' if metrics.net_pnl >= 0 else 'pnl-neg'}">₹{metrics.net_pnl:,.2f}</div>
        </div>
        <div class="metric-card">
            <div class="metric-title">Win Rate</div>
            <div class="metric-val">{metrics.win_rate:.1f}%</div>
        </div>
        <div class="metric-card">
            <div class="metric-title">Profit Factor</div>
            <div class="metric-val">{metrics.profit_factor:.2f}</div>
        </div>
        <div class="metric-card">
            <div class="metric-title">Total Trades</div>
            <div class="metric-val">{metrics.total_trades}</div>
        </div>
        <div class="metric-card">
            <div class="metric-title">Avg R-Multiple</div>
            <div class="metric-val">{metrics.average_r:.2f}R</div>
        </div>
        <div class="metric-card">
            <div class="metric-title">Max Drawdown</div>
            <div class="metric-val pnl-neg">{metrics.max_drawdown_pct:.1f}%</div>
        </div>
    </div>

    <div class="card">
        <h2>Summary Performance Statistics</h2>
        {summary_table_html}
    </div>

    <div class="card">
        <h2>Executed Trades (Sample)</h2>
        {trades_table_html}
    </div>
</body>
</html>
"""
        with open(html_file, "w", encoding="utf-8") as f:
            f.write(html_content)

    def _persist_run(
        self,
        from_date: date,
        to_date: date,
        universe: str,
        metrics: BacktestSummaryMetrics,
    ) -> None:
        """Stores backtest summary record into SQLite backtest_runs table."""
        metrics_dict = {
            "trading_days": metrics.trading_days,
            "total_trades": metrics.total_trades,
            "win_rate": metrics.win_rate,
            "profit_factor": metrics.profit_factor,
            "net_pnl": metrics.net_pnl,
            "max_drawdown_pct": metrics.max_drawdown_pct,
            "average_r": metrics.average_r,
        }
        with db.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO backtest_runs (strategy_name, from_date, to_date, universe, total_trades, win_rate, net_pnl, metrics_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    self.strategy_config.name,
                    from_date.isoformat(),
                    to_date.isoformat(),
                    universe,
                    metrics.total_trades,
                    metrics.win_rate,
                    metrics.net_pnl,
                    json.dumps(metrics_dict),
                ),
            )

    def _print_console_summary(self, metrics: BacktestSummaryMetrics) -> None:
        """Displays tabulated backtest results on standard output."""
        table_data = [
            ["Trading Days", metrics.trading_days],
            ["Total Trades", metrics.total_trades],
            ["Long / Short Trades", f"{metrics.long_trades} / {metrics.short_trades}"],
            ["Win / Loss Trades", f"{metrics.winning_trades} / {metrics.losing_trades}"],
            ["Win Rate", f"{metrics.win_rate:.2f}%"],
            ["Gross Profit", f"₹{metrics.gross_profit:,.2f}"],
            ["Gross Loss", f"₹{metrics.gross_loss:,.2f}"],
            ["Regulatory Charges", f"₹{metrics.total_charges:,.2f}"],
            ["Net P&L", f"₹{metrics.net_pnl:,.2f}"],
            ["Profit Factor", f"{metrics.profit_factor:.2f}"],
            ["Expectancy", f"₹{metrics.expectancy:.2f} / trade"],
            ["Average R-Multiple", f"{metrics.average_r:.2f}R"],
            ["Max Drawdown", f"₹{metrics.max_drawdown_amount:,.2f} ({metrics.max_drawdown_pct:.2f}%)"],
            ["Consecutive Wins / Losses", f"{metrics.consecutive_wins} / {metrics.consecutive_losses}"],
        ]
        print("\n" + "=" * 50)
        print("         ORB BACKTEST PERFORMANCE SUMMARY         ")
        print("=" * 50)
        print(tabulate(table_data, headers=["Metric", "Value"], tablefmt="fancy_grid"))
        print("=" * 50 + "\n")
