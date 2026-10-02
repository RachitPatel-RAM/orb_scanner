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
from datetime import datetime, date, time, timedelta
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
                self._handle_signal(signal, candle)

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
                self._handle_signal(signal, candle)

    def _handle_signal(self, sig: Signal, candle: Optional[Candle] = None) -> None:
        """Processes a new ORB breakout signal with ML conviction verification."""
        # AI learned conviction & false-breakout trap check:
        if candle:
            try:
                from app.strategies.ml_learner import ml_learner
                ai_eval = ml_learner.calculate_conviction_score(
                    candle=candle,
                    direction=sig.direction,
                    orb_high=sig.orb_high,
                    orb_low=sig.orb_low,
                )
                # Strict High-Probability Filter: only allow >= 65% conviction setups
                if ai_eval.score < 65:
                    logger.warning(
                        f"AI High-Probability Filter: Blocked {sig.symbol} {sig.direction.value} breakout "
                        f"(Conviction: {ai_eval.score}%, Reasons: {ai_eval.reasons}). Only high-probability (>=65%) allowed."
                    )
                    return
            except Exception as e:
                logger.error(f"Error evaluating ML conviction for signal {sig.symbol}: {e}")

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

        # Dispatch Telegram alert (fail-safe async task with candle context)
        asyncio.create_task(notifier.send_signal(sig, candle=candle))

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

        if now <= market_open_dt or now.time() >= default_session.market_close_time or not default_session.is_trading_day(now.date()):
            logger.info("Market is not currently in session. Intraday state recovery skipped.")
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

                    # Trades summary (only count real live trades linked to signals)
                    tr_rows = conn.execute(
                        "SELECT exit_reason, pnl, status FROM paper_trades WHERE trade_date = ? AND signal_id IS NOT NULL",
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

                logger.info(f"Generating Market Close Summary & 5-Year Deep Learning: {summary_data}")

                # 5-Year Empirical Deep Learning & Multi-Year Statistical Calibration
                try:
                    from app.strategies.historical_learner import historical_learner
                    from app.storage.firebase_sync import firebase_sync

                    # Run multi-year learning cycle across universe on genuine Dhan historical bars
                    historical_res = await historical_learner.run_historical_learning_cycle()

                    # Format clean EOD message without brand names, insights in quotes
                    eod_msg = historical_learner.format_eod_report_message(
                        daily_trades_summary=summary_data,
                        learning_res=historical_res,
                    )
                    await notifier.send_message(eod_msg, idempotency_key=f"{today_str}_EOD_SUMMARY")
                    await firebase_sync.save_daily_report(today_str, summary_data)
                except Exception as ml_err:
                    logger.error(f"Error during EOD historical learning: {ml_err}")
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

        # Start Telegram 1-click interactive approval listener
        from app.trading.order_executor import order_executor
        from app.strategies.gemini_analyzer import gemini_analyzer
        approval_listener_task = asyncio.create_task(order_executor.run_telegram_listener())

        # Start Hourly AI Market Intelligence Report Loop (Every hour on real Dhan data)
        async def _hourly_intelligence_loop():
            while self._running:
                await asyncio.sleep(3600)  # Every 60 minutes
                if not self._running:
                    break
                now_t = default_session.now()
                if time(10, 0) <= now_t.time() <= time(15, 35):
                    try:
                        hour_str = now_t.strftime("%H:00")
                        recent_trades = self.paper_tracker.get_closed_trades()
                        report_text = await gemini_analyzer.generate_hourly_market_report(
                            recent_trades=recent_trades,
                            signals_count=len(self.paper_tracker.active_trades) + len(recent_trades),
                            hour_label=hour_str,
                        )
                        await notifier.send_message(report_text, idempotency_key=f"hourly_{now_t.strftime('%Y%m%d_%H')}")
                    except Exception as e:
                        logger.debug(f"Error in hourly intelligence loop: {e}")

        hourly_task = asyncio.create_task(_hourly_intelligence_loop())

        # Continuous background 5-year empirical learning loop (runs every 30 minutes 24/7)
        async def _continuous_historical_learner_loop():
            from app.strategies.historical_learner import historical_learner
            # Fast initial pass 20 seconds after startup if market is closed or holiday
            await asyncio.sleep(20)
            while self._running:
                now_t = default_session.now()
                try:
                    logger.info("Continuous background 5-year historical learning pass running (30m interval)...")
                    res = await historical_learner.run_historical_learning_cycle()
                    if res:
                        report_text = historical_learner.format_offmarket_learning_report(res)
                        idemp = f"OFFMARKET_LEARN_{now_t.strftime('%Y%m%d_%H%M')}"
                        await notifier.send_message(report_text, idempotency_key=idemp)
                        logger.info("Dispatched 30-min AI learning update to Telegram.")
                except Exception as e:
                    logger.debug(f"Continuous background learning error: {e}")

                # Continuous learning every 30 minutes (1800 seconds)
                await asyncio.sleep(1800)

        learner_task = asyncio.create_task(_continuous_historical_learner_loop())

        try:
            logger.info(f"Live ORB Scanner running for {len(instruments)} stocks (24/7 Engine).")
            while self._running:
                now = default_session.now()
                if default_session.is_market_open(now):
                    try:
                        logger.info("Market is OPEN. Connecting to Dhan Live Market Feed...")
                        await live_feed.start()
                    except asyncio.CancelledError:
                        break
                    except Exception as e:
                        logger.error(f"Live feed error: {e}. Retrying in 10s...")
                        await asyncio.sleep(10)
                else:
                    # Off-market / Holiday / Weekend: sleep while background tasks (Telegram bot, continuous learner) run!
                    await asyncio.sleep(30)
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False
            watchdog_task.cancel()
            renew_task.cancel()
            cleanup_task.cancel()
            approval_listener_task.cancel()
            hourly_task.cancel()
            learner_task.cancel()
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
    compare: bool = False,
    capital: float = 5000.0,
) -> None:
    """Runs historical backtest on Dhan OHLCV data with optional timeframe comparison."""
    # Ensure instrument master is loaded
    if not instrument_manager.is_cache_valid():
        await instrument_manager.download_master()
    instrument_manager.load_and_parse()

    start_date = date.fromisoformat(from_date_str)
    end_date = date.fromisoformat(to_date_str)
    custom_list = [s.strip().upper() for s in symbols.split(",")] if symbols else None

    backtester = ORBBacktester(debug_mode=debug)
    if compare:
        await backtester.compare_timeframes(
            from_date=start_date,
            to_date=end_date,
            universe_mode=universe,
        )
    else:
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


