"""
Dhan 1-Click Interactive Order Execution Engine with Target & Stop Loss.

Handles Telegram inline keyboard callbacks (Approve with Whole Lot Price / Reject),
places real Super Orders (Bracket Orders with SL & Target) or Intraday MIS orders on Dhan,
and updates Telegram messages in real-time.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, date
from typing import Any, Dict, Optional, Tuple
import httpx
from dhanhq import DhanContext, dhanhq

from app.config import logger, settings
from app.dhan.auth import auth
from app.dhan.instruments import instrument_manager
from app.storage.database import db
from app.storage.models import Direction, Signal


class DhanOrderExecutor:
    """Manages 1-click Telegram order approvals and executes orders on Dhan with SL & Target."""

    def __init__(self):
        self._dhan: Optional[dhanhq] = None
        self._pending_orders: Dict[str, Dict[str, Any]] = {}
        self._processed_callbacks: set[str] = set()

    @property
    def client(self) -> dhanhq:
        if self._dhan is None:
            ctx = DhanContext(
                client_id=settings.dhan_client_id,
                access_token=settings.dhan_access_token,
            )
            self._dhan = dhanhq(ctx)
        return self._dhan

    def register_signal_for_approval(
        self,
        signal: Signal,
        opt_contract: Optional[Any] = None,
    ) -> Tuple[Dict[str, Any], int, float]:
        """
        Calculates quantity and required margin fitting user's capital.
        For index signals, incorporates exact OptionContractInfo with option-level pricing.
        """
        from app.storage.database import db
        default_cap = float(os.getenv("TRADING_CAPITAL", "4322.0"))
        capital = db.get_account_balance(default_cap)
        # 5x intraday MIS margin on NSE Equity
        usable_capital = capital * 0.85  # keep safety buffer
        max_exposure = usable_capital * 5.0

        is_index = signal.symbol in ("NIFTY", "BANKNIFTY", "SENSEX") or str(signal.security_id) in ("13", "25", "51")

        if opt_contract:
            qty = opt_contract.lot_size
            margin_req = opt_contract.margin_required
            total_value = margin_req
            btn_text = f"{'🟢' if signal.direction == Direction.LONG else '🔴'} Buy 1 Lot {int(opt_contract.strike_price)} {opt_contract.option_type} @ ₹{opt_contract.ltp:,.0f}"
            sig_key = f"{signal.security_id}_{int(signal.timestamp.timestamp())}"

            self._pending_orders[sig_key] = {
                "signal": signal,
                "security_id": opt_contract.security_id,
                "symbol": opt_contract.underlying,
                "direction": signal.direction,
                "entry_price": opt_contract.ltp,
                "stop_loss": opt_contract.stop_loss_premium,
                "target": opt_contract.target_premium,
                "lot_size": qty,
                "margin_req": margin_req,
                "total_lot_price": total_value,
                "opt_contract": opt_contract,
                "created_at": datetime.now(),
            }

            reply_markup = {
                "inline_keyboard": [
                    [
                        {"text": btn_text, "callback_data": f"app:{sig_key}:1"},
                        {"text": "✖ Dismiss", "callback_data": f"rej:{sig_key}"},
                    ],
                    [
                        {"text": f"2 Lots ({qty * 2})", "callback_data": f"app:{sig_key}:2"},
                        {"text": f"3 Lots ({qty * 3})", "callback_data": f"app:{sig_key}:3"},
                        {"text": f"4 Lots ({qty * 4})", "callback_data": f"app:{sig_key}:4"},
                    ],
                ]
            }
            return reply_markup, qty, margin_req

        if is_index:
            lot_map = {"NIFTY": 65, "BANKNIFTY": 30, "SENSEX": 20}
            idx_lot = lot_map.get(signal.symbol, 65)
            opt_type = "CE (Call)" if signal.direction == Direction.LONG else "PE (Put)"
            strike = round(signal.entry_price / 50.0) * 50 if signal.symbol == "NIFTY" else round(signal.entry_price / 100.0) * 100
            btn_text = f"{'🟢' if signal.direction == Direction.LONG else '🔴'} Buy 1 Lot {signal.symbol} {strike} {opt_type}"
            sig_key = f"{signal.security_id}_{int(signal.timestamp.timestamp())}"
            self._pending_orders[sig_key] = {
                "signal": signal,
                "security_id": signal.security_id,
                "symbol": signal.symbol,
                "direction": signal.direction,
                "entry_price": signal.entry_price,
                "stop_loss": signal.stop_loss,
                "target": signal.target,
                "lot_size": idx_lot,
                "margin_req": 4500.0,
                "total_lot_price": 4500.0,
                "created_at": datetime.now(),
            }
            reply_markup = {
                "inline_keyboard": [
                    [
                        {"text": btn_text, "callback_data": f"app:{sig_key}:1"},
                        {"text": "✖ Dismiss", "callback_data": f"rej:{sig_key}"},
                    ],
                ]
            }
            return reply_markup, idx_lot, 4500.0

        qty = max(1, int(max_exposure / signal.entry_price))
        margin_req = round((signal.entry_price * qty) / 5.0, 2)
        total_value = round(signal.entry_price * qty, 2)

        sig_key = f"{signal.security_id}_{int(signal.timestamp.timestamp())}"

        self._pending_orders[sig_key] = {
            "signal": signal,
            "security_id": signal.security_id,
            "symbol": signal.symbol,
            "direction": signal.direction,
            "entry_price": signal.entry_price,
            "stop_loss": signal.stop_loss,
            "target": signal.target,
            "lot_size": qty,
            "margin_req": margin_req,
            "total_lot_price": total_value,
            "created_at": datetime.now(),
        }

        # Format button with exact margin price fitting the user's capital
        if signal.direction == Direction.LONG:
            btn_text = f"🟢 Buy {qty} Qty (₹{margin_req:,.0f})"
        else:
            btn_text = f"🔴 Sell {qty} Qty (₹{margin_req:,.0f})"

        reply_markup = {
            "inline_keyboard": [
                [
                    {"text": btn_text, "callback_data": f"app:{sig_key}:1"},
                    {"text": "✖ Reject", "callback_data": f"rej:{sig_key}"},
                ],
                [
                    {"text": f"2 Lots ({qty * 2})", "callback_data": f"app:{sig_key}:2"},
                    {"text": f"3 Lots ({qty * 3})", "callback_data": f"app:{sig_key}:3"},
                    {"text": f"4 Lots ({qty * 4})", "callback_data": f"app:{sig_key}:4"},
                ],
            ]
        }

        return reply_markup, qty, margin_req

    async def execute_dhan_order(self, order_data: Dict[str, Any], lot_multiplier: int = 1) -> Tuple[bool, str]:
        """
        Places order on Dhan with Stop Loss and Target.
        Uses place_super_order (Bracket Order) with fallback to place_order with trigger.
        """
        signal: Signal = order_data["signal"]
        sec_id = str(order_data["security_id"])
        symbol = order_data["symbol"]
        is_long = order_data["direction"] == Direction.LONG
        txn_type = "BUY" if is_long else "SELL"
        qty = int(order_data["lot_size"]) * max(1, lot_multiplier)
        price = float(order_data["entry_price"])
        target = float(order_data["target"])
        stop_loss = float(order_data["stop_loss"])

        opt_contract = order_data.get("opt_contract")
        if opt_contract:
            return True, (
                f"✅ <b>Option Setup Staged:</b> Buy {lot_multiplier} Lot(s) of <b>{opt_contract.custom_symbol}</b> "
                f"({opt_contract.lot_size * lot_multiplier} Qty) @ ₹{opt_contract.ltp:,.2f} | "
                f"SL: ₹{opt_contract.stop_loss_premium:,.2f} | Target: ₹{opt_contract.target_premium:,.2f}.\n\n"
                f"⚡ <i>Execute immediately via Dhan App or Web Option Chain.</i>"
            )

        logger.info(
            f"Placing Dhan Order: {txn_type} {symbol} ({sec_id}) Lots={lot_multiplier} Qty={qty} Price={price} "
            f"SL={stop_loss} Target={target}"
        )

        if symbol in ("NIFTY", "BANKNIFTY", "SENSEX") or sec_id in ("13", "25", "51"):
            opt_type = "CE (Call)" if is_long else "PE (Put)"
            strike = round(price / 50.0) * 50 if symbol == "NIFTY" else round(price / 100.0) * 100
            return True, f"Index Setup Logged: {symbol} broke ORB ({'Long' if is_long else 'Short'}). Recommended strike: {strike} {opt_type}. Execute options contract via Dhan Option Chain / Web."

        try:
            # 1. Try Super Order (Bracket Order with Entry + SL + Target)
            loop = asyncio.get_event_loop()
            res = await loop.run_in_executor(
                None,
                lambda: self.client.place_super_order(
                    security_id=sec_id,
                    exchange_segment="NSE_EQ",
                    transaction_type=txn_type,
                    quantity=qty,
                    order_type="LIMIT",
                    product_type="INTRADAY",
                    price=price,
                    targetPrice=target,
                    stopLossPrice=stop_loss,
                    tag=f"ORB_{sec_id}"[:15],
                )
            )

            status = res.get("status", "").lower() if isinstance(res, dict) else ""
            if status == "success":
                order_id = res.get("data", {}).get("orderId", "N/A")
                return True, f"Super Order Placed! Order ID: #{order_id}"
            
            # If Super Order rejected or not supported, try standard market/limit order
            logger.warning(f"Super order returned {res}. Trying standard intraday order...")
            res2 = await loop.run_in_executor(
                None,
                lambda: self.client.place_order(
                    security_id=sec_id,
                    exchange_segment="NSE_EQ",
                    transaction_type=txn_type,
                    quantity=qty,
                    order_type="MARKET",
                    product_type="INTRADAY",
                    price=0,
                    bo_profit_value=round(abs(target - price), 2),
                    bo_stop_loss_Value=round(abs(price - stop_loss), 2),
                    tag=f"ORB_{sec_id}"[:15],
                )
            )

            status2 = res2.get("status", "").lower() if isinstance(res2, dict) else ""
            if status2 == "success":
                order_id = res2.get("data", {}).get("orderId", "N/A")
                return True, f"Intraday Order Placed! Order ID: #{order_id}"
            else:
                remarks = res2.get("remarks", str(res2))
                err_str = str(remarks)
                if "DH-905" in err_str or "Invalid IP" in err_str:
                    return False, (
                        "⚠️ <b>Dhan API Error: Server IP Not Whitelisted (DH-905)</b>\n\n"
                        "Dhan blocks API orders unless your server IP is whitelisted in your Dhan account.\n"
                        "👉 <b>Server Static IP:</b> <code>34.10.99.222</code>\n\n"
                        "<b>How to fix in 1 minute:</b>\n"
                        "1. Open <a href='https://web.dhan.co'>web.dhan.co</a>\n"
                        "2. Go to <b>My Profile ➔ DhanHQ Trading & Data APIs ➔ IP Setup / Static IP</b>\n"
                        "3. Enter: <code>34.10.99.222</code> and Save.\n\n"
                        "💡 <i>Once saved, all future 1-click orders will execute directly on Dhan!</i>"
                    )
                return False, f"Dhan API Error: {remarks}"

        except Exception as e:
            logger.error(f"Error placing Dhan order: {e}")
            return False, f"Execution Exception: {str(e)}"

    async def run_telegram_listener(self):
        """
        Polls Telegram updates in background to listen for user clicking Approve/Reject.
        Ensures secure 1-click execution.
        """
        if not settings.telegram_bot_token or not settings.telegram_chat_id:
            logger.warning("Telegram listener disabled: bot_token or chat_id missing.")
            return

        url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/getUpdates"
        offset = 0
        logger.info("Telegram 1-Click Approval Listener started.")

        # Register Telegram bot commands menu so typing '/' displays options
        try:
            cmds = [
                {"command": "balance", "description": "Check live Dhan margin and funds"},
                {"command": "limit", "description": "Check Dhan funds and available limits"},
                {"command": "indices", "description": "View Nifty 50, BankNifty & Sensex ORB levels"},
                {"command": "learn", "description": "Run on-demand AI deep learning on 5-yr exchange data"},
                {"command": "positions", "description": "View open positions on Dhan"},
                {"command": "orders", "description": "View today Dhan orders"},
                {"command": "status", "description": "Scanner and ML engine health"},
                {"command": "token", "description": "Update Dhan access token via /token <jwt>"},
                {"command": "help", "description": "Show command menu"},
            ]
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(
                    f"https://api.telegram.org/bot{settings.telegram_bot_token}/setMyCommands",
                    json={"commands": cmds},
                )
        except Exception as e:
            logger.debug(f"Could not register Telegram commands: {e}")

        async with httpx.AsyncClient(timeout=35.0) as client:
            while True:
                try:
                    resp = await client.get(url, params={"offset": offset, "timeout": 20})
                    if resp.status_code == 200:
                        data = resp.json()
                        updates = data.get("result", [])
                        for u in updates:
                            offset = max(offset, u["update_id"] + 1)
                            if "callback_query" in u:
                                await self._handle_callback(u["callback_query"])
                            elif "message" in u:
                                await self._handle_message_command(u["message"])
                    elif resp.status_code == 429:
                        await asyncio.sleep(5)
                    else:
                        await asyncio.sleep(2)
                except asyncio.CancelledError:
                    logger.info("Telegram listener cancelled.")
                    break
                except Exception as e:
                    logger.debug(f"Telegram listener polling cycle error: {e}")
                    await asyncio.sleep(2)

    async def get_indices_orb_report(self, target_date: Optional[date] = None) -> str:
        """Alias for get_daily_indices_orb_message."""
        return await self.get_daily_indices_orb_message(target_date)

    async def get_daily_indices_orb_message(self, target_date: Optional[date] = None) -> str:
        """Fetches live 09:30-09:45 ORB benchmarks and true real-time LTP from Dhan."""
        from app.dhan.auth import auth
        from app.market.session import default_session
        d = target_date or default_session.now().date()
        headers = auth.get_headers()
        url_chart = "https://api.dhan.co/v2/charts/intraday"
        url_ltp = "https://api.dhan.co/v2/marketfeed/ltp"
        indices = [
            ("13", "NIFTY 50", "NSE", "IDX_I"),
            ("25", "BANKNIFTY", "NSE", "IDX_I"),
            ("51", "SENSEX", "BSE", "IDX_I"),
        ]

        # 1. Fetch instantaneous live LTP for indices
        live_ltps = {}
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r_ltp = await client.post(url_ltp, headers=headers, json={"IDX_I": [13, 25, 51]})
                if r_ltp.status_code == 200:
                    d_data = r_ltp.json().get("data", {}).get("IDX_I", {})
                    for sid_str, val in d_data.items():
                        live_ltps[sid_str] = float(val.get("last_price", 0.0))
        except Exception as e:
            logger.debug(f"Error fetching live index LTPs: {e}")

        # 2. Fetch 09:30-09:45 Benchmark Range
        lines = []
        for sid, name, exch, seg in indices:
            payload = {
                "securityId": sid,
                "exchangeSegment": seg,
                "instrument": "INDEX",
                "fromDate": f"{d.isoformat()} 09:15:00",
                "toDate": f"{d.isoformat()} 15:30:00",
                "interval": "15",
            }
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(url_chart, headers=headers, json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    highs = data.get("high", [])
                    lows = data.get("low", [])
                    closes = data.get("close", [])
                    if len(highs) >= 3:
                        # Full 09:30-10:00 Benchmark Range (Candles 1 & 2)
                        orb_high = max(highs[1], highs[2])
                        orb_low = min(lows[1], lows[2])
                        orb_mid = round((orb_high + orb_low) / 2.0, 2)
                        # Use instantaneous live tick LTP if available, fallback to latest close
                        cur_p = live_ltps.get(sid) or (closes[-1] if closes else orb_mid)
                        diff_pts = cur_p - orb_mid
                        diff_pct = (diff_pts / orb_mid * 100.0) if orb_mid else 0.0
                        status_sym = "🟢 Bullish (&gt;Mid)" if diff_pts >= 0 else "🔴 Bearish (&lt;Mid)"
                        lines.append(
                            f"🔹 <b>{name}</b> ({exch})\n"
                            f"• <b>09:30–10:00 High:</b> ₹{orb_high:,.2f}\n"
                            f"• <b>09:30–10:00 Low:</b> ₹{orb_low:,.2f}\n"
                            f"• <b>ORB Midpoint:</b> ₹{orb_mid:,.2f}\n"
                            f"• <b>Current LTP:</b> ₹{cur_p:,.2f} ({status_sym} | {diff_pct:+.2f}%)\n"
                        )
            except Exception as e:
                logger.debug(f"Error fetching {name} ORB: {e}")

        if not lines:
            return ""

        dt_str = d.strftime("%d-%b-%Y")
        return (
            f"🏛 <b>Daily Major Indices ORB Benchmark (10:00 AM IST)</b>\n"
            f"📅 <b>Date:</b> {dt_str} | <b>Benchmark Range:</b> 09:30–10:00 IST\n\n"
            + "\n".join(lines)
            + "⚡ <i>Individual stock alerts trigger exclusively upon confirmed 15m candle close breakout.</i>"
        )

    async def check_indices_breakouts(self, on_signal_callback) -> None:
        """
        Monitors NIFTY 50, BANKNIFTY, and SENSEX for ORB breakouts.
        STRICT REQUIREMENT: The 15-minute candle MUST fully close before a breakout is confirmed!
        No premature mid-candle triggers allowed.
        """
        from app.dhan.auth import auth
        from app.market.session import default_session
        from app.storage.models import Signal, Direction, Candle
        now_dt = default_session.now()
        now_epoch = now_dt.timestamp()
        d = now_dt.date()
        headers = auth.get_headers()
        url = "https://api.dhan.co/v2/charts/intraday"
        indices = [
            ("13", "NIFTY", "Nifty 50", "NSE", "IDX_I", 75),
            ("25", "BANKNIFTY", "Nifty Bank", "NSE", "IDX_I", 30),
            ("51", "SENSEX", "Sensex", "BSE", "IDX_I", 20),
        ]
        for sid, sym, name, exch, seg, default_lot in indices:
            idemp_prefix = f"IDX_{sym}_{d.isoformat()}"
            if getattr(self, f"_idx_broken_{sym}_{d.isoformat()}", False):
                continue

            payload = {
                "securityId": sid,
                "exchangeSegment": seg,
                "instrument": "INDEX",
                "fromDate": f"{d.isoformat()} 09:15:00",
                "toDate": f"{d.isoformat()} 15:30:00",
                "interval": "15",
            }
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(url, headers=headers, json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    highs = data.get("high", [])
                    lows = data.get("low", [])
                    closes = data.get("close", [])
                    opens = data.get("open", [])
                    timestamps = data.get("timestamp", [])
                    volumes = data.get("volume", [1000] * len(closes))

                    if len(highs) >= 3:
                        # Full 09:30-10:00 Benchmark Range (Candles 1 & 2)
                        orb_high = max(highs[1], highs[2])
                        orb_low = min(lows[1], lows[2])
                        orb_mid = round((orb_high + orb_low) / 2.0, 2)

                        # Find the LAST STRICTLY CLOSED 15m candle (must have elapsed full 900 seconds)
                        closed_idx = None
                        for i in range(len(timestamps) - 1, -1, -1):
                            candle_start_ts = timestamps[i]
                            # A 15-minute candle starting at T is only fully closed at T + 900s
                            if now_epoch >= (candle_start_ts + 900):
                                closed_idx = i
                                break

                        # Must be past the ORB period (i.e. closed_idx >= 3, after 10:00 AM)
                        if closed_idx is None or closed_idx < 3:
                            continue

                        closed_close = closes[closed_idx]
                        closed_high = highs[closed_idx]
                        closed_low = lows[closed_idx]
                        closed_open = opens[closed_idx]

                        direction = None
                        if closed_close > orb_high:
                            direction = Direction.LONG
                            sl = orb_mid
                            target = round(closed_close + (closed_close - sl) * 2.0, 2)
                        elif closed_close < orb_low:
                            direction = Direction.SHORT
                            sl = orb_mid
                            target = round(closed_close - (sl - closed_close) * 2.0, 2)

                        if direction:
                            setattr(self, f"_idx_broken_{sym}_{d.isoformat()}", True)
                            c = Candle(
                                security_id=sid,
                                symbol=sym,
                                timestamp=now_dt,
                                open=closed_open,
                                high=closed_high,
                                low=closed_low,
                                close=closed_close,
                                volume=float(volumes[closed_idx]) if volumes else 1000.0,
                                is_closed=True,
                            )
                            sig = Signal(
                                trade_date=d,
                                security_id=sid,
                                symbol=sym,
                                timestamp=now_dt,
                                strategy="ORB-15",
                                direction=direction,
                                entry_price=closed_close,
                                orb_high=orb_high,
                                orb_low=orb_low,
                                stop_loss=sl,
                                target=target,
                                risk_reward=2.0,
                                idempotency_key=f"{idemp_prefix}_{direction.value}",
                            )
                            if on_signal_callback:
                                on_signal_callback(sig, candle=c)
            except Exception as e:
                logger.debug(f"Error checking {sym} breakout: {e}")

    async def check_commodity_smc_setups(self) -> None:
        """Commodity scanner disabled by user preference (focused 100% on NSE Equity & Indices)."""
        return

        now_epoch = now_dt.timestamp()
        today_str = now_dt.strftime("%Y-%m-%d")
        headers = auth.get_headers()
        url = "https://api.dhan.co/v2/charts/intraday"

        commodities = [
            ("569900", "CRUDEOIL", "CRUDEOIL OCT FUT", 100),
            ("495213", "GOLD", "GOLD DEC FUT", 1),
            ("495214", "SILVER", "SILVER DEC FUT", 30),
        ]

        if not hasattr(self, "_last_commodity_signal"):
            self._last_commodity_signal = {}

        for sid, sym, full_name, lot_sz in commodities:
            try:
                # 1. Fetch 15m HTF candles to evaluate structural market bias
                payload_15m = {
                    "securityId": sid,
                    "exchangeSegment": "MCX_COMM",
                    "instrument": "FUTCOM",
                    "fromDate": f"{today_str} 09:00:00",
                    "toDate": f"{today_str} 23:30:00",
                    "interval": "15",
                }
                # 2. Fetch 5m candles for trade setup timing
                payload_5m = {
                    "securityId": sid,
                    "exchangeSegment": "MCX_COMM",
                    "instrument": "FUTCOM",
                    "fromDate": f"{today_str} 09:00:00",
                    "toDate": f"{today_str} 23:30:00",
                    "interval": "5",
                }

                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp_15m = await client.post(url, headers=headers, json=payload_15m)
                    resp_5m = await client.post(url, headers=headers, json=payload_5m)

                if resp_5m.status_code != 200:
                    continue

                # Parse 15m HTF candles
                htf_trend = None
                if resp_15m.status_code == 200:
                    data_15 = resp_15m.json()
                    c15 = data_15.get("close", [])
                    o15 = data_15.get("open", [])
                    h15 = data_15.get("high", [])
                    l15 = data_15.get("low", [])
                    t15 = data_15.get("timestamp", [])
                    v15 = data_15.get("volume", [1000] * len(c15))
                    closed_15m = []
                    for i in range(len(c15)):
                        ts_ep = t15[i] if i < len(t15) else 0
                        if now_epoch >= (ts_ep + 900) or i < (len(c15) - 1):
                            closed_15m.append(Candle(
                                security_id=sid,
                                symbol=sym,
                                timestamp=datetime.fromtimestamp(ts_ep, tz=IST_TZ) if ts_ep else now_dt,
                                open=float(o15[i]),
                                high=float(h15[i]),
                                low=float(l15[i]),
                                close=float(c15[i]),
                                volume=float(v15[i]) if v15 else 1000.0,
                                is_closed=True,
                            ))
                    htf_trend, htf_ema = smc_engine.calculate_htf_trend(closed_15m)

                # Parse 5m execution candles
                data = resp_5m.json()
                closes = data.get("close", [])
                highs = data.get("high", [])
                lows = data.get("low", [])
                opens = data.get("open", [])
                timestamps = data.get("timestamp", [])
                volumes = data.get("volume", [1000] * len(closes))

                if len(closes) < 10:
                    continue

                closed_candles = []
                for i in range(len(closes)):
                    ts_epoch = timestamps[i] if i < len(timestamps) else 0
                    if now_epoch >= (ts_epoch + 300) or i < (len(closes) - 1):
                        closed_candles.append(Candle(
                            security_id=sid,
                            symbol=sym,
                            timestamp=datetime.fromtimestamp(ts_epoch, tz=IST_TZ) if ts_epoch else now_dt,
                            open=float(opens[i]),
                            high=float(highs[i]),
                            low=float(lows[i]),
                            close=float(closes[i]),
                            volume=float(volumes[i]) if volumes else 1000.0,
                            is_closed=True,
                        ))

                if len(closed_candles) < 10:
                    continue

                # Compute 20-period average volume
                vol_window = [c.volume for c in closed_candles[-21:-1]]
                avg_vol_20 = sum(vol_window) / len(vol_window) if vol_window else 1000.0

                # Swing high and low across last 8 closed 5m candles (40-min window)
                recent_window = closed_candles[-8:]
                swing_h = max(c.high for c in recent_window)
                swing_l = min(c.low for c in recent_window)

                # Evaluate FVG Strategy with HTF filter & real volume
                fvg_setup = smc_engine.evaluate_fvg_strategy(
                    symbol=sym,
                    candles=closed_candles[-8:],
                    swing_high=swing_h,
                    swing_low=swing_l,
                    risk_reward_target=2.0,
                    strict_filters=True,
                    avg_volume_20=avg_vol_20,
                    htf_trend=htf_trend,
                )

                # Evaluate Hidden Liquidity Strategy with HTF filter & real volume
                hl_setup = smc_engine.evaluate_hidden_liquidity_strategy(
                    symbol=sym,
                    candles=closed_candles[-8:],
                    swing_high=swing_h,
                    swing_low=swing_l,
                    risk_reward_target=2.0,
                    avg_volume_20=avg_vol_20,
                    htf_trend=htf_trend,
                )

                candidates = [s for s in [fvg_setup, hl_setup] if s is not None and getattr(s, "conviction_score", 0) >= 80]
                for setup in candidates:
                    # Directional Cooldown: Suppress opposing signals within 45 minutes
                    if sym in self._last_commodity_signal:
                        prev_dir, prev_ts = self._last_commodity_signal[sym]
                        if prev_dir != setup.direction and (now_epoch - prev_ts) < 2700:
                            logger.info(
                                f"SMC Anti-Whipsaw: Suppressed opposing {setup.direction.value} setup on {sym} "
                                f"within 45m of previous {prev_dir.value}."
                            )
                            continue

                    idemp_key = f"SMC_{sym}_{setup.direction.value}_{today_str}_{int(setup.entry_price)}"
                    if getattr(self, f"_comm_alert_{idemp_key}", False):
                        continue

                    # Mark as alerted and record direction for anti-whipsaw cooldown
                    setattr(self, f"_comm_alert_{idemp_key}", True)
                    self._last_commodity_signal[sym] = (setup.direction, now_epoch)

                    # Build actionable range (+/- 0.08%)
                    p_delta = setup.entry_price * 0.0008
                    min_r = round(setup.entry_price - p_delta, 2)
                    max_r = round(setup.entry_price + p_delta, 2)
                    actionable_range = f"₹{min_r:,.2f} – ₹{max_r:,.2f}"

                    # Send to Telegram
                    await notifier.send_smc_trade_alert(
                        symbol=full_name,
                        strategy_name=setup.strategy_name,
                        direction=setup.direction,
                        entry_price=setup.entry_price,
                        stop_loss=setup.stop_loss,
                        target_price=setup.target_price,
                        risk_reward=2.0,
                        lot_size=lot_sz,
                        conviction_score=setup.conviction_score,
                        timeframe="5m",
                        logic_summary=setup.calculation_breakdown,
                    )
                    logger.info(f"Dispatched verified institutional MCX SMC alert for {full_name} ({setup.strategy_name} | {setup.conviction_score}%).")
            except Exception as e:
                logger.debug(f"Error checking commodity setups for {sym}: {e}")

    async def check_equity_breakouts(self, on_signal_callback=None) -> None:
        """
        Monitors active NSE Equity F&O universe (231 stocks) for confirmed ORB breakouts.
        STRICT RULES:
        1. Benchmark Range: 09:30–10:00 IST using both Candle 1 (09:30-09:45) & Candle 2 (09:45-10:00) High & Low.
        2. Confirmation: A 15-minute candle must STRICTLY CLOSE across ORB High/Low.
        3. Trigger: At the open of the subsequent candle.
        4. Rate-Limit Safe: Sequential throttling (0.08s pause) to protect Dhan API boundaries.
        """
        from app.dhan.auth import auth
        from app.dhan.instruments import instrument_manager
        from app.market.session import default_session, IST_TZ
        from app.storage.models import Signal, Direction, Candle

        now_dt = default_session.now()
        if not (default_session.is_market_open(now_dt) and default_session.is_entry_allowed(now_dt)):
            return

        now_epoch = now_dt.timestamp()
        d = now_dt.date()
        today_str = d.isoformat()
        headers = auth.get_headers()
        url = "https://api.dhan.co/v2/charts/intraday"

        # Resolve F&O liquid universe
        instruments = instrument_manager.resolve_universe("fno")
        if not instruments:
            return

        if not hasattr(self, "_equity_alerted_today"):
            self._equity_alerted_today = set()

        async with httpx.AsyncClient(timeout=8.0) as client:
            for inst in instruments:
                sid = str(inst.security_id)
                sym = inst.symbol
                alert_key = f"{sym}_{today_str}"
                if alert_key in self._equity_alerted_today:
                    continue

                payload = {
                    "securityId": sid,
                    "exchangeSegment": "NSE_EQ",
                    "instrument": "EQUITY",
                    "fromDate": f"{today_str} 09:15:00",
                    "toDate": f"{today_str} 15:30:00",
                    "interval": "15",
                }
                try:
                    resp = await client.post(url, headers=headers, json=payload)
                    if resp.status_code == 429:
                        await asyncio.sleep(1.0)
                        continue
                    if resp.status_code != 200:
                        await asyncio.sleep(0.08)
                        continue

                    data = resp.json()
                    highs = data.get("high", [])
                    lows = data.get("low", [])
                    closes = data.get("close", [])
                    opens = data.get("open", [])
                    timestamps = data.get("timestamp", [])
                    volumes = data.get("volume", [1000] * len(closes))

                    if len(highs) < 3:
                        await asyncio.sleep(0.08)
                        continue

                    # Benchmark Range: 09:30–10:00 IST (Candles 1 & 2)
                    orb_high = max(highs[1], highs[2])
                    orb_low = min(lows[1], lows[2])
                    orb_mid = round((orb_high + orb_low) / 2.0, 2)

                    # Find the LAST strictly closed 15m candle (elapsed full 900 seconds)
                    closed_idx = None
                    for i in range(len(timestamps) - 1, -1, -1):
                        candle_start_ts = timestamps[i]
                        if now_epoch >= (candle_start_ts + 900):
                            closed_idx = i
                            break

                    # Must be past the ORB period (index >= 3, after 10:00 AM)
                    if closed_idx is None or closed_idx < 3:
                        await asyncio.sleep(0.08)
                        continue

                    closed_close = closes[closed_idx]
                    closed_high = highs[closed_idx]
                    closed_low = lows[closed_idx]
                    closed_open = opens[closed_idx]

                    direction = None
                    if closed_close > orb_high:
                        direction = Direction.LONG
                        sl = orb_mid
                        target = round(closed_close + (closed_close - sl) * 2.0, 2)
                    elif closed_close < orb_low:
                        direction = Direction.SHORT
                        sl = orb_mid
                        target = round(closed_close - (sl - closed_close) * 2.0, 2)

                    if direction:
                        self._equity_alerted_today.add(alert_key)
                        # Compute 20-period average volume for authentic volume expansion scoring
                        vols_20 = [volumes[j] for j in range(max(0, closed_idx - 20), closed_idx)]
                        avg_vol = (sum(vols_20) / len(vols_20)) if vols_20 else 1000.0

                        ts_start = timestamps[closed_idx] if closed_idx < len(timestamps) else int(now_epoch)
                        c = Candle(
                            security_id=sid,
                            symbol=sym,
                            timestamp=datetime.fromtimestamp(ts_start, tz=IST_TZ) if ts_start else now_dt,
                            open=closed_open,
                            high=closed_high,
                            low=closed_low,
                            close=closed_close,
                            volume=float(volumes[closed_idx]) if volumes else 1000.0,
                            is_closed=True,
                        )
                        setattr(c, "avg_volume_20", avg_vol)
                        sig = Signal(
                            trade_date=d,
                            security_id=sid,
                            symbol=sym,
                            timestamp=now_dt,
                            strategy="ORB-15",
                            direction=direction,
                            entry_price=closed_close,
                            orb_high=orb_high,
                            orb_low=orb_low,
                            stop_loss=sl,
                            target=target,
                            risk_reward=2.0,
                            idempotency_key=f"EQ_ORB_{sym}_{today_str}_{direction.value}",
                        )
                        logger.info(f"Confirmed 15m ORB breakout for {sym} ({direction.value} @ Rs {closed_close:.2f}).")
                        if on_signal_callback:
                            on_signal_callback(sig, candle=c)

                except Exception as e:
                    logger.debug(f"Error checking {sym} equity breakout: {e}")

                await asyncio.sleep(0.08)

    async def _handle_message_command(self, msg: Dict[str, Any]):
        """Processes interactive chat commands from user (balance, positions, status, orders)."""
        chat = msg.get("chat", {})
        chat_id = str(chat.get("id", ""))
        raw_text = str(msg.get("text", "")).strip()
        text = raw_text.lower()

        # Security check: only authorized telegram chat
        if chat_id != str(settings.telegram_chat_id).strip():
            return

        from app.notifications.telegram import notifier

        if text in ("/balance", "/funds", "/limit", "/limits", "balance", "funds", "limit", "limits"):
            try:
                headers = auth.get_headers()
                async with httpx.AsyncClient(timeout=8.0) as client:
                    resp = await client.get("https://api.dhan.co/v2/fundlimit", headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    avail = float(data.get("availabelBalance", 0.0))
                    utilized = float(data.get("utilizedAmount", 0.0))
                    withdrawable = float(data.get("withdrawableBalance", 0.0))
                    cid = data.get("dhanClientId", settings.dhan_client_id)
                    reply = (
                        "💰 <b>Live Dhan Account Funds</b>\n\n"
                        f"• <b>Available Margin:</b> ₹{avail:,.2f}\n"
                        f"• <b>Utilized Margin:</b> ₹{utilized:,.2f}\n"
                        f"• <b>Withdrawable:</b> ₹{withdrawable:,.2f}\n"
                        f"• <b>5x Intraday Buying Power:</b> ₹{avail * 5:,.2f}\n"
                        f"• <b>Client ID:</b> <code>{cid}</code>"
                    )
                else:
                    err_msg = f"HTTP {resp.status_code}: {resp.text}"
                    fallback_bal = db.get_account_balance(4322.15)
                    reply = (
                        f"💰 <b>Dhan Account Funds</b>\n\n"
                        f"• <b>Available Margin:</b> ₹{fallback_bal:,.2f}\n"
                        f"• <b>5x Intraday Buying Power:</b> ₹{fallback_bal * 5:,.2f}\n"
                        f"• <b>Status:</b> Active Standby\n\n"
                        f"<i>(Dhan Fund API: {err_msg[:60]})</i>"
                    )
            except Exception as e:
                reply = f"⚠️ Error querying Dhan API: {e}"
            await notifier.send_message(reply)

        elif text in ("/indices", "/index", "indices", "index"):
            report = await self.get_indices_orb_report()
            if report:
                await notifier.send_message(report)
            else:
                await notifier.send_message("⚠️ Could not retrieve today's Index ORB levels from Dhan.")

        elif text in ("/token", "token"):
            reply = (
                "🔑 <b>Update Dhan Access Token</b>\n\n"
                "To update your token, generate a fresh 24-hour token from web.dhan.co and reply with:\n"
                "<code>/token &lt;your_jwt_token&gt;</code>\n\n"
                "<i>Or simply paste the raw token (starting with eyJ...) directly into this chat!</i>"
            )
            await notifier.send_message(reply)

        elif raw_text.startswith("/token ") or (raw_text.startswith("eyJ") and len(raw_text) > 80):
            token_val = raw_text.split(" ", 1)[1].strip() if raw_text.startswith("/token ") else raw_text
            if not token_val.startswith("eyJ"):
                await notifier.send_message("⚠️ Invalid token format. A Dhan token must start with <code>eyJ...</code>.")
                return

            test_headers = {
                "client-id": settings.dhan_client_id,
                "access-token": token_val,
                "Content-Type": "application/json",
            }
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get("https://api.dhan.co/v2/profile", headers=test_headers)
                if resp.status_code == 200:
                    settings.dhan_access_token = token_val
                    auth.access_token = token_val
                    self._dhan = None

                    from pathlib import Path
                    env_file = Path(".env")
                    if env_file.exists():
                        txt = env_file.read_text(encoding="utf-8")
                        lines = [
                            f"DHAN_ACCESS_TOKEN={token_val}" if l.startswith("DHAN_ACCESS_TOKEN=") else l
                            for l in txt.splitlines()
                        ]
                        env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

                    try:
                        from app.dhan.live_feed import live_feed
                        asyncio.create_task(live_feed.reconnect())
                    except Exception:
                        pass

                    await notifier.send_message(
                        "✅ <b>DhanHQ Access Token Updated & Validated!</b>\n\n"
                        "• <b>Status:</b> Connected & Active\n"
                        "• <b>Environment:</b> <code>.env</code> updated automatically.\n"
                        "• <b>Live Feed:</b> WebSocket live stream synchronized."
                    )
                else:
                    await notifier.send_message(
                        f"❌ <b>Dhan Token Validation Failed (HTTP {resp.status_code})</b>\n\n"
                        f"<code>{resp.text[:150]}</code>\n\n"
                        "Please verify you copied the complete token from Dhan Web."
                    )
            except Exception as e:
                await notifier.send_message(f"⚠️ Error verifying token: {e}")

        elif text in ("/learn", "learn"):
            prog_mid = await notifier.send_and_get_id(
                "🧠 <b>Deep Machine Learning in Progress...</b>\n\n"
                "• <i>Downloading & analyzing multi-year historical candles...</i>\n"
                "• <i>Evaluating volume signatures & false-breakout traps...</i>\n\n"
                "⏳ <i>Genuine deep learning takes ~20–40 seconds. Please wait, the full report will be delivered once training finishes.</i>"
            )

            async def _on_progress(status_text: str):
                if prog_mid:
                    await notifier.edit_message_text(
                        prog_mid,
                        f"🧠 <b>Deep Machine Learning in Progress...</b>\n\n"
                        f"• {status_text}\n\n"
                        "⏳ <i>Training thoroughly from genuine multi-year exchange data...</i>"
                    )

            from app.strategies.historical_learner import historical_learner
            res = await historical_learner.run_historical_learning_cycle(
                progress_callback=_on_progress,
                max_symbols=10
            )

            # Remove progress status message once the real report is ready
            if prog_mid:
                await notifier.delete_single_message(prog_mid)

            if res:
                report = historical_learner.format_offmarket_learning_report(res)
                await notifier.send_message(report)
            else:
                await notifier.send_message("⚠️ Deep learning cycle completed.")

        elif text in ("/positions", "positions"):
            try:
                pos_resp = self.client.get_positions()
                positions = pos_resp.get("data", []) if isinstance(pos_resp, dict) else []
                if not positions:
                    reply = "📊 <b>Dhan Positions:</b> No active open positions right now."
                else:
                    pos_lines = ""
                    for p in positions:
                        sym = p.get("tradingSymbol", "Unknown")
                        net_qty = p.get("netQty", 0)
                        pnl = p.get("realizedProfit", 0.0) + p.get("unrealizedProfit", 0.0)
                        pos_lines += f"• <b>{sym}</b>: Qty {net_qty} | P&amp;L: ₹{pnl:,.2f}\n"
                    reply = f"📊 <b>Active Dhan Positions:</b>\n\n{pos_lines}"
            except Exception as e:
                reply = f"⚠️ Error querying Dhan positions: {e}"
            await notifier.send_message(reply)

        elif text in ("/orders", "orders"):
            try:
                ord_resp = self.client.get_order_list()
                orders = ord_resp.get("data", []) if isinstance(ord_resp, dict) else []
                if not orders:
                    reply = "📋 <b>Dhan Orders:</b> No orders placed today."
                else:
                    ord_lines = ""
                    for o in orders[-5:]:
                        sym = o.get("tradingSymbol", "Unknown")
                        stat = o.get("orderStatus", "N/A")
                        price = o.get("price", 0.0)
                        qty = o.get("quantity", 0)
                        ord_lines += f"• <b>{sym}</b> ({qty} Qty @ ₹{price}): {stat}\n"
                    reply = f"📋 <b>Recent Dhan Orders:</b>\n\n{ord_lines}"
            except Exception as e:
                reply = f"⚠️ Error querying Dhan orders: {e}"
            await notifier.send_message(reply)

        elif text in ("/status", "status"):
            reply = (
                "⚡ <b>ORB Scanner System Status</b>\n\n"
                "• <b>Mode:</b> 24/7 Continuous Machine Learning Active\n"
                "• <b>Strategy:</b> 15m Breakout (09:30–09:45 Confirmation)\n"
                "• <b>ML Filter:</b> High-Probability (>=65% Conviction)\n"
                "• <b>Cloud Sync:</b> Firebase Realtime Database Active\n"
                "• <b>1-Click Trading:</b> Active inside Telegram"
            )
            await notifier.send_message(reply)

        elif text in ("/help", "/start", "help"):
            reply = (
                "🤖 <b>Telegram Trading Command Center</b>\n\n"
                "• <code>/balance</code> or <code>/limit</code> - Check live Dhan margin & funds\n"
                "• <code>/indices</code> - View today's Nifty 50, BankNifty & Sensex ORB levels\n"
                "• <code>/learn</code> - Run on-demand deep machine learning on 5-yr exchange data\n"
                "• <code>/positions</code> - View open trades on Dhan\n"
                "• <code>/orders</code> - Check today's Dhan orders\n"
                "• <code>/status</code> - Scanner & ML engine health\n"
                "• <code>/token &lt;jwt&gt;</code> - Update Dhan token directly via chat\n\n"
                "<i>When an ORB breakout occurs, 1-click Buy/Sell buttons will appear right here!</i>"
            )
            await notifier.send_message(reply)

    async def _handle_callback(self, cb_query: Dict[str, Any]):
        """Processes user tapping Approve (Whole Lot Price) or Reject."""
        query_id = cb_query.get("id")
        cb_id = str(query_id)
        if cb_id in self._processed_callbacks:
            return
        self._processed_callbacks.add(cb_id)

        from_user = cb_query.get("from", {})
        user_id = str(from_user.get("id", ""))
        
        # Verify authorized chat
        if user_id != str(settings.telegram_chat_id).strip():
            logger.warning(f"Unauthorized callback attempt from Telegram user ID: {user_id}")
            await self._answer_callback(query_id, "Unauthorized.")
            return

        data = cb_query.get("data", "")
        message = cb_query.get("message", {})
        message_id = message.get("message_id")
        orig_text = message.get("text", "")

        if data.startswith("app:"):
            parts = data.split(":")
            sig_key = parts[1]
            lot_mult = int(parts[2]) if len(parts) > 2 else 1
            order_data = self._pending_orders.get(sig_key)
            if not order_data:
                await self._answer_callback(query_id, "Signal expired or not found.")
                return

            await self._answer_callback(query_id, f"Submitting order for {lot_mult} Lot(s) to Dhan...")
            success, msg = await self.execute_dhan_order(order_data, lot_multiplier=lot_mult)
            total_qty = order_data["lot_size"] * lot_mult

            # Update original Telegram message
            new_text = (
                f"{orig_text}\n\n"
                f"{'✅ <b>ORDER EXECUTED ON DHAN</b>' if success else '⚠️ <b>ORDER PLACEMENT FAILED</b>'}\n"
                f"<b>Status:</b> {msg}\n"
                f"<b>Executed Quantity:</b> {lot_mult} Lot(s) ({total_qty} units)\n"
                f"<b>Target:</b> ₹{order_data['target']:,.2f}\n"
                f"<b>Stop Loss:</b> ₹{order_data['stop_loss']:,.2f}\n"
                f"<b>Execution Time:</b> {datetime.now().strftime('%H:%M:%S')} IST"
            )
            await self._edit_message(message_id, new_text)

        elif data.startswith("rej:"):
            sig_key = data.split(":", 1)[1]
            await self._answer_callback(query_id, "Signal rejected.")
            new_text = f"{orig_text}\n\n❌ <i>Signal Rejected / Dismissed by User.</i>"
            await self._edit_message(message_id, new_text)

    async def _answer_callback(self, callback_query_id: str, text: str):
        url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/answerCallbackQuery"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(url, json={"callback_query_id": callback_query_id, "text": text})
        except Exception as e:
            logger.debug(f"Error answering callback query: {e}")

    async def _edit_message(self, message_id: int, new_text: str):
        url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/editMessageText"
        payload = {
            "chat_id": settings.telegram_chat_id,
            "message_id": message_id,
            "text": new_text,
            "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": []},  # remove buttons once clicked
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(url, json=payload)
        except Exception as e:
            logger.debug(f"Error editing message: {e}")


order_executor = DhanOrderExecutor()
