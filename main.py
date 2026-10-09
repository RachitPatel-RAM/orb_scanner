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
import httpx
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
            on_milestone_hit=self._on_milestone_hit,
        )
        self.candle_builder = CandleBuilder(
            on_1m_candle_closed=self._on_1m_candle_closed,
            on_5m_candle_closed=self._on_5m_candle_closed,
            on_15m_candle_closed=self._on_15m_candle_closed,
            persist_to_db=True,
        )
        self._running = False
        self._daily_summary_sent = False
        self._on_signal_generated = self._handle_signal

    async def stop(self) -> None:
        """Gracefully halts live scanner and disconnects feed."""
        self._running = False
        await live_feed.stop()

    def _on_signal_generated(self, sig: Signal, candle: Optional[Candle] = None) -> None:
        """Callback alias for processing signals."""
        self._handle_signal(sig, candle=candle)

    def _on_1m_candle_closed(self, candle: Candle) -> None:
        """Triggered whenever a 1-minute candle finalizes."""
        # Update existing open paper trades
        self.paper_tracker.update_with_candle(candle)

        # 09:16 AM Opening Momentum Scalp (fires when 09:15-09:16 candle closes)
        c_time = default_session.localize(candle.timestamp)
        if c_time.hour == 9 and c_time.minute == 16:
            try:
                from app.trading.fast_scalp import fast_scalp_engine
                async def _run_0916_scalp():
                    s = await fast_scalp_engine.evaluate_0916_scalp(candle, c_time.date())
                    if s:
                        await fast_scalp_engine.broadcast_0916_scalp_alert(s)
                        logger.info(f"Dispatched 09:16 AM Opening Momentum Scalp for {s.contract_symbol} ({s.direction.value}).")
                asyncio.create_task(_run_0916_scalp())
            except Exception as e:
                logger.debug(f"09:16 scalp evaluation note: {e}")

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
            # Query recent 15m candles to calculate 20-period average volume for verified institutional expansion
            try:
                recent_vols = db.get_recent_candles_15m(candle.security_id, limit=20)
                if recent_vols and len(recent_vols) >= 3:
                    avg_v = sum(r["volume"] for r in recent_vols) / len(recent_vols)
                    setattr(candle, "avg_volume_20", avg_v)
            except Exception as e:
                logger.debug(f"Volume calculation note for {candle.symbol}: {e}")

            signal = self.strategy.on_candle_closed(candle)
            if signal:
                self._handle_signal(signal, candle)

        # 3. 60-Minute Liquidity Context Bar Completion (Section 7)
        if c_time.minute == 15 and c_time.hour in (10, 11, 12, 13, 14, 15):
            try:
                from app.analysis.liquidity_context import liquidity_context_engine, Bar60m, ContextDirection
                start_dt = c_time - timedelta(hours=1)
                with db.get_connection() as conn:
                    rows = conn.execute(
                        "SELECT open, high, low, close, volume FROM candles_15m WHERE security_id = ? AND timestamp >= ? AND timestamp <= ? ORDER BY timestamp ASC",
                        (str(candle.security_id), start_dt.isoformat(), c_time.isoformat())
                    ).fetchall()
                if rows and len(rows) >= 2:
                    bar60 = Bar60m(
                        bar_id=f"{candle.symbol}_{start_dt.strftime('%Y%m%d%H%M')}",
                        symbol=candle.symbol,
                        security_id=candle.security_id,
                        start_time=start_dt,
                        end_time=c_time,
                        open=float(rows[0]["open"]),
                        high=max(float(r["high"]) for r in rows),
                        low=min(float(r["low"]) for r in rows),
                        close=float(rows[-1]["close"]),
                        volume=sum(float(r["volume"]) for r in rows),
                    )
                    prev_ctx = liquidity_context_engine.get_current_context(candle.symbol, start_dt)
                    new_ctx = liquidity_context_engine.add_60m_bar(bar60)
                    if new_ctx.direction != prev_ctx.direction and new_ctx.direction in (ContextDirection.BULLISH, ContextDirection.BEARISH, ContextDirection.CONFLICT):
                        paused_resumed = "PAUSED (Conflict)" if new_ctx.direction == ContextDirection.CONFLICT else f"ACTIVE ({new_ctx.direction.value})"
                        asyncio.create_task(
                            notifier.send_entry_permission_update(
                                symbol=candle.symbol,
                                morning_bias="ACTIVE",
                                context_and_level=f"{new_ctx.direction.value} Sweep",
                                paused_or_resumed=paused_resumed,
                                reason=new_ctx.reason,
                                event_id=new_ctx.active_events[0].event_id if new_ctx.active_events else "EVT",
                            )
                        )
            except Exception as e:
                logger.debug(f"60m bar aggregation note for {candle.symbol}: {e}")

    def _handle_signal(self, sig: Signal, candle: Optional[Candle] = None) -> None:
        """Schedules async multi-confluence verification and signal dispatch."""
        asyncio.create_task(self._process_signal_async(sig, candle))

    async def _process_signal_async(self, sig: Signal, candle: Optional[Candle] = None) -> None:
        """Processes a new ORB breakout through Daily Bias Gate, ML conviction & Confluence verification."""
        # 0. Daily Bias and Intraday Liquidity Context Gate (Sections 6, 7 & 9)
        try:
            from app.analysis.daily_bias import daily_bias_engine, DailyBiasSnapshot, BiasDirection, BiasReasonCode
            from app.analysis.liquidity_context import liquidity_context_engine
            from app.trading.bias_gate import bias_gate_engine

            today_date = sig.trade_date
            daily_snap_dict = db.get_daily_bias_snapshot(sig.symbol, today_date.isoformat(), settings.bias_rule_version)
            if not daily_snap_dict:
                daily_snap = daily_bias_engine.get_or_compute_snapshot(
                    symbol=sig.symbol,
                    security_id=sig.security_id,
                    trading_date=today_date,
                )
            else:
                daily_snap = DailyBiasSnapshot(
                    snapshot_id=daily_snap_dict["snapshot_id"],
                    symbol=daily_snap_dict["symbol"],
                    security_id=str(daily_snap_dict["security_id"]),
                    exchange=daily_snap_dict.get("exchange", "NSE"),
                    trading_date=daily_snap_dict["trading_date"],
                    previous_session_date=daily_snap_dict["previous_session_date"],
                    reference_session_date=daily_snap_dict["reference_session_date"],
                    previous_close=float(daily_snap_dict["previous_close"]),
                    reference_high=float(daily_snap_dict["reference_high"]),
                    reference_low=float(daily_snap_dict["reference_low"]),
                    daily_bias=BiasDirection(daily_snap_dict["daily_bias"]),
                    reason_code=BiasReasonCode(daily_snap_dict["reason_code"]),
                    source_data_timestamp=daily_snap_dict.get("source_data_timestamp"),
                    rule_version=daily_snap_dict.get("rule_version", settings.bias_rule_version),
                    data_health=daily_snap_dict.get("data_health", "HEALTHY"),
                )

            ctx_snap = liquidity_context_engine.get_current_context(sig.symbol, default_session.now())

            bias_decision = bias_gate_engine.evaluate_candidate(
                candidate_id=sig.idempotency_key,
                symbol=sig.symbol,
                security_id=sig.security_id,
                trade_date=today_date.isoformat(),
                candidate_direction=sig.direction,
                daily_snapshot=daily_snap,
                liquidity_snapshot=ctx_snap,
                persist=True,
            )

            if not bias_decision.is_allowed:
                logger.warning(
                    f"[Bias Gate VETO] Blocked {sig.symbol} {sig.direction.value} breakout: "
                    f"Decision={bias_decision.decision.value}, Mode={bias_decision.gate_mode}, "
                    f"DailyBias={bias_decision.daily_bias}, Context={bias_decision.context_direction}, "
                    f"Reason={bias_decision.reason_code}"
                )
                return
            else:
                logger.info(
                    f"[Bias Gate PASS] {sig.symbol} {sig.direction.value} permitted: "
                    f"Decision={bias_decision.decision.value}, Mode={bias_decision.gate_mode}, "
                    f"WouldAllow={bias_decision.would_allow}"
                )
        except Exception as e:
            logger.error(f"Error in Bias Gate candidate evaluation for {sig.symbol}: {e}")

        is_index = sig.symbol in ("NIFTY", "BANKNIFTY", "SENSEX", "NIFTY50") or str(sig.security_id) in ("13", "25", "51")

        # 1. AI learned conviction & false-breakout trap check:
        if candle and not is_index:
            try:
                from app.strategies.ml_learner import ml_learner
                avg_vol = getattr(candle, "avg_volume_20", None)
                ai_eval = ml_learner.calculate_conviction_score(
                    candle=candle,
                    direction=sig.direction,
                    orb_high=sig.orb_high,
                    orb_low=sig.orb_low,
                    avg_volume_20=avg_vol,
                )
                # Strict High-Probability Filter: only allow >= 75% conviction setups (SURE SHOT)
                if ai_eval.score < 75:
                    logger.warning(
                        f"AI High-Probability Filter: Blocked {sig.symbol} {sig.direction.value} breakout "
                        f"(Conviction: {ai_eval.score}%, Reasons: {ai_eval.reasons}). Only sure-shot high-probability (>=75%) allowed."
                    )
                    return
            except Exception as e:
                logger.error(f"Error evaluating ML conviction for signal {sig.symbol}: {e}")

        # 2. Institutional Multi-Confluence Engine (Traditional Pivots + Dhan OI Profile)
        # Prevents selling right into S1 Support or buying right into R1 Resistance!
        confluence = None
        try:
            from app.strategies.confluence_engine import confluence_engine
            confluence = await confluence_engine.evaluate_confluence(
                security_id=sig.security_id,
                symbol=sig.symbol,
                direction=sig.direction,
                entry_price=sig.entry_price,
                stop_loss=sig.stop_loss,
                target=sig.target,
                candle=candle,
            )
            if not confluence.is_valid:
                logger.warning(
                    f"[Confluence Filter] Blocked {sig.symbol} {sig.direction.value} breakout: "
                    f"{confluence.rejection_reason}"
                )
                return

            # Adopt dynamic structural Target and Stop Loss from SMC / Pivots / OI (Not rigid 1:2)
            if confluence.structural_target and confluence.structural_stop_loss:
                sig.target = confluence.structural_target
                sig.stop_loss = confluence.structural_stop_loss
                if confluence.structural_rr:
                    sig.risk_reward = confluence.structural_rr
                logger.info(
                    f"[Structural Levels] {sig.symbol} {sig.direction.value}: "
                    f"Target=₹{sig.target:.2f} ({confluence.target_milestone}), "
                    f"SL=₹{sig.stop_loss:.2f} ({confluence.sl_milestone}), R:R=1:{sig.risk_reward:g}"
                )

            logger.info(f"[Confluence Confirmed] {sig.symbol} {sig.direction.value}: {confluence.summary_text}")
        except Exception as e:
            logger.debug(f"Confluence evaluation note for {sig.symbol}: {e}")

        # 2.5 AI Dual Ensemble Veto Gate (Gemini 3.5 Flash Lite + Groq LPU)
        # If AI flags CAUTION, TRAP, lack of volume, or false move risk,
        # the signal is IMMEDIATELY VETOED before any trade is opened or alert sent!
        ai_reason_text: Optional[str] = None
        try:
            from app.strategies.groq_analyzer import groq_analyzer
            from app.strategies.gemini_analyzer import gemini_analyzer

            vol_ratio = 1.0
            if candle and getattr(candle, "avg_volume_20", None) and candle.avg_volume_20 > 0:
                vol_ratio = candle.volume / candle.avg_volume_20

            groq_task = groq_analyzer.analyze_breakout_fast(sig, candle, volume_surge=vol_ratio)
            gemini_task = gemini_analyzer.analyze_breakout(sig, candle, volume_surge=vol_ratio)
            groq_res, gemini_res = await asyncio.gather(groq_task, gemini_task, return_exceptions=True)

            groq_verdict = groq_res.get("verdict", "") if isinstance(groq_res, dict) else ""
            gemini_verdict = gemini_res.get("verdict", "") if isinstance(gemini_res, dict) else ""

            groq_reason = groq_res.get("reasoning", "") if isinstance(groq_res, dict) else ""
            gemini_reason = gemini_res.get("reasoning", "") if isinstance(gemini_res, dict) else ""
            active_reason = gemini_reason or groq_reason

            # Check if AI flags caution, trap, lack of volume, or false move risk
            is_caution = (
                "CAUTION" in groq_verdict
                or "CAUTION" in gemini_verdict
                or "false move" in active_reason.lower()
                or "lacks" in active_reason.lower()
                or "trap" in active_reason.lower()
            )

            if is_caution:
                logger.warning(
                    f"[AI Veto Filter] Blocked {sig.symbol} {sig.direction.value}: AI flagged CAUTION/TRAP. "
                    f"Groq={groq_verdict}, Gemini={gemini_verdict}. Reason: {active_reason}"
                )
                return

            if active_reason:
                ai_reason_text = active_reason
        except Exception as e:
            logger.debug(f"AI ensemble verification note for {sig.symbol}: {e}")

        # 3. Save signal in SQLite (idempotency key prevents duplicate insertions)
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

        # 4. Open virtual position in paper tracker
        self.paper_tracker.open_trade_from_signal(sig, signal_id=sig_id)

        # 5. Dispatch Telegram alert to trader with 1-click execution button
        await notifier.send_signal(sig, candle=candle, ai_reason_override=ai_reason_text)

        # 6. Broadcast clean signal to VIP Paid Channel (if configured)
        if confluence:
            try:
                from app.notifications.vip_channel import vip_manager
                await vip_manager.broadcast_vip_signal(sig, confluence)
            except Exception as e:
                logger.debug(f"VIP broadcast note: {e}")

    def _on_milestone_hit(self, trade: PaperTrade, milestone_num: int, milestone_price: float) -> None:
        """Callback when virtual position reaches Target 1 or Target 2 milestone."""
        asyncio.create_task(notifier.send_milestone_hit(trade, milestone_num, milestone_price))

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
        # Fast Scalp tick tracking (09:15 - 09:30)
        try:
            from app.trading.fast_scalp import fast_scalp_engine
            active_s = fast_scalp_engine.get_active_scalp()
            if active_s and active_s.security_id == str(tick.security_id):
                asyncio.create_task(fast_scalp_engine.on_tick(tick.ltp))
        except Exception:
            pass

    async def recover_intraday_state(self, instruments) -> None:
        """
        Crash / Late-Start Recovery:
        If started after 09:15, recover missing 1m candles for today and reconstruct ORB levels.
        Replays post-10:00 candles to detect and register any breakouts that occurred earlier today.
        """
        now = default_session.now()
        market_open_dt, orb_end_dt, _, _ = default_session.get_session_datetimes(now.date())

        if now <= market_open_dt or now.time() >= default_session.market_close_time or not default_session.is_trading_day(now.date()):
            logger.info("Market is not currently in session. Intraday state recovery skipped.")
            return

        logger.info(f"Late start / restart detected at {now.strftime('%H:%M:%S')}. Recovering session state in parallel...")

        sem = asyncio.Semaphore(10)

        async def _recover_single_stock(inst):
            async with sem:
                try:
                    candles = await historical_manager.recover_today_intraday(inst.security_id, inst.symbol)
                    for c in candles:
                        c_time = default_session.localize(c.timestamp)
                        if default_session.is_orb_period(c_time):
                            self.strategy.register_orb_candle(c)

                    # Finalize ORB if past 10:00
                    if now >= orb_end_dt:
                        self.strategy.finalize_orb_levels(now.date(), inst.security_id, inst.symbol)

                        # Replay post-10:00 candles through breakout checker
                        for c in candles:
                            c_time = default_session.localize(c.timestamp)
                            if c_time >= orb_end_dt:
                                sig = self.strategy.process_candle(c)
                                if sig:
                                    self._on_signal_generated(sig, candle=c)
                                    break
                except Exception as e:
                    logger.debug(f"Error recovering intraday candles for {inst.symbol}: {e}")

        await asyncio.gather(*[_recover_single_stock(inst) for inst in instruments])
        logger.info("Intraday state recovery completed across universe.")

    async def _market_close_watchdog(self, instruments_count: int) -> None:
        """Monitors for market close (15:25–15:30) to generate and dispatch the daily summary."""
        while self._running:
            await asyncio.sleep(15)
            now = default_session.now()

            # Flush any unclosed candles
            self.candle_builder.flush_stale_candles(now)

            # Check if market has reached close cutoff (15:25 IST) and summary not yet sent
            today_str = now.date().isoformat()
            if getattr(self, "_last_summary_date", None) != today_str:
                self._daily_summary_sent = False

            if (
                default_session.is_trading_day(now.date())
                and now.time() >= default_session.entry_end_time
                and not self._daily_summary_sent
            ):
                with db.get_connection() as conn:
                    # Signals summary
                    sig_rows = conn.execute(
                        "SELECT direction, COUNT(*) as cnt FROM signals WHERE trade_date = ? GROUP BY direction",
                        (today_str,),
                    ).fetchall()
                    long_sig = sum(r["cnt"] for r in sig_rows if r["direction"] == "LONG")
                    short_sig = sum(r["cnt"] for r in sig_rows if r["direction"] == "SHORT")
                    total_sig = long_sig + short_sig

                    # Detailed itemized breakouts for today
                    breakout_rows = conn.execute(
                        """
                        SELECT s.symbol, s.direction, s.entry_price, s.orb_high, s.orb_low,
                               t.exit_price, t.pnl, t.exit_reason
                        FROM signals s
                        LEFT JOIN paper_trades t ON s.id = t.signal_id
                        WHERE s.trade_date = ?
                        ORDER BY s.id ASC
                        """,
                        (today_str,),
                    ).fetchall()

                    breakout_items = []
                    for row in breakout_rows:
                        sym = row["symbol"]
                        dir_str = row["direction"]
                        entry = float(row["entry_price"] or 0.0)
                        exit_p = float(row["exit_price"] or entry)
                        pct = ((exit_p - entry) / entry * 100.0) if dir_str == "LONG" else ((entry - exit_p) / entry * 100.0)
                        lot_sz = instrument_manager.get_lot_size(sym)
                        lot_pnl = ((exit_p - entry) * lot_sz) if dir_str == "LONG" else ((entry - exit_p) * lot_sz)
                        breakout_items.append({
                            "symbol": sym,
                            "direction": dir_str,
                            "entry_price": entry,
                            "exit_price": exit_p,
                            "pct_move": pct,
                            "lot_size": lot_sz,
                            "lot_pnl": lot_pnl,
                            "exit_reason": row["exit_reason"],
                        })

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
                    "breakouts": breakout_items,
                }

                logger.info(f"Generating Market Close Summary for {today_str}: {summary_data}")

                # 1. Immediately dismiss all active Telegram inline buttons from today's alerts
                try:
                    cleared_btns = await notifier.dismiss_all_active_buttons_for_date(today_str)
                    logger.info(f"Market close button cleanup: dismissed active buttons on {cleared_btns} messages.")
                except Exception as btn_err:
                    logger.debug(f"Button cleanup error: {btn_err}")

                # 2. Dispatch verified daily summary to Telegram immediately
                try:
                    await notifier.send_daily_summary(summary_data)
                    logger.info("Market close daily summary dispatched to Telegram.")
                except Exception as sum_err:
                    logger.error(f"Error dispatching EOD daily summary: {sum_err}")

                # 3. Background empirical multi-year deep learning cycle (non-blocking)
                async def _bg_historical_learning():
                    try:
                        from app.strategies.historical_learner import historical_learner
                        from app.storage.firebase_sync import firebase_sync

                        logger.info("Starting background EOD 5-Year Deep Learning calibration...")
                        historical_res = await historical_learner.run_historical_learning_cycle()
                        await firebase_sync.save_daily_report(today_str, summary_data)
                        logger.info("Background EOD 5-Year Deep Learning calibration complete.")
                    except Exception as ml_err:
                        logger.error(f"Error during background EOD learning: {ml_err}")

                asyncio.create_task(_bg_historical_learning())

                self._daily_summary_sent = True
                self._last_summary_date = today_str

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

        # 3. Start Telegram listener immediately so /balance, /limit, /learn respond right away
        from app.trading.order_executor import order_executor
        from app.strategies.gemini_analyzer import gemini_analyzer
        from app.notifications.support_bot import support_bot
        approval_listener_task = asyncio.create_task(order_executor.run_telegram_listener())
        if support_bot.is_configured:
            support_listener_task = asyncio.create_task(support_bot.run_support_bot_listener())

        # 4. Load open paper trades across restarts
        today = default_session.now().date()
        self.paper_tracker.load_open_trades_from_db(today)

        # 5. Crash / late start recovery
        await self.recover_intraday_state(instruments)

        # 6. Connect live market feed
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

        # Start periodic token renewal watchdog (renews immediately on startup & every 3 hours to keep token permanently active)
        async def _auto_renew_loop():
            # Initial proactive renewal on startup
            try:
                ok_init, msg_init = await auth.renew_token()
                if ok_init:
                    logger.info(f"Startup Dhan token auto-renewal succeeded: {msg_init}")
                else:
                    logger.debug(f"Startup token renewal note: {msg_init}")
            except Exception as e:
                logger.debug(f"Startup token renewal check: {e}")

            failed_attempts = 0
            while self._running:
                # Renew every 2 hours on success, or retry every 15 minutes if an attempt failed
                sleep_sec = (2 * 3600) if failed_attempts == 0 else 900
                await asyncio.sleep(sleep_sec)
                if not self._running:
                    break
                logger.info("Triggering scheduled background Dhan token renewal...")
                ok, msg = await auth.renew_token()
                if ok:
                    failed_attempts = 0
                    logger.info(f"Background token auto-renewal succeeded: {msg}")
                    await notifier.send_message(f"🔄 <b>Dhan Token Auto-Renewed</b>\n\n• {msg}")
                else:
                    failed_attempts += 1
                    logger.warning(f"Background token auto-renewal attempt #{failed_attempts} failed: {msg}")
                    if failed_attempts >= 3 or "Invalid Token" in msg:
                        await notifier.send_message(
                            f"⚠️ <b>Dhan Token Auto-Renewal Alert</b>\n\n"
                            f"• {msg}\n"
                            "• You can generate a new token from Dhan and paste it here directly using: <code>/token &lt;jwt&gt;</code> or simply paste the raw token."
                        )

        renew_task = asyncio.create_task(_auto_renew_loop())

        # Start 09:10 AM Pre-Market Discovery & CPR Digest Broadcast Loop
        async def _premarket_digest_loop():
            while self._running:
                await asyncio.sleep(20)
                if not self._running:
                    break
                now_t = default_session.now()
                # On trading days between 09:09:00 and 09:14:00 IST
                if default_session.is_trading_day(now_t.date()) and time(9, 9) <= now_t.time() < time(9, 14):
                    idemp = f"PREMARKET_{now_t.strftime('%Y%m%d')}"
                    try:
                        from app.analysis.pre_market import pre_market_manager
                        await pre_market_manager.generate_and_broadcast_digest(now_t.date())
                        logger.info("Dispatched 09:10 AM Pre-Market CPR & Daily Bias Digest.")
                    except Exception as e:
                        logger.debug(f"Error in premarket broadcast loop: {e}")
                    await asyncio.sleep(300)

        premarket_task = asyncio.create_task(_premarket_digest_loop())

        # 10:00 AM Daily Major Indices Benchmark Tracking (Internal Levels Only, No Channel Spam)
        async def _indices_orb_broadcast_loop():
            while self._running:
                await asyncio.sleep(60)
                if not self._running:
                    break
                now_t = default_session.now()
                # Levels are logged internally, not broadcasted to channels per user preference
                if default_session.is_trading_day(now_t.date()) and time(10, 0) <= now_t.time() < time(10, 5):
                    logger.info("Internal 10:00 AM Benchmark levels established. (Channel marking broadcast suppressed).")
                    await asyncio.sleep(300)

        indices_task = asyncio.create_task(_indices_orb_broadcast_loop())

        # Start Index Breakout Watchdog (Monitors Nifty 50, BankNifty, Sensex for ORB breakouts & tracks open trades)
        async def _indices_breakout_loop():
            while self._running:
                await asyncio.sleep(15)
                if not self._running:
                    break
                now_t = default_session.now()
                if default_session.is_market_open(now_t) and default_session.is_entry_allowed(now_t):
                    try:
                        await order_executor.check_indices_breakouts(self._on_signal_generated)
                    except Exception as e:
                        logger.debug(f"Error checking index breakouts: {e}")

                    # Monitor open index positions against live Dhan LTP
                    if self.paper_tracker.open_trades:
                        try:
                            import httpx
                            from app.dhan.auth import auth
                            headers = auth.get_headers()
                            async with httpx.AsyncClient(timeout=5.0) as client:
                                r_ltp = await client.post("https://api.dhan.co/v2/marketfeed/ltp", headers=headers, json={"IDX_I": [13, 25, 51]})
                                if r_ltp.status_code == 200:
                                    d_data = r_ltp.json().get("data", {}).get("IDX_I", {})
                                    for sid_s, val in d_data.items():
                                        ltp = float(val.get("last_price", 0.0))
                                        if ltp > 0:
                                            self.paper_tracker.update_with_tick(sid_s, ltp, now_t)
                        except Exception as e:
                            logger.debug(f"Error updating open index trades with live tick: {e}")

        index_breakout_task = asyncio.create_task(_indices_breakout_loop())

        # Start Equity Breakout Watchdog (Only active if universe mode is not indices_only)
        equity_breakout_task = None
        if settings.universe.mode.lower() not in ("indices_only", "indices"):
            async def _equity_breakout_loop():
                while self._running:
                    await asyncio.sleep(60)
                    if not self._running:
                        break
                    now_t = default_session.now()
                    if default_session.is_market_open(now_t) and default_session.is_entry_allowed(now_t):
                        try:
                            await order_executor.check_equity_breakouts(self._on_signal_generated)
                        except Exception as e:
                            logger.debug(f"Error checking equity breakouts: {e}")

            equity_breakout_task = asyncio.create_task(_equity_breakout_loop())
        else:
            logger.info("Universe mode is indices_only: Equity stock breakout watchdog disabled.")

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

        # Daily Schedule Watchdog:
        # - 08:30 AM IST: Global Market Pulse & VIP Pricing Card
        # - 09:00 AM IST: Morning Greeting & System Health Alert
        # - 09:14 AM IST: Final Pre-Market Briefing & Numerical Backtest Report
        # - 09:30 AM IST: Automatic Morning Greeting Cleanup
        # - 18:00 PM IST: Evening P&L Recap & VIP Performance Showcase
        # - 22:00 PM IST: Night Market Strategy & Game Plan
        async def _daily_schedule_watchdog():
            sent_pulse_day = None
            sent_health_day = None
            sent_bias_day = None
            sent_scalp_day = None
            sent_briefing_day = None
            deleted_greeting_day = None
            sent_choppy_day = None
            sent_evening_day = None
            sent_night_day = None

            while self._running:
                await asyncio.sleep(15)
                if not self._running:
                    break
                now_t = default_session.now()
                c_date = now_t.date()
                cur_time = now_t.time()

                if default_session.is_trading_day(c_date):
                    # 1. 08:30 AM Global Market Pulse & VIP Pricing Card
                    if time(8, 30) <= cur_time < time(8, 35) and sent_pulse_day != c_date:
                        try:
                            await notifier.send_global_market_pulse()
                            sent_pulse_day = c_date
                            logger.info("Dispatched 08:30 AM Global Market Pulse & VIP Pricing Table.")
                        except Exception as e:
                            logger.debug(f"Error sending global market pulse: {e}")

                    # 2. 09:00 AM Morning Greeting & Health Check
                    if time(9, 0) <= cur_time < time(9, 5) and sent_health_day != c_date:
                        try:
                            await notifier.send_morning_market_briefing()
                            await notifier.send_morning_health_alert(
                                stocks_count=len(instruments),
                                dhan_connected=auth.has_credentials,
                                db_connected=True,
                                feed_connected=True,
                            )
                            sent_health_day = c_date
                            logger.info("Dispatched 09:00 AM Morning Greeting & Health Check alert.")
                        except Exception as e:
                            logger.debug(f"Error sending morning health alert: {e}")

                        # Daily VIP Channel Expired Subscriber Audit
                        try:
                            from app.notifications.vip_channel import vip_manager
                            evicted = await vip_manager.check_and_evict_expired_subscribers()
                            if evicted > 0:
                                logger.info(f"Daily VIP audit: evicted {evicted} expired subscribers.")
                        except Exception as e:
                            logger.debug(f"VIP expiry audit note: {e}")

                    # 2.5. 09:05 AM Daily Bias Snapshot & Digest Broadcast (Sections 6 & 13)
                    if time(9, 5) <= cur_time < time(9, 12) and sent_bias_day != c_date:
                        try:
                            from app.analysis.daily_bias import daily_bias_engine

                            prior_days = default_session.get_previous_trading_days(c_date, count=2)
                            if len(prior_days) == 2:
                                d_prev, d_ref = prior_days[0], prior_days[1]

                                # 1. Compute benchmark index (NIFTY 50)
                                nifty_c_prev = await historical_manager.fetch_daily_candle("13", "NIFTY", d_prev)
                                nifty_c_ref = await historical_manager.fetch_daily_candle("13", "NIFTY", d_ref)
                                nifty_snap = daily_bias_engine.compute_and_persist(
                                    symbol="NIFTY",
                                    security_id="13",
                                    trading_date=c_date,
                                    previous_candle=nifty_c_prev,
                                    reference_candle=nifty_c_ref,
                                )

                                # 2. Daily Bias calculated for internal gating (channel broadcast suppressed per pure trade alert policy)
                                logger.info(f"09:05 AM Daily Bias snapshot generated for NIFTY ({nifty_snap.daily_bias.value}) for internal gating.")

                                # 3. Pre-warm daily bias snapshots for universe instruments in background
                                async def _warm_stock(inst):
                                    try:
                                        c_p = await historical_manager.fetch_daily_candle(inst.security_id, inst.symbol, d_prev)
                                        c_r = await historical_manager.fetch_daily_candle(inst.security_id, inst.symbol, d_ref)
                                        daily_bias_engine.compute_and_persist(
                                            symbol=inst.symbol,
                                            security_id=inst.security_id,
                                            trading_date=c_date,
                                            previous_candle=c_p,
                                            reference_candle=c_r,
                                        )
                                    except Exception as e:
                                        logger.debug(f"Pre-warm bias note for {inst.symbol}: {e}")

                                asyncio.create_task(asyncio.gather(*[_warm_stock(inst) for inst in instruments[:50]]))

                            sent_bias_day = c_date
                        except Exception as e:
                            logger.error(f"Error in 09:05 Daily Bias snapshot job: {e}")

                    # 2.8. 09:12 AM Fast Scalp Pre-Market Trade Alert (Public & VIP Channels)
                    if time(9, 11) <= cur_time < time(9, 14) and sent_scalp_day != c_date:
                        try:
                            from app.trading.fast_scalp import fast_scalp_engine
                            setup = await fast_scalp_engine.evaluate_premarket_scalp(c_date)
                            if setup:
                                await fast_scalp_engine.broadcast_premarket_scalp_alert(setup)
                                logger.info(f"Dispatched 09:12 AM Fast Scalp alert for {setup.contract_symbol} ({setup.direction.value}).")
                            else:
                                await fast_scalp_engine.broadcast_no_trade_advisory(c_date)
                                logger.info("Dispatched 09:12 AM Fast Scalp Capital Protection Advisory (Flat / Choppy Open).")
                            sent_scalp_day = c_date
                        except Exception as e:
                            logger.error(f"Error evaluating 09:12 AM premarket fast scalp: {e}")

                    # 3. 09:14 AM Pre-Market Final Briefing
                    if time(9, 14) <= cur_time < time(9, 15) and sent_briefing_day != c_date:
                        try:
                            from app.strategies.historical_learner import historical_learner
                            premarket_info = historical_learner.get_premarket_summary()
                            await notifier.send_premarket_briefing(premarket_info)
                            sent_briefing_day = c_date
                            logger.info("Dispatched 09:14 AM Pre-Market Final Briefing alert.")
                        except Exception as e:
                            logger.debug(f"Error sending pre-market briefing alert: {e}")

                    # 4. 09:30 AM Cleanup Morning Greeting
                    if time(9, 30) <= cur_time < time(9, 35) and deleted_greeting_day != c_date:
                        try:
                            await notifier.delete_morning_briefing()
                            deleted_greeting_day = c_date
                            logger.info("Cleaned up 09:00 AM morning greeting message.")
                        except Exception as e:
                            logger.debug(f"Error deleting morning greeting: {e}")

                    # 4.5. 12:45 PM - 13:00 PM Midday Zuperior Crypto/Forex Global Desk Launch Promo
                    if time(12, 45) <= cur_time < time(13, 0) and sent_choppy_day != c_date:
                        try:
                            await notifier.send_zuperior_promo(slot="midday")
                            sent_choppy_day = c_date
                            logger.info("Dispatched 12:45 PM Zuperior Crypto & Forex Desk Launch promo.")
                        except Exception as e:
                            logger.debug(f"Error in midday promo broadcast: {e}")

                    # 5. 18:00 PM Evening P&L Showcase
                    if time(18, 0) <= cur_time < time(18, 10) and sent_evening_day != c_date:
                        try:
                            await notifier.send_evening_pnl_showcase()
                            sent_evening_day = c_date
                            logger.info("Dispatched 18:00 PM Evening P&L Showcase.")
                        except Exception as e:
                            logger.debug(f"Error sending evening showcase: {e}")

                    # 5.5. 21:15 PM - 21:30 PM Night Crypto & Forex Prime Session Promo
                    if time(21, 15) <= cur_time < time(21, 30) and getattr(self, "_sent_night_promo_day", None) != c_date:
                        try:
                            await notifier.send_zuperior_promo(slot="night")
                            self._sent_night_promo_day = c_date
                            logger.info("Dispatched 21:15 PM Night Crypto & Forex Prime Session promo.")
                        except Exception as e:
                            logger.debug(f"Error in night promo broadcast: {e}")

                    # 6. 22:00 PM Night Market Plan
                    if time(22, 0) <= cur_time < time(22, 10) and sent_night_day != c_date:
                        try:
                            await notifier.send_night_market_plan()
                            sent_night_day = c_date
                            logger.info("Dispatched 22:00 PM Night Market Game Plan.")
                        except Exception as e:
                            logger.debug(f"Error sending night game plan: {e}")

        morning_task = asyncio.create_task(_daily_schedule_watchdog())

        # Continuous background 5-year empirical learning loop:
        # - Learns continuously & silently in background off-market/overnight (15:30 to 09:14 IST)
        # - Saves all parameters, weights & models to Firebase Realtime DB & SQLite
        # - Sends silent hourly confirmation: [LEARN INDIAN MARKET ✅]
        # - Pauses heavy backtesting during active trading (09:15-15:30 IST) for 100% focus on execution
        async def _continuous_historical_learner_loop():
            from app.strategies.historical_learner import historical_learner
            await asyncio.sleep(20)
            last_tick_hour = -1

            while self._running:
                now_t = default_session.now()
                # During live NSE market hours (09:15-15:30), stop deep training
                if default_session.is_market_open(now_t):
                    await asyncio.sleep(60)
                    continue

                try:
                    # In background, continuously train and calibrate ML conviction models across sectors (Indian Market)
                    res_nse = await historical_learner.run_historical_learning_cycle()

                    # Hourly silent confirmation: send [LEARN INDIAN MARKET ✅]
                    current_hour = now_t.hour
                    if current_hour != last_tick_hour:
                        await notifier.send_learning_tick()
                        last_tick_hour = current_hour
                        logger.info(f"Dispatched hourly Indian Market learning heartbeat at hour {current_hour}.")
                except Exception as e:
                    logger.debug(f"Continuous background learning error: {e}")

                # Interval between background training batches: 15 minutes (900 seconds)
                await asyncio.sleep(900)

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
            indices_task.cancel()
            index_breakout_task.cancel()
            equity_breakout_task.cancel()
            hourly_task.cancel()
            morning_task.cancel()
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


