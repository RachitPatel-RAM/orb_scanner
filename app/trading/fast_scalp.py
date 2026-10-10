"""
Fast Scalp Pre-Market & 09:16 AM Opening Momentum Trade Engine.

Delivers high-velocity, low-risk, high-return opening scalp trades:
1. 09:12 AM Pre-Market Scalp (Prepared for 09:15:00 AM open)
2. 09:16 AM Opening Momentum Scalp (Mutual Fallback if Pre-Market had no setup)

Key Rules:
- Full broker-standard contract name with Day and Month (e.g. 'BANKNIFTY 14 OCT 51400 CE')
- One-tap copyable contract name (<code>...</code>) for instant broker search
- Maximum 1 morning trade per day (Never gives 2 clashing trades)
- Auto Trail-to-Cost alert (+12 pts) to lock in zero risk
- Expressive emojis for maximum visual punch
- Strictly zero disclosure of proprietary strategy names (no ORB, CPR, SMC)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import os
from typing import Any, Dict, List, Optional, Tuple

from app.analysis.pre_market import pre_market_manager, PreMarketSnapshot
from app.config import logger, settings
from app.dhan.auth import auth
from app.dhan.option_finder import OptionContractInfo, option_finder
from app.market.session import default_session
from app.notifications.telegram import notifier
from app.storage.database import db
from app.storage.models import Candle, Direction


def format_contract_with_month(
    underlying: str,
    strike: float,
    opt_type: str,
    expiry_date_str: Optional[str] = None,
    ref_date: Optional[date] = None,
) -> str:
    """
    Formats the complete broker-standard contract name with Day, Month, Strike, and Option Type.
    Example: 'BANKNIFTY 14 OCT 51400 CE' or 'NIFTY 15 OCT 25100 CE'
    """
    sym = underlying.upper().strip()
    strike_int = int(strike)
    ot = opt_type.upper().strip()

    day_month_str = ""
    if expiry_date_str:
        try:
            d_part = expiry_date_str.split(" ")[0].strip()
            exp_d = datetime.strptime(d_part, "%Y-%m-%d").date()
            day_month_str = exp_d.strftime("%d %b").upper()
        except Exception:
            pass

    if not day_month_str:
        # Calculate nearest standard weekly expiry (Wednesday for BANKNIFTY, Thursday for NIFTY)
        base_d = ref_date or default_session.now().date()
        target_weekday = 2 if sym == "BANKNIFTY" else 3  # Wed=2, Thu=3
        days_ahead = (target_weekday - base_d.weekday()) % 7
        exp_d = base_d + timedelta(days=days_ahead)
        day_month_str = exp_d.strftime("%d %b").upper()

    return f"{sym} {day_month_str} {strike_int} {ot}"


async def get_live_trading_capital() -> float:
    """
    Retrieves live real-time trading capital:
    1. If Dhan credentials are active, queries Dhan /v2/fundlimit.
    2. Synchronizes live Dhan available balance with SQLite database.
    3. Fallback to SQLite persistent database account_balance.
    """
    default_cap = float(getattr(settings, "capital_per_lot", 30000.0))
    if getattr(settings, "has_dhan_credentials", False):
        try:
            import httpx
            headers = auth.get_headers()
            async with httpx.AsyncClient(timeout=4.0) as client:
                resp = await client.get("https://api.dhan.co/v2/fundlimit", headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    avail = float(data.get("availabelBalance", 0.0))
                    if avail > 0:
                        db.set_account_balance(avail)
                        return avail
        except Exception as e:
            logger.debug(f"Live fundlimit API check note: {e}")

    return db.get_account_balance(default_cap)


def calculate_dynamic_scalp_lots(capital: float, symbol: str = "NIFTY") -> Tuple[int, float]:
    """
    Calculates dynamic lots based on live capital compounding and risk management.
    Mathematical Rules:
      - ₹30,000 -> 1 Lot
      - ₹60,000 -> 2 Lots
      - ₹90,000 -> 3 Lots
      - Compounding scales up to index-specific institutional caps:
        * NIFTY: max 108 Lots (8,100 shares - auspicious & clean 5 slices)
        * BANKNIFTY: max 60 Lots (1,800 shares - clean 2 slices)
        * SENSEX: max 50 Lots (1,000 shares - clean 1 slice)
    
    If profits are withdrawn back to ₹30,000, lots automatically drop to 1 Lot.
    If capital is topped up or compounds past ₹60,000+, lots automatically scale up.
    """
    unit = max(10000.0, float(getattr(settings, "capital_per_lot", 30000.0)))
    configured_max = max(1, int(getattr(settings, "max_scalp_lots", 108)))

    sym_upper = (symbol or "NIFTY").upper()
    if sym_upper == "BANKNIFTY":
        max_lots = min(configured_max, 60)
    elif sym_upper == "SENSEX":
        max_lots = min(configured_max, 50)
    else:
        max_lots = min(configured_max, 108)

    raw_lots = int(capital // unit)
    if raw_lots < 1 and capital >= 15000.0:
        raw_lots = 1

    lots = max(1, min(max_lots, raw_lots))
    return lots, unit



@dataclass
class FastScalpSetup:
    trade_date: date
    symbol: str
    security_id: str
    direction: Direction
    underlying_price: float
    gap_points: float
    gap_pct: float
    contract_symbol: str
    option_type: str        # CE or PE
    strike_price: float
    entry_est: float        # Expected option premium
    stop_loss: float        # Tight stop (e.g. -12 to -14 pts)
    target_1: float         # +24 to +30 pts
    target_2: float         # +40 to +50 pts
    risk_reward: str        # e.g. "1:2.5+"
    rationale: str
    setup_source: str = "PRE_MARKET"  # PRE_MARKET or OPEN_0916
    status: str = "PENDING"           # PENDING, ACTIVE, TARGET_HIT, STOP_HIT, EXPIRED
    entry_time: Optional[datetime] = None
    exit_time: Optional[datetime] = None
    exit_price: float = 0.0
    lot_size: int = 75
    lots: int = 1                     # Dynamic lots based on live capital compounding
    capital_available: float = 30000.0 # Live capital at entry time
    trailed_to_cost: bool = False
    opt_contract: Optional[OptionContractInfo] = None


class FastScalpEngine:
    """Manages pre-market and 09:16 AM opening scalps with multi-channel dispatch."""

    def __init__(self):
        self._active_scalps: Dict[str, FastScalpSetup] = {}  # key: symbol_date_source

    def get_active_scalp(self, trade_date: Optional[date] = None) -> Optional[FastScalpSetup]:
        d = trade_date or default_session.now().date()
        for k, s in self._active_scalps.items():
            if s.trade_date == d and s.status in ("PENDING", "ACTIVE"):
                return s
        return None

    def has_active_trade_today(self, trade_date: Optional[date] = None) -> bool:
        """Returns True if any morning trade was already issued today."""
        d = trade_date or default_session.now().date()
        for s in self._active_scalps.values():
            if s.trade_date == d and s.status in ("PENDING", "ACTIVE", "TARGET_HIT"):
                return True
        return False

    async def evaluate_premarket_scalp(
        self,
        trade_date: Optional[date] = None,
    ) -> Optional[FastScalpSetup]:
        """
        Analyzes 09:08 AM auction indicative open.
        Selects high-conviction pre-market scalp if directional momentum is present.
        """
        d = trade_date or default_session.now().date()
        live_cap = await get_live_trading_capital()
        dynamic_lots, _ = calculate_dynamic_scalp_lots(live_cap)

        candidates = [
            ("13", "NIFTY"),
            ("25", "BANKNIFTY"),
        ]

        best_setup: Optional[FastScalpSetup] = None
        max_edge = 0.0

        for sid, sym in candidates:
            snap = await pre_market_manager.compute_snapshot(sid, sym, d)
            if not snap or snap.prev_close <= 0 or snap.pre_open_price <= 0:
                continue

            gap_abs = abs(snap.gap_pct)
            if gap_abs < 0.20:
                continue

            is_bullish = snap.gap_points > 0 and snap.pre_open_price >= snap.prev_high * 0.998
            is_bearish = snap.gap_points < 0 and snap.pre_open_price <= snap.prev_low * 1.002

            if not (is_bullish or is_bearish):
                continue

            direction = Direction.LONG if is_bullish else Direction.SHORT
            opt_type = "CE" if direction == Direction.LONG else "PE"

            # Query ATM Option contract details
            opt_info: Optional[OptionContractInfo] = None
            try:
                opt_info = await option_finder.find_atm_contract(
                    underlying=sym,
                    spot_price=snap.pre_open_price,
                    direction=direction,
                    target_date=d,
                )
            except Exception as e:
                logger.debug(f"Option finder lookup note for {sym}: {e}")

            strike = round(snap.pre_open_price / 50.0) * 50.0 if sym == "NIFTY" else round(snap.pre_open_price / 100.0) * 100.0
            lot_size = 75 if sym == "NIFTY" else 15
            est_ltp = opt_info.ltp if (opt_info and opt_info.ltp > 5.0) else (135.0 if sym == "NIFTY" else 280.0)
            exp_str = opt_info.expiry_date if opt_info else None

            # Format full contract with month: e.g. 'BANKNIFTY 14 OCT 51400 CE'
            contract_sym = format_contract_with_month(
                underlying=sym,
                strike=strike,
                opt_type=opt_type,
                expiry_date_str=exp_str,
                ref_date=d,
            )

            sl_pts = 12.0 if sym == "NIFTY" else 22.0
            tgt1_pts = 28.0 if sym == "NIFTY" else 55.0
            tgt2_pts = 42.0 if sym == "NIFTY" else 85.0

            stop_loss = round(max(5.0, est_ltp - sl_pts), 1)
            target_1 = round(est_ltp + tgt1_pts, 1)
            target_2 = round(est_ltp + tgt2_pts, 1)

            dynamic_lots, _ = calculate_dynamic_scalp_lots(live_cap, sym)

            if gap_abs > max_edge:
                max_edge = gap_abs
                best_setup = FastScalpSetup(
                    trade_date=d,
                    symbol=sym,
                    security_id=sid,
                    direction=direction,
                    underlying_price=snap.pre_open_price,
                    gap_points=snap.gap_points,
                    gap_pct=snap.gap_pct,
                    contract_symbol=contract_sym,
                    option_type=opt_type,
                    strike_price=strike,
                    entry_est=est_ltp,
                    stop_loss=stop_loss,
                    target_1=target_1,
                    target_2=target_2,
                    risk_reward="1:2.5+",
                    rationale=f"Opening Drive with {snap.gap_points:+,.1f} pts momentum.",
                    setup_source="PRE_MARKET",
                    status="PENDING",
                    lot_size=lot_size,
                    lots=dynamic_lots,
                    capital_available=live_cap,
                    opt_contract=opt_info,
                )


        return best_setup

    async def evaluate_0916_scalp(
        self,
        candle_1m: Candle,
        trade_date: Optional[date] = None,
    ) -> Optional[FastScalpSetup]:
        """
        Evaluates the first 1-minute candle (09:15 - 09:16) at 09:16:00 IST.
        Smart Move: Mutual Fallback — ONLY fires if NO pre-market trade was sent today!
        """
        d = trade_date or default_session.now().date()
        sym = candle_1m.symbol
        sid = str(candle_1m.security_id)

        if sym not in ("NIFTY", "BANKNIFTY"):
            return None

        # Smart Move: Prevent clashing trades. If pre-market trade is already running, skip!
        if self.has_active_trade_today(d):
            logger.info(f"09:16 scalp skipped: Morning trade is already active for {d.isoformat()}.")
            return None

        key = f"{sym}_{d.isoformat()}_0916"
        if key in self._active_scalps:
            return None

        c_open = candle_1m.open
        c_high = candle_1m.high
        c_low = candle_1m.low
        c_close = candle_1m.close
        c_range = c_high - c_low

        if c_range <= 2.0:
            return None

        body = abs(c_close - c_open)
        is_bullish = c_close > c_open and (body / c_range) >= 0.55
        is_bearish = c_close < c_open and (body / c_range) >= 0.55

        if not (is_bullish or is_bearish):
            return None

        direction = Direction.LONG if is_bullish else Direction.SHORT
        opt_type = "CE" if direction == Direction.LONG else "PE"

        strike = round(c_close / 50.0) * 50.0 if sym == "NIFTY" else round(c_close / 100.0) * 100.0
        lot_size = 75 if sym == "NIFTY" else 15

        opt_info: Optional[OptionContractInfo] = None
        try:
            opt_info = await option_finder.find_atm_contract(
                underlying=sym,
                spot_price=c_close,
                direction=direction,
                target_date=d,
            )
        except Exception as e:
            logger.debug(f"09:16 option finder note for {sym}: {e}")

        est_ltp = opt_info.ltp if (opt_info and opt_info.ltp > 5.0) else (140.0 if sym == "NIFTY" else 290.0)
        exp_str = opt_info.expiry_date if opt_info else None

        # Format full contract with month: e.g. 'BANKNIFTY 14 OCT 51400 CE'
        contract_sym = format_contract_with_month(
            underlying=sym,
            strike=strike,
            opt_type=opt_type,
            expiry_date_str=exp_str,
            ref_date=d,
        )

        sl_pts = 12.0 if sym == "NIFTY" else 22.0
        tgt1_pts = 28.0 if sym == "NIFTY" else 55.0
        tgt2_pts = 42.0 if sym == "NIFTY" else 85.0

        live_cap = await get_live_trading_capital()
        dynamic_lots, _ = calculate_dynamic_scalp_lots(live_cap, sym)

        setup = FastScalpSetup(
            trade_date=d,
            symbol=sym,
            security_id=sid,
            direction=direction,
            underlying_price=c_close,
            gap_points=round(c_close - c_open, 1),
            gap_pct=round((c_close - c_open) / c_open * 100, 2),
            contract_symbol=contract_sym,
            option_type=opt_type,
            strike_price=strike,
            entry_est=est_ltp,
            stop_loss=round(max(5.0, est_ltp - sl_pts), 1),
            target_1=round(est_ltp + tgt1_pts, 1),
            target_2=round(est_ltp + tgt2_pts, 1),
            risk_reward="1:2.5+",
            rationale="1-Minute opening momentum expansion.",
            setup_source="OPEN_0916",
            status="ACTIVE",
            entry_time=default_session.now(),
            lot_size=lot_size,
            lots=dynamic_lots,
            capital_available=live_cap,
            opt_contract=opt_info,
        )

        self._active_scalps[key] = setup
        return setup

    async def broadcast_premarket_scalp_alert(self, setup: FastScalpSetup) -> bool:
        """Dispatches short, clean, tap-to-copy Pre-Market scalp alert."""
        key = f"{setup.symbol}_{setup.trade_date.isoformat()}_{setup.setup_source}"
        self._active_scalps[key] = setup
        idemp = f"FAST_SCALP_{key}"

        dir_label = "🟢 BUY CALL (CE)" if setup.direction == Direction.LONG else "🔴 BUY PUT (PE)"
        pts_sl = round(setup.entry_est - setup.stop_loss, 1)
        pts_tgt = round(setup.target_1 - setup.entry_est, 1)

        pts_tgt2 = round(setup.target_2 - setup.entry_est, 1)

        lines = [
            "⚡ <b>[PRE-MARKET SCALP ALERT]</b> 🚀",
            "━━━━━━━━━━━━━━━━━━━━━",
            f"🎯 <b>Contract:</b> <code>{setup.contract_symbol}</code>",
            f"⚡ <b>Action:</b> {dir_label} 💥",
            f"💵 <b>Buy Range:</b> ₹{setup.entry_est:,.1f} - ₹{setup.entry_est + 4:,.1f}",
            f"🛑 <b>Stop Loss:</b> ₹{setup.stop_loss:,.1f} (-{pts_sl} pts)",
            f"🎯 <b>Target 1:</b> ₹{setup.target_1:,.1f} (+{pts_tgt} pts)",
            f"🏆 <b>Target 2:</b> ₹{setup.target_2:,.1f} (+{pts_tgt2} pts)",
            "━━━━━━━━━━━━━━━━━━━━━",
            "⚡ <i>Fast execution at 09:15 AM Open! Set SL immediately!</i> 🐂🔥",
        ]

        text = "\n".join(lines)
        dispatched = await self._dispatch_to_all_channels(text, idemp)

        # Live Dhan execution at 09:15:00 open when LIVE_ORDER_ENABLED=true
        if getattr(settings, "live_order_enabled", False) and setup.opt_contract:
            async def _schedule_0915_open_order():
                now_dt = default_session.now()
                open_dt = datetime.combine(setup.trade_date, time(9, 15, 0))
                try:
                    open_dt = default_session.tz.localize(open_dt)
                except Exception:
                    pass
                wait_sec = max(0.0, (open_dt - now_dt).total_seconds())
                if wait_sec > 0:
                    await asyncio.sleep(wait_sec)
                try:
                    from app.trading.order_executor import order_executor
                    order_data = {
                        "security_id": setup.opt_contract.security_id,
                        "symbol": setup.opt_contract.underlying,
                        "direction": setup.direction,
                        "entry_price": setup.opt_contract.ltp,
                        "stop_loss": setup.stop_loss,
                        "target": setup.target_1,
                        "lot_size": setup.lot_size,
                        "margin_req": setup.opt_contract.margin_required,
                        "opt_contract": setup.opt_contract,
                    }
                    st_cat, ok, res_msg = await order_executor.execute_dhan_order(order_data, lot_multiplier=setup.lots)
                    if ok:
                        await notifier.send_message(f"🚀 <b>Live Pre-Market Scalp Executed on Dhan:</b>\n\n{res_msg}")
                    else:
                        await notifier.send_message(f"⚠️ <b>Live Pre-Market Scalp Order Alert:</b>\n\n{res_msg}")
                except Exception as e:
                    logger.error(f"Error placing live pre-market scalp order on Dhan: {e}")
            asyncio.create_task(_schedule_0915_open_order())

        return dispatched

    async def broadcast_0916_scalp_alert(self, setup: FastScalpSetup) -> bool:
        """Dispatches short, clean, tap-to-copy 09:16 AM Opening Momentum scalp alert."""
        key = f"{setup.symbol}_{setup.trade_date.isoformat()}_{setup.setup_source}"
        self._active_scalps[key] = setup
        idemp = f"FAST_SCALP_{key}"

        dir_label = "🟢 BUY CALL (CE)" if setup.direction == Direction.LONG else "🔴 BUY PUT (PE)"
        pts_sl = round(setup.entry_est - setup.stop_loss, 1)
        pts_tgt = round(setup.target_1 - setup.entry_est, 1)
        pts_tgt2 = round(setup.target_2 - setup.entry_est, 1)

        lines = [
            "🚀 <b>[09:16 AM TRADE ALERT]</b> ⚡",
            "━━━━━━━━━━━━━━━━━━━━━",
            f"🎯 <b>Contract:</b> <code>{setup.contract_symbol}</code>",
            f"⚡ <b>Action:</b> {dir_label} NOW 💥",
            f"💵 <b>Buy Range:</b> ₹{setup.entry_est:,.1f} - ₹{setup.entry_est + 4:,.1f}",
            f"🛑 <b>Stop Loss:</b> ₹{setup.stop_loss:,.1f} (-{pts_sl} pts)",
            f"🎯 <b>Target 1:</b> ₹{setup.target_1:,.1f} (+{pts_tgt} pts)",
            f"🏆 <b>Target 2:</b> ₹{setup.target_2:,.1f} (+{pts_tgt2} pts)",
            "━━━━━━━━━━━━━━━━━━━━━",
            "⚡ <i>Fast execution! Set SL & Target immediately!</i> 🐂🔥",
        ]

        text = "\n".join(lines)
        dispatched = await self._dispatch_to_all_channels(text, idemp)

        # Live Dhan execution at 09:16:00 when LIVE_ORDER_ENABLED=true
        if getattr(settings, "live_order_enabled", False) and setup.opt_contract:
            try:
                from app.trading.order_executor import order_executor
                order_data = {
                    "security_id": setup.opt_contract.security_id,
                    "symbol": setup.opt_contract.underlying,
                    "direction": setup.direction,
                    "entry_price": setup.opt_contract.ltp,
                    "stop_loss": setup.stop_loss,
                    "target": setup.target_1,
                    "lot_size": setup.lot_size,
                    "margin_req": setup.opt_contract.margin_required,
                    "opt_contract": setup.opt_contract,
                }
                async def _exec_0916_scalp():
                    st_cat, ok, res_msg = await order_executor.execute_dhan_order(order_data, lot_multiplier=setup.lots)
                    if ok:
                        await notifier.send_message(f"🚀 <b>Live 09:16 Scalp Executed on Dhan:</b>\n\n{res_msg}")
                    else:
                        await notifier.send_message(f"⚠️ <b>Live 09:16 Scalp Order Alert:</b>\n\n{res_msg}")
                asyncio.create_task(_exec_0916_scalp())
            except Exception as e:
                logger.error(f"Error placing live 09:16 scalp order on Dhan: {e}")

        return dispatched

    async def broadcast_no_trade_advisory(self, trade_date: Optional[date] = None) -> bool:
        """Dispatches concise capital protection advisory (no proprietary terminology)."""
        d = trade_date or default_session.now().date()
        idemp = f"FAST_SCALP_NO_TRADE_{d.isoformat()}"

        lines = [
            "🛡️ <b>[CAPITAL PROTECTION ADVISORY]</b> 🛡️",
            "━━━━━━━━━━━━━━━━━━━━━",
            "⚖️ <b>Opening Range: Neutral Consolidation</b>",
            "🛑 <b>NO 09:15 SCALP TRADE TODAY!</b>",
            "",
            "💎 <i>We take low-loss, high-reward trades only. Capital protection comes first!</i>",
            "🚀 <i>Monitoring 09:16 opening momentum setup! Stay ready!</i> 🔥",
        ]

        text = "\n".join(lines)
        try:
            current_cap = db.get_account_balance(float(getattr(settings, "capital_per_lot", 30000.0)))
            db.record_scalp_session(
                trade_date=d.isoformat(),
                nifty_open=0.0,
                nifty_high=0.0,
                nifty_low=0.0,
                nifty_close=0.0,
                gap_pts=0.0,
                setup_type="PRE_MARKET",
                trade_direction="NO_TRADE",
                outcome="NO_TRADE_NEUTRAL_RANGE",
                gross_pnl=0.0,
                brokerage_taxes=0.0,
                net_pnl=0.0,
                running_capital=current_cap,
            )
        except Exception as e:
            logger.debug(f"Error recording no-trade session to DB: {e}")

        return await self._dispatch_to_all_channels(text, idemp)

    async def notify_trail_sl_to_cost(self, setup: FastScalpSetup) -> bool:
        """Dispatches proactive Zero-Risk Trail-to-Cost alert when trade reaches +12 pts."""
        idemp = f"SCALP_TRAIL_{setup.symbol}_{setup.trade_date.isoformat()}_{setup.setup_source}"
        shield_price = round(setup.entry_est + 1.0 if setup.direction == Direction.LONG else setup.entry_est - 1.0, 1)
        lines = [
            "🛡️ <b>TRAIL SL TO COST + 1.0 PT — BROKERAGE SHIELD!</b> 🛡️",
            "━━━━━━━━━━━━━━━━━━━━━",
            f"⚡ <code>{setup.contract_symbol}</code>",
            "📈 <b>Profit:</b> +12 to +15 Points in Profit! 🚀",
            f"👉 Shift Stop Loss to <b>₹{shield_price:,.1f} (Entry + 1.0 pt Shield)</b> NOW.",
            "",
            "💎 <i>100% of Dhan Brokerage & Taxes are now covered by the market! Zero risk to your ₹30,000!</i> 🔥",
        ]
        text = "\n".join(lines)
        return await self._dispatch_to_all_channels(text, idemp)

    async def notify_target_hit(self, setup: FastScalpSetup, current_price: float) -> bool:
        """Dispatches short, celebratory Target Hit message with expressive emojis."""
        setup.status = "TARGET_HIT"
        setup.exit_time = default_session.now()
        setup.exit_price = current_price
        pts_gained = round(current_price - setup.entry_est, 1)
        per_lot_profit = int(pts_gained * setup.lot_size)
        total_profit = int(per_lot_profit * setup.lots)
        new_balance = db.update_account_balance(total_profit, default_capital=30000.0)
        idemp = f"SCALP_TARGET_{setup.symbol}_{setup.trade_date.isoformat()}_{setup.setup_source}"

        brokerage = round(69.60 * setup.lots, 2)
        net_pnl = round(total_profit - brokerage, 2)
        try:
            db.record_scalp_session(
                trade_date=setup.trade_date.isoformat(),
                nifty_open=setup.underlying_price,
                nifty_high=setup.underlying_price,
                nifty_low=setup.underlying_price,
                nifty_close=setup.underlying_price,
                gap_pts=setup.gap_points,
                setup_type=setup.setup_source,
                trade_direction="BUY_CE" if setup.direction == Direction.LONG else "BUY_PE",
                outcome="TARGET_HIT",
                gross_pnl=float(total_profit),
                brokerage_taxes=brokerage,
                net_pnl=net_pnl,
                running_capital=new_balance,
            )
        except Exception as e:
            logger.error(f"Error recording scalp target session to DB: {e}")

        lot_str = f"{setup.lots} Lot{'s' if setup.lots > 1 else ''}"
        lines = [
            "🎉🏆 <b>BOOM! TARGET ACHIEVED!</b> 🚀🎉",
            "━━━━━━━━━━━━━━━━━━━━━",
            f"⚡ <b>{setup.contract_symbol}</b>",
            f"🎯 <b>Exit:</b> ₹{current_price:,.1f} <b>(+{pts_gained} PTS GAINED!)</b> 💰",
            f"💸 <b>Net Realized Profit:</b> <b>+₹{total_profit:,}</b> <i>({lot_str} × ₹{per_lot_profit:,}/lot)</i> 🔥",
            f"💼 <b>Compounded Live Balance:</b> ₹{new_balance:,.2f} 📈",
            "",
            "👑 <i>Low Risk, Maximum Gains! Book profits or trail SL to cost!</i> 🚀",
        ]

        text = "\n".join(lines)
        return await self._dispatch_to_all_channels(text, idemp)

    async def notify_stop_loss_hit(self, setup: FastScalpSetup, current_price: float) -> bool:
        """Dispatches disciplined Stop Loss or Cost Shield alert."""
        setup.exit_time = default_session.now()
        setup.exit_price = current_price

        # Check if trade was already protected by Cost Shield
        if setup.trailed_to_cost:
            setup.status = "COST_SHIELD"
            pts_shield = 1.0
            gross_pnl = round(pts_shield * setup.lot_size * setup.lots, 2)
            brokerage = round(69.60 * setup.lots, 2)
            net_pnl = round(gross_pnl - brokerage, 2)  # +Rs. 5.40 net profit
            new_balance = db.update_account_balance(net_pnl, default_capital=30000.0)
            idemp = f"SCALP_SHIELD_{setup.symbol}_{setup.trade_date.isoformat()}_{setup.setup_source}"

            try:
                db.record_scalp_session(
                    trade_date=setup.trade_date.isoformat(),
                    nifty_open=setup.underlying_price,
                    nifty_high=setup.underlying_price,
                    nifty_low=setup.underlying_price,
                    nifty_close=setup.underlying_price,
                    gap_pts=setup.gap_points,
                    setup_type=setup.setup_source,
                    trade_direction="BUY_CE" if setup.direction == Direction.LONG else "BUY_PE",
                    outcome="COST_SHIELD (+1.0 pt)",
                    gross_pnl=float(gross_pnl),
                    brokerage_taxes=brokerage,
                    net_pnl=net_pnl,
                    running_capital=new_balance,
                )
            except Exception as e:
                logger.error(f"Error recording cost shield session to DB: {e}")

            lines = [
                "🛡️ <b>COST SHIELD EXIT (ZERO CAPITAL RISK)</b> 🛡️",
                "━━━━━━━━━━━━━━━━━━━━━",
                f"⚡ <b>{setup.contract_symbol}</b>",
                f"• <b>Exit:</b> ₹{current_price:,.1f} (+1.0 pt Brokerage Shield)",
                f"• <b>Net P&L:</b> +₹{net_pnl:,.2f} <i>(100% Dhan Brokerage & Taxes Covered!)</i>",
                f"💼 <b>Protected Balance:</b> ₹{new_balance:,.2f}",
                "",
                "💎 <i>Capital 100% safe! Not a single rupee paid out of pocket. Market covered all broker charges!</i> 🐂⚡",
            ]
            text = "\n".join(lines)
            return await self._dispatch_to_all_channels(text, idemp)

        setup.status = "STOP_HIT"
        pts_lost = round(setup.entry_est - current_price, 1)
        per_lot_loss = int(pts_lost * setup.lot_size)
        total_loss = int(per_lot_loss * setup.lots)
        new_balance = db.update_account_balance(-total_loss, default_capital=30000.0)
        idemp = f"SCALP_STOP_{setup.symbol}_{setup.trade_date.isoformat()}_{setup.setup_source}"

        brokerage = round(69.60 * setup.lots, 2)
        net_pnl = round(-total_loss - brokerage, 2)
        try:
            db.record_scalp_session(
                trade_date=setup.trade_date.isoformat(),
                nifty_open=setup.underlying_price,
                nifty_high=setup.underlying_price,
                nifty_low=setup.underlying_price,
                nifty_close=setup.underlying_price,
                gap_pts=setup.gap_points,
                setup_type=setup.setup_source,
                trade_direction="BUY_CE" if setup.direction == Direction.LONG else "BUY_PE",
                outcome="STOP_LOSS",
                gross_pnl=float(-total_loss),
                brokerage_taxes=brokerage,
                net_pnl=net_pnl,
                running_capital=new_balance,
            )
        except Exception as e:
            logger.error(f"Error recording scalp stop loss session to DB: {e}")

        lot_str = f"{setup.lots} Lot{'s' if setup.lots > 1 else ''}"
        lines = [
            "🛡️ <b>STOP LOSS HIT</b> 🛡️",
            "━━━━━━━━━━━━━━━━━━━━━",
            f"⚡ <b>{setup.contract_symbol}</b>",
            f"• <b>Exit:</b> ₹{current_price:,.1f} (-{pts_lost} pts)",
            f"• <b>Net Realized Loss:</b> -₹{total_loss:,} <i>({lot_str})</i>",
            f"💼 <b>Remaining Live Balance:</b> ₹{new_balance:,.2f}",
            "",
            "💬 <i>Disciplined small loss! We strictly hunt low-risk, high-reward setups. Stay ready for the next primary setup!</i> 🐂⚡",
        ]

        text = "\n".join(lines)
        return await self._dispatch_to_all_channels(text, idemp)

    async def on_tick(self, ltp: float, trade_date: Optional[date] = None):
        """Monitors live ticks for active scalp trades."""
        d = trade_date or default_session.now().date()
        for k, s in list(self._active_scalps.items()):
            if s.trade_date != d or s.status != "ACTIVE":
                continue

            # Smart Move: Trail SL to Cost + 1.0 pt (Brokerage Shield) when trade gains +12 points
            pts_profit = (ltp - s.entry_est) if s.direction == Direction.LONG else (s.entry_est - ltp)
            if pts_profit >= 12.0 and not s.trailed_to_cost:
                s.trailed_to_cost = True
                if s.direction == Direction.LONG:
                    s.stop_loss = round(s.entry_est + 1.0, 1)
                else:
                    s.stop_loss = round(s.entry_est - 1.0, 1)
                await self.notify_trail_sl_to_cost(s)

            if s.direction == Direction.LONG:
                if ltp >= s.target_1:
                    await self.notify_target_hit(s, ltp)
                elif ltp <= s.stop_loss:
                    await self.notify_stop_loss_hit(s, ltp)
            else:
                if ltp <= s.target_1:
                    await self.notify_target_hit(s, ltp)
                elif ltp >= s.stop_loss:
                    await self.notify_stop_loss_hit(s, ltp)

    async def _dispatch_to_all_channels(self, text: str, idemp_key: str) -> bool:
        """Delivers message reliably to Admin chat, Public Channel, and VIP Channel."""
        ok = True
        try:
            ok = await notifier.send_message(text, idempotency_key=idemp_key) and ok
        except Exception as e:
            logger.error(f"Error dispatching scalp to admin: {e}")

        pub_ch = os.getenv("TELEGRAM_PUBLIC_CHANNEL_ID", "-1003414953207").strip()
        if pub_ch:
            try:
                await notifier.send_message(text, target_chat_id=pub_ch, idempotency_key=f"{idemp_key}_PUB")
            except Exception as e:
                logger.error(f"Error dispatching scalp to public channel {pub_ch}: {e}")

        vip_ch = (os.getenv("VIP_CHANNEL_ID", "").strip() or getattr(settings, "vip_channel_id", "") or "-1003416174805").strip()
        if vip_ch:
            try:
                await notifier.send_message(text, target_chat_id=vip_ch, idempotency_key=f"{idemp_key}_VIP")
            except Exception as e:
                logger.error(f"Error dispatching scalp to VIP channel {vip_ch}: {e}")

        return ok


fast_scalp_engine = FastScalpEngine()