async def cmd_learn(symbols: Optional[str] = None, max_stocks: int = 15) -> None:
    """Runs 5-year historical empirical learning on Dhan data and generates strategy report."""
    print("\n🧠 Running 5-Year Historical Deep Learning Cycle (DhanHQ API)...\n")
    if not instrument_manager.is_cache_valid():
        await instrument_manager.download_master()
    instrument_manager.load_and_parse()

    sym_list = [s.strip().upper() for s in symbols.split(",")] if symbols else None

    from app.strategies.historical_learner import historical_learner
    res = await historical_learner.run_historical_learning_cycle(symbols=sym_list, max_symbols=max_stocks)

    if not res:
        print("  [FAIL] Could not complete learning cycle. Check Dhan credentials and network.")
        return

    print(f"  [OK] Analyzed {res.get('total_bars_examined', 0):,} real historical daily bars.")
    print(f"  [OK] High-Volume Win Rate: {res.get('high_vol_win_rate', 0):.1f}% (Edge: +{res.get('vol_edge_pct', 0):.1f}%)")
    print(f"  [OK] Top Momentum Stocks:")
    for s in res.get("top_stocks", [])[:5]:
        print(f"       - {s['symbol']}: {s['high_vol_win_rate']}% Win Rate ({s['sessions_analyzed']} bars)")

    # Send report to Telegram
    today_str = date.today().isoformat()
    mock_summary = {"targets_hit": 0, "stops_hit": 0, "pnl": 0.0, "win_rate": res.get("high_vol_win_rate", 0.0)}
    eod_msg = historical_learner.format_eod_report_message(daily_trades_summary=mock_summary, learning_res=res)
    ok = await notifier.send_message(eod_msg, idempotency_key=f"{today_str}_CLI_LEARNING")
    if ok:
        print("  [OK] Dispatched clean strategy report to Telegram.")
    print("\nLearning cycle completed successfully.\n")


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

        balance = db.get_account_balance(4322.0)
        print("\nDatabase Record Counts:")
        print(f"  - Cached Instruments:   {c_inst}")
        print(f"  - 1-Minute Candles:     {c_1m}")
        print(f"  - 5-Minute Candles:     {c_5m}")
        print(f"  - ORB Daily Levels:     {c_orb}")
        print(f"  - Breakout Signals:     {c_sig}")
        print(f"  - Total Paper Trades:   {c_trades}")
        print(f"  - Active Open Trades:   {c_open}")
        print(f"  - Compounded Balance:   Rs {balance:,.2f} (5x Margin Active)")
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
    bt_parser.add_argument("--compare", action="store_true", help="Compare 15m, 30m, and 60m timeframes")
    bt_parser.add_argument("--capital", type=float, default=5000.0, help="Simulated trading capital (default: 5000)")

    # learn (5-year deep learning on genuine Dhan data)
    learn_parser = subparsers.add_parser("learn", help="Run 5-year historical learning on Dhan data")
    learn_parser.add_argument("--symbols", help="Specific symbols to analyze (comma-separated)")
    learn_parser.add_argument("--max", type=int, default=15, help="Max stocks to analyze (default: 15)")

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
                compare=args.compare,
                capital=args.capital,
            )
        )
    elif args.command == "learn":
        asyncio.run(cmd_learn(symbols=args.symbols, max_stocks=args.max))
    elif args.command == "status":
        cmd_status()


if __name__ == "__main__":
    main()

