"""
ORB Scanner - Main Application Entrypoint and CLI.

Commands:
  python main.py validate     - Test Dhan auth, Telegram connectivity, Database
  python main.py instruments  - Download and parse Dhan scrip master
  python main.py live         - Start real-time live feed & ORB breakout scanner
  python main.py backtest     - Run historical backtest on Dhan OHLCV data
  python main.py status       - Inspect current system and database state
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, date, timedelta
import signal
import sys
from typing import List, Optional
import zoneinfo

# Ensure Windows terminal can print UTF-8 without charmap errors
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from app.config import logger, settings, DATA_DIR
from app.dhan.auth import auth
from app.dhan.historical import historical_manager
from app.dhan.instruments import instrument_manager
from app.dhan.live_feed import live_feed
from app.market.candle_builder import CandleBuilder, TickData
from app.market.session import default_session
from app.notifications.telegram import notifier
from app.storage.database import db
from app.storage.models import Candle, ExitReason, PaperTrade, Signal
from app.strategies.orb import ORBStrategy
from app.trading.paper_tracker import PaperTracker
from app.backtest.orb_backtester import ORBBacktester


class LiveEngine:
    """Coordinates the live real-time market data feed, candle builder, ORB scanner, and tracker."""

    def __init__(self, debug_mode: bool = False):
        self.debug_mode = debug_mode
        self.strategy = ORBStrategy(debug_mode=self.debug_mode)
        self.paper_tracker = PaperTracker(
            on_target_hit=self._on_target_hit,
            on_stop_hit=self._on_stop_hit,
            on_eod_squareoff=self._on_eod_squareoff,
        )
        self.candle_builder = CandleBuilder(
            on_1m_candle_closed=self._on_1m_candle_closed,
            on_5m_candle_closed=self._on_5m_candle_closed,
            on_15m_candle_closed=self._on_15m_candle_closed,
            persist_to_db=True,
        )
        self._running = False
        self._daily_summary_sent = False

    async def stop(self) -> None:
        """Gracefully halts live scanner and disconnects feed."""
        self._running = False
        await live_feed.stop()

    def _on_1m_candle_closed(self, candle: Candle) -> None:
        """Triggered whenever a 1-minute candle finalizes."""
        # Update existing open paper trades
        self.paper_tracker.update_with_candle(candle)

    def _on_5m_candle_closed(self, candle: Candle) -> None:
        """Triggered whenever a 5-minute candle finalizes."""
        c_time = default_session.localize(candle.timestamp)

        # 1. Register or update opening range if inside ORB window (09:15 - 09:30)
        if default_session.is_orb_period(c_time):
            self.strategy.register_orb_candle(candle)
            return

        # 2. Check for breakout signals if 5m signal timeframe is active
        if settings.strategy.signal_timeframe == 5:
            signal = self.strategy.on_candle_closed(candle)
            if signal:
                self._handle_signal(signal)

    def _on_15m_candle_closed(self, candle: Candle) -> None:
        """Triggered whenever a 15-minute candle finalizes (e.g. 09:30, 09:45, 10:00)."""
        c_time = default_session.localize(candle.timestamp)

        # 1. Register opening range if inside ORB window (09:30 - 10:00)
        if default_session.is_orb_period(c_time):
            self.strategy.register_orb_candle(candle)
            return

        # 2. Check for breakout signals if 15m signal timeframe is active
        if settings.strategy.signal_timeframe == 15:
            signal = self.strategy.on_candle_closed(candle)
            if signal:
                self._handle_signal(signal)

    def _handle_signal(self, sig: Signal) -> None:
        """Processes a new ORB breakout signal."""
        # Save signal in SQLite (idempotency key prevents duplicate insertions)
        sig_id = db.save_signal(
            trade_date=sig.trade_date.isoformat(),
            security_id=sig.security_id,
            symbol=sig.symbol,
            strategy=sig.strategy,
            direction=sig.direction.value,
            timestamp=sig.timestamp.isoformat(),
            entry_price=sig.entry_price,
            orb_high=sig.orb_high,
            orb_low=sig.orb_low,
            stop_loss=sig.stop_loss,
            target=sig.target,
            risk_reward=sig.risk_reward,
            idempotency_key=sig.idempotency_key,
        )

        if sig_id is None:
            logger.info(f"Signal for {sig.symbol} already recorded. Skipping duplicate alert.")
            return

        # Open virtual position in paper tracker
        self.paper_tracker.open_trade_from_signal(sig)

        # Dispatch Telegram alert (fail-safe async task)
        asyncio.create_task(notifier.send_signal(sig))

    def _on_target_hit(self, trade: PaperTrade) -> None:
        """Callback when virtual position reaches its target."""
        asyncio.create_task(notifier.send_target_hit(trade))

    def _on_stop_hit(self, trade: PaperTrade) -> None:
        """Callback when virtual position hits stop loss."""
        asyncio.create_task(notifier.send_stop_hit(trade))

    def _on_eod_squareoff(self, trade: PaperTrade) -> None:
        """Callback on EOD square-off."""
        logger.info(f"Paper trade for {trade.symbol} squared off at EOD.")

    def on_tick_received(self, tick: TickData) -> None:
        """Feeds raw tick to candle builder and checks tick-level exits."""
        self.candle_builder.process_tick(tick)
        self.paper_tracker.update_with_tick(tick.security_id, tick.ltp, tick.timestamp)

    async def recover_intraday_state(self, instruments) -> None:
        """
        Crash / Late-Start Recovery:
        If started after 09:15, recover missing 1m candles for today and reconstruct ORB levels.
        """
        now = default_session.now()
        market_open_dt, orb_end_dt, _, _ = default_session.get_session_datetimes(now.date())

        if now <= market_open_dt:
            logger.info("Market not yet open today. Clean state initialized.")
            return

        logger.info(f"Late start / restart detected at {now.strftime('%H:%M:%S')}. Recovering session state...")

        for inst in instruments:
            try:
                candles = await historical_manager.recover_today_intraday(inst.security_id, inst.symbol)
                for c in candles:
                    c_time = default_session.localize(c.timestamp)
                    if default_session.is_orb_period(c_time):
                        self.strategy.register_orb_candle(c)

                # Finalize ORB if past 09:30
                if now >= orb_end_dt:
                    self.strategy.finalize_orb_levels(now.date(), inst.security_id, inst.symbol)

            except Exception as e:
                logger.error(f"Error recovering intraday candles for {inst.symbol}: {e}")

        logger.info("Intraday state recovery completed.")

    async def _market_close_watchdog(self, instruments_count: int) -> None:
        """Monitors for market close (15:30) to generate and dispatch the daily summary."""
        while self._running:
            await asyncio.sleep(15)
            now = default_session.now()

            # Flush any unclosed candles
            self.candle_builder.flush_stale_candles(now)

            # Check if market has closed and summary not yet sent
            if now.time() >= default_session.market_close_time and not self._daily_summary_sent:
                today_str = now.date().isoformat()
                with db.get_connection() as conn:
                    # Signals summary
                    sig_rows = conn.execute(
                        "SELECT direction, COUNT(*) as cnt FROM signals WHERE trade_date = ? GROUP BY direction",
                        (today_str,),
                    ).fetchall()
                    long_sig = sum(r["cnt"] for r in sig_rows if r["direction"] == "LONG")
                    short_sig = sum(r["cnt"] for r in sig_rows if r["direction"] == "SHORT")
                    total_sig = long_sig + short_sig

                    # Trades summary
                    tr_rows = conn.execute(
                        "SELECT exit_reason, pnl, status FROM paper_trades WHERE trade_date = ?",
                        (today_str,),
                    ).fetchall()
                    targets = sum(1 for r in tr_rows if r["exit_reason"] == "TARGET")
                    stops = sum(1 for r in tr_rows if r["exit_reason"] == "STOP_LOSS")
                    eod_exits = sum(1 for r in tr_rows if r["exit_reason"] == "EOD")
                    total_pnl = sum((r["pnl"] or 0.0) for r in tr_rows)
                    closed_count = targets + stops
                    win_rate = (targets / closed_count * 100.0) if closed_count > 0 else 0.0

                summary_data = {
                    "date": today_str,
                    "monitored_stocks": instruments_count,
                    "total_signals": total_sig,
                    "long_signals": long_sig,
                    "short_signals": short_sig,
                    "targets_hit": targets,
                    "stops_hit": stops,
                    "eod_exits": eod_exits,
                    "pnl": total_pnl,
                    "win_rate": win_rate,
                }

                logger.info(f"Generating Daily Market Summary: {summary_data}")
                await notifier.send_daily_summary(summary_data)
                self._daily_summary_sent = True

    async def run(
        self,
        symbols: Optional[List[str]] = None,
        universe_mode: Optional[str] = None,
    ) -> None:
        """Starts the live market feed and scanner loop."""
        self._running = True

        # 1. Ensure instrument master is loaded
        if not instrument_manager.is_cache_valid():
            logger.info("Instrument master cache expired or missing. Downloading...")
            await instrument_manager.download_master()
        instrument_manager.load_and_parse()

        # 2. Resolve universe or specific symbols
        if symbols:
            instruments = []
            for s in symbols:
                sec_id = instrument_manager.get_security_id(s)
                if sec_id:
                    info = instrument_manager.get_instrument_info(sec_id)
                    if info:
                        instruments.append(info)
                else:
                    logger.warning(f"Could not resolve symbol '{s}' in instrument master.")
            logger.info(f"Targeting specific symbols ({len(instruments)}): {symbols}")
        else:
            instruments = instrument_manager.resolve_universe(universe_mode)

        if not instruments:
            logger.error("No instruments found for configured universe. Aborting live scanner.")
            return

        # 3. Load open paper trades across restarts
        today = default_session.now().date()
        self.paper_tracker.load_open_trades_from_db(today)

        # 4. Crash / late start recovery
        await self.recover_intraday_state(instruments)

        # 5. Connect live market feed
        live_feed.on_tick = self.on_tick_received
        live_feed.subscribe_instruments(instruments)

        # Send Telegram startup notification
        await notifier.send_startup_message(
            stocks_count=len(instruments),
            strategy_name=settings.strategy.name,
            dhan_connected=auth.has_credentials,
            feed_connected=True,
        )

        # Start close watchdog
        watchdog_task = asyncio.create_task(self._market_close_watchdog(len(instruments)))

        # Start periodic token renewal watchdog (renews every 16 hours to keep token permanently active)
        async def _auto_renew_loop():
            while self._running:
                await asyncio.sleep(16 * 3600)  # every 16 hours
                if not self._running:
                    break
                logger.info("Triggering scheduled background Dhan token renewal...")
                ok, msg = await auth.renew_token()
                if ok:
                    logger.info(f"Background token auto-renewal succeeded: {msg}")
                else:
                    logger.warning(f"Background token auto-renewal failed: {msg}")

        renew_task = asyncio.create_task(_auto_renew_loop())

        # Start hourly Telegram 24h auto-delete cleanup loop
        async def _telegram_cleanup_loop():
            while self._running:
                await asyncio.sleep(3600)  # every hour
                if not self._running:
                    break
                try:
                    await notifier.cleanup_old_messages(older_than_hours=24)
                except Exception as e:
                    logger.debug(f"Error during Telegram cleanup: {e}")

        cleanup_task = asyncio.create_task(_telegram_cleanup_loop())

        try:
            logger.info(f"Live ORB Scanner running for {len(instruments)} stocks. Press Ctrl+C to terminate.")
            await live_feed.start()
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False
            watchdog_task.cancel()
            renew_task.cancel()
            cleanup_task.cancel()
            await live_feed.stop()
            logger.info("Live ORB scanner shut down cleanly.")


# =====================================================================
# CLI COMMAND IMPLEMENTATIONS
# =====================================================================


async def cmd_validate() -> None:
    """Validates Dhan credentials, Telegram bot, and Database state."""
    print("\n🔍 Running System & Credential Validation...\n")

    # 1. Database
    try:
        db.init_db()
        print("  [OK] SQLite Database initialized at:", settings.database_path)
    except Exception as e:
        print(f"  [FAIL] Database initialization error: {e}")

    # 2. Telegram
    if notifier.bot_token:
        print("  [INFO] Checking Telegram bot configuration...")
        if not notifier.chat_id:
            print("  [INFO] Searching for your chat_id via getUpdates...")
            discovered = await notifier.discover_chat_id()
            if discovered:
                print(f"  [OK] Discovered Telegram Chat ID: {discovered}")
                # Save to .env
                env_path = DATA_DIR.parent / ".env"
                if env_path.exists():
                    txt = env_path.read_text(encoding="utf-8")
                    if "TELEGRAM_CHAT_ID=" in txt:
                        lines = [f"TELEGRAM_CHAT_ID={discovered}" if l.startswith("TELEGRAM_CHAT_ID=") else l for l in txt.splitlines()]
                        env_path.write_text("\n".join(lines), encoding="utf-8")
                    else:
                        env_path.write_text(txt + f"\nTELEGRAM_CHAT_ID={discovered}\n", encoding="utf-8")
                    print("  [OK] Saved TELEGRAM_CHAT_ID to .env")
            else:
                print("  [PENDING] Bot token is valid, but no incoming messages were found.")
                print("  --> Please open your Telegram bot, press 'Start' or send /start, then re-run validate.")

        if notifier.is_configured:
            test_msg = (
                "<b>✅ ORB Scanner Test</b>\n\n"
                "<b>Telegram:</b> Connected\n"
                f"<b>Dhan:</b> {'Connected' if auth.has_credentials else 'Pending'}\n"
                "<b>Data API:</b> Active\n"
                "<b>Order Execution:</b> DISABLED\n"
                f"<b>Timestamp:</b> {default_session.now().strftime('%Y-%m-%d %H:%M:%S')} IST"
            )
            ok = await notifier.send_message(test_msg)
            if ok:
                print("  [OK] Telegram test message successfully delivered!")
            else:
                print("  [WARN] Telegram credentials provided but message dispatch failed.")
    else:
        print("  [PENDING] TELEGRAM_BOT_TOKEN not configured in .env.")

    # 3. Dhan Credentials
    if auth.has_credentials:
        print("  [INFO] Validating DhanHQ v2 credentials with API (GET /v2/profile)...")
        try:
            profile = await auth.get_profile()
            print("  [OK] DhanHQ authenticated successfully!")
            token_valid = profile.get("tokenValidity", "Valid")
            data_plan = profile.get("dataPlan", "Active")
            data_valid = profile.get("dataValidity", "Valid")
            print(f"  [OK] Token: {token_valid} | Data Plan: {data_plan} | Data Validity: {data_valid}")
        except PermissionError as pe:
            print(f"  [EXPIRED] Dhan access token is invalid or expired: {pe}")
            print("  --> Manually generate a fresh 24h token from Dhan Web/App and paste into .env")
        except Exception as e:
            print(f"  [FAIL] Dhan profile validation failed: {e}")
    else:
        print("  [PENDING] Dhan credentials not configured in .env (DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN).")

    print("\nValidation completed.\n")


async def cmd_instruments() -> None:
    """Downloads and caches Dhan scrip master, then outputs counts."""
    print("\n📥 Fetching DhanHQ Scrip Master...\n")
    downloaded = await instrument_manager.download_master(force=True)
    if not downloaded:
        print("  [FAIL] Could not download scrip master from Dhan.")
        return

    instrument_manager.load_and_parse()
    universe = instrument_manager.resolve_universe()
    print(f"  [OK] Indexed {len(instrument_manager.instruments_by_id)} NSE Equity scrips.")
    print(f"  [OK] Resolved universe mode '{settings.universe.mode}': {len(universe)} stocks.")
    print("Scrip master update completed.\n")


async def cmd_live(
    debug: bool = False,
    symbols: Optional[str] = None,
    universe: Optional[str] = None,
) -> None:
    """Starts the real-time ORB scanner."""
    sym_list = [s.strip().upper() for s in symbols.split(",")] if symbols else None
    engine = LiveEngine(debug_mode=debug)
    await engine.run(symbols=sym_list, universe_mode=universe)


async def cmd_backtest(
    from_date_str: str,
    to_date_str: str,
    universe: str = "nifty50",
    symbols: Optional[str] = None,
    debug: bool = False,
    force_download: bool = False,
) -> None:
    """Runs historical backtest on Dhan OHLCV data."""
    # Ensure instrument master is loaded
    if not instrument_manager.is_cache_valid():
        await instrument_manager.download_master()
    instrument_manager.load_and_parse()

    start_date = date.fromisoformat(from_date_str)
    end_date = date.fromisoformat(to_date_str)
    custom_list = [s.strip().upper() for s in symbols.split(",")] if symbols else None

    backtester = ORBBacktester(debug_mode=debug)
    await backtester.run(
        from_date=start_date,
        to_date=end_date,
        universe_mode=universe,
        custom_symbols=custom_list,
        force_download=force_download,
    )


async def cmd_renew() -> None:
    """Renews an active Dhan access token via GET /v2/RenewToken."""
    print("\n🔄 Attempting to renew Dhan Access Token...\n")
    ok, msg = await auth.renew_token()
    if ok:
        print(f"  [OK] {msg}")
    else:
        print(f"  [FAIL] {msg}")
        print("  --> Note: RenewToken works only while the current token is still active.")
        print("  --> If your token is already expired, generate a fresh token from Dhan Web/App and paste into .env")
    print("\n")


def cmd_status() -> None:
    """Displays current system status, database metrics, and open positions."""
    print("\n" + "=" * 55)
    print("              ORB SCANNER SYSTEM STATUS              ")
    print("=" * 55)
    print(f"Timezone:           {settings.timezone_name}")
    print(f"Local IST Time:     {default_session.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Market Session:     {'OPEN' if default_session.is_market_open() else 'CLOSED'}")
    print(f"Strategy:           {settings.strategy.name} (Timeframe: {settings.strategy.signal_timeframe}m)")
    print(f"Universe Mode:      {settings.universe.mode}")
    print(f"Dhan Credentials:   {'CONFIGURED' if auth.has_credentials else 'NOT SET'}")
    print(f"Telegram Bot:       {'CONFIGURED' if notifier.is_configured else 'NOT SET'}")
    print(f"Database File:      {settings.database_path}")

    # Database query counts
    try:
        with db.get_connection() as conn:
            c_inst = conn.execute("SELECT COUNT(*) as c FROM instruments").fetchone()["c"]
            c_1m = conn.execute("SELECT COUNT(*) as c FROM candles_1m").fetchone()["c"]
            c_5m = conn.execute("SELECT COUNT(*) as c FROM candles_5m").fetchone()["c"]
            c_orb = conn.execute("SELECT COUNT(*) as c FROM orb_daily_levels").fetchone()["c"]
            c_sig = conn.execute("SELECT COUNT(*) as c FROM signals").fetchone()["c"]
            c_trades = conn.execute("SELECT COUNT(*) as c FROM paper_trades").fetchone()["c"]
            c_open = conn.execute("SELECT COUNT(*) as c FROM paper_trades WHERE status='OPEN'").fetchone()["c"]

        print("\nDatabase Record Counts:")
        print(f"  - Cached Instruments:   {c_inst}")
        print(f"  - 1-Minute Candles:     {c_1m}")
        print(f"  - 5-Minute Candles:     {c_5m}")
        print(f"  - ORB Daily Levels:     {c_orb}")
        print(f"  - Breakout Signals:     {c_sig}")
        print(f"  - Total Paper Trades:   {c_trades}")
        print(f"  - Active Open Trades:   {c_open}")
    except Exception as e:
        print(f"  [ERROR querying database: {e}]")

    print("=" * 55 + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="ORB Stock Scanner & Backtester (DhanHQ API v2)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # validate
    subparsers.add_parser("validate", help="Validate Dhan credentials, Telegram, and DB")

    # renew
    subparsers.add_parser("renew", help="Renew an active Dhan access token (adds another 24 hours)")

    # instruments
    subparsers.add_parser("instruments", help="Download and index Dhan instrument master")

    # live
    live_parser = subparsers.add_parser("live", help="Start live feed ORB scanner")
    live_parser.add_argument("--symbols", help="Specific symbols to scan (e.g. RELIANCE or RELIANCE,TCS)")
    live_parser.add_argument("--universe", help="Universe mode: custom, nifty50, nifty100, all_nse_equity")
    live_parser.add_argument("--debug", action="store_true", help="Enable TradingView comparison debug prints")

    # backtest
    bt_parser = subparsers.add_parser("backtest", help="Run historical backtest")
    bt_parser.add_argument("--from", "--from-date", dest="from_date", required=True, help="Start date (YYYY-MM-DD)")
    bt_parser.add_argument("--to", "--to-date", dest="to_date", required=True, help="End date (YYYY-MM-DD)")
    bt_parser.add_argument("--universe", default="nifty50", help="Universe mode: custom, nifty50, all_nse_equity")
    bt_parser.add_argument("--symbols", help="Comma-separated symbols (e.g. RELIANCE,TCS)")
    bt_parser.add_argument("--debug", action="store_true", help="Print TradingView comparison debugging lines")
    bt_parser.add_argument("--force-download", action="store_true", help="Force re-download historical data")

    # status
    subparsers.add_parser("status", help="Show system status and database statistics")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "validate":
        asyncio.run(cmd_validate())
    elif args.command == "renew":
        asyncio.run(cmd_renew())
    elif args.command == "instruments":
        asyncio.run(cmd_instruments())
    elif args.command == "live":
        asyncio.run(cmd_live(debug=args.debug, symbols=args.symbols, universe=args.universe))
    elif args.command == "backtest":
        asyncio.run(
            cmd_backtest(
                from_date_str=args.from_date,
                to_date_str=args.to_date,
                universe=args.universe,
                symbols=args.symbols,
                debug=args.debug,
                force_download=args.force_download,
            )
        )
    elif args.command == "status":
        cmd_status()


if __name__ == "__main__":
    main()