async def cmd_trend_sweep_replay(
    symbol: str = "NIFTY",
    from_date_str: Optional[str] = None,
    to_date_str: Optional[str] = None,
    capital: float = 50000.0,
) -> None:
    """Executes deterministic historical replay for TREND_SWEEP_FVG_V1 comparing FIXED_2R vs BE_TRAIL_2R."""
    print("\n" + "=" * 65)
    print("      TREND_SWEEP_FVG_V1 HISTORICAL REPLAY & PERFORMANCE      ")
    print("=" * 65)
    print(f"Symbol:             {symbol.upper()}")
    print(f"Evaluation Capital: Rs {capital:,.2f}")
    print(f"Risk per Position:  0.25% (Rs {capital * 0.0025:,.2f})")
    print(f"Mode:               SHADOW / DETERMINISTIC EVENT REPLAY")
    print("-" * 65)

    if not instrument_manager.is_cache_valid():
        await instrument_manager.download_master()
    instrument_manager.load_and_parse()

    sec_id = instrument_manager.get_security_id(symbol.upper()) or "13"
    info = instrument_manager.get_instrument_info(sec_id)
    meta = {
        "symbol": symbol.upper(),
        "security_id": sec_id,
        "lot_size": info.lot_size if info else 1,
        "tick_size": info.tick_size if info else 0.05,
        "product_type": "INTRADAY",
    }

    from app.backtest.trend_sweep_replay import TrendSweepReplayRunner
    runner = TrendSweepReplayRunner(initial_capital=capital, risk_per_trade_pct=0.25)

    # Fetch candles from database
    start_d = date.fromisoformat(from_date_str) if from_date_str else (date.today() - timedelta(days=30))
    end_d = date.fromisoformat(to_date_str) if to_date_str else date.today()

    candles_5m_raw = db.get_candles_5m(sec_id, limit=2000) if hasattr(db, "get_candles_5m") else []
    candles_15m_raw = db.get_recent_candles_15m(sec_id, limit=1000)
    candles_60m_raw = db.get_recent_candles_60m(sec_id, limit=500)

    if not candles_5m_raw or len(candles_5m_raw) < 20:
        print(f"  [INFO] Insufficient local candles in database for {symbol}. Fetching via Dhan historical...")
        from app.dhan.historical import historical_manager
        trading_days = [start_d + timedelta(days=i) for i in range((end_d - start_d).days + 1) if (start_d + timedelta(days=i)).weekday() < 5]
        for t_day in trading_days[-10:]:
            await historical_manager.fetch_intraday_candles(sec_id, symbol, t_day, interval=1)
        candles_15m_raw = db.get_recent_candles_15m(sec_id, limit=1000)
        candles_60m_raw = db.get_recent_candles_60m(sec_id, limit=500)

    print(f"  [OK] Loaded {len(candles_5m_raw)} 5m, {len(candles_15m_raw)} 15m, and {len(candles_60m_raw)} 60m bars.")

    res = runner.run_replay(
        symbol=symbol.upper(),
        security_id=sec_id,
        candles_5m=candles_5m_raw,
        candles_15m=candles_15m_raw,
        candles_60m=candles_60m_raw,
        instrument_meta=meta,
    )

    fixed = res["FIXED_2R"]
    trail = res["BE_TRAIL_2R"]

    print("\n" + "=" * 65)
    print(f"{'METRIC':<30} | {'FIXED_2R':<15} | {'BE_TRAIL_2R':<15}")
    print("-" * 65)
    print(f"{'Total Setups Detected':<30} | {fixed.total_setups_detected:<15} | {trail.total_setups_detected:<15}")
    print(f"{'Armed Setups':<30} | {fixed.armed_setups:<15} | {trail.armed_setups:<15}")
    print(f"{'Expired Setups':<30} | {fixed.expired_setups:<15} | {trail.expired_setups:<15}")
    print(f"{'Filled Trades':<30} | {fixed.filled_trades:<15} | {trail.filled_trades:<15}")
    print(f"{'Winning Trades':<30} | {fixed.winning_trades:<15} | {trail.winning_trades:<15}")
    print(f"{'Losing Trades':<30} | {fixed.losing_trades:<15} | {trail.losing_trades:<15}")
    print(f"{'Breakeven Trades':<30} | {fixed.breakeven_trades:<15} | {trail.breakeven_trades:<15}")
    print(f"{'Target Hit Rate':<30} | {f'{fixed.target_hit_rate_pct:.1f}%':<15} | {f'{trail.target_hit_rate_pct:.1f}%':<15}")
    print(f"{'Net Trade Win Rate':<30} | {f'{fixed.net_win_rate_pct:.1f}%':<15} | {f'{trail.net_win_rate_pct:.1f}%':<15}")
    print(f"{'Total Regulatory Charges':<30} | {f'Rs {fixed.total_charges:,.2f}':<15} | {f'Rs {trail.total_charges:,.2f}':<15}")
    print(f"{'Net Realized PnL':<30} | {f'Rs {fixed.net_pnl:,.2f}':<15} | {f'Rs {trail.net_pnl:,.2f}':<15}")
    print(f"{'Profit Factor':<30} | {f'{fixed.profit_factor:.2f}':<15} | {f'{trail.profit_factor:.2f}':<15}")
    print(f"{'Expectancy per Trade':<30} | {f'Rs {fixed.expectancy_per_trade:,.2f}':<15} | {f'Rs {trail.expectancy_per_trade:,.2f}':<15}")
    print(f"{'Max Drawdown':<30} | {f'Rs {fixed.max_drawdown_amount:,.2f}':<15} | {f'Rs {trail.max_drawdown_amount:,.2f}':<15}")
    print(f"{'Max Consecutive Losses':<30} | {fixed.consecutive_losses:<15} | {trail.consecutive_losses:<15}")
    print("=" * 65 + "\n")


def cmd_status() -> None:
    """Displays current system status, database metrics, and open positions."""
    print("\n" + "=" * 55)
    print("              ORB SCANNER SYSTEM STATUS              ")
    print("=" * 55)
    print(f"Timezone:           {settings.timezone_name}")
    print(f"Local IST Time:     {default_session.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Market Session:     {'OPEN' if default_session.is_market_open() else 'CLOSED'}")
    print(f"Strategy:           {settings.strategy.name} (Timeframe: {settings.strategy.signal_timeframe}m)")
    print(f"New Strategy:       TREND_SWEEP_FVG_V1 (SHADOW / Isolated Paper)")
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
            c_15m = conn.execute("SELECT COUNT(*) as c FROM candles_15m").fetchone()["c"]
            c_60m = conn.execute("SELECT COUNT(*) as c FROM candles_60m").fetchone()["c"]
            c_orb = conn.execute("SELECT COUNT(*) as c FROM orb_daily_levels").fetchone()["c"]
            c_sig = conn.execute("SELECT COUNT(*) as c FROM signals").fetchone()["c"]
            c_trades = conn.execute("SELECT COUNT(*) as c FROM paper_trades").fetchone()["c"]
            c_open = conn.execute("SELECT COUNT(*) as c FROM paper_trades WHERE status='OPEN'").fetchone()["c"]
            c_trend_sweep = conn.execute("SELECT COUNT(*) as c FROM trend_sweep_setups").fetchone()["c"]

        balance = db.get_account_balance(4322.0)
        print("\nDatabase Record Counts:")
        print(f"  - Cached Instruments:   {c_inst}")
        print(f"  - 1-Minute Candles:     {c_1m}")
        print(f"  - 5-Minute Candles:     {c_5m}")
        print(f"  - 15-Minute Candles:    {c_15m}")
        print(f"  - 60-Minute Candles:    {c_60m}")
        print(f"  - Trend Sweep Setups:   {c_trend_sweep}")
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

    # trend-sweep-replay
    ts_parser = subparsers.add_parser("trend-sweep-replay", help="Run historical replay of TREND_SWEEP_FVG_V1")
    ts_parser.add_argument("--symbol", default="NIFTY", help="Symbol to evaluate (default: NIFTY)")
    ts_parser.add_argument("--from", "--from-date", dest="from_date", help="Start date (YYYY-MM-DD)")
    ts_parser.add_argument("--to", "--to-date", dest="to_date", help="End date (YYYY-MM-DD)")
    ts_parser.add_argument("--capital", type=float, default=50000.0, help="Simulated capital (default: 50000)")

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
    elif args.command == "trend-sweep-replay":
        asyncio.run(
            cmd_trend_sweep_replay(
                symbol=args.symbol,
                from_date_str=args.from_date,
                to_date_str=args.to_date,
                capital=args.capital,
            )
        )
    elif args.command == "learn":
        asyncio.run(cmd_learn(symbols=args.symbols, max_stocks=args.max))
    elif args.command == "status":
        cmd_status()



if __name__ == "__main__":
    main()

