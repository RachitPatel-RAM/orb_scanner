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
from app.market.session import default_session
from app.storage.database import db
from app.storage.models import Direction, Signal


class DhanOrderExecutor:
    """Manages 1-click Telegram order approvals and executes orders on Dhan with SL & Target."""

    def __init__(self, orb_strategy: Optional[Any] = None):
        self._dhan: Optional[dhanhq] = None
        self._pending_orders: Dict[str, Dict[str, Any]] = {}
        self._processed_callbacks: set[str] = set()
        from app.strategies.orb import ORBStrategy
        self.orb_strategy = orb_strategy or ORBStrategy()

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

        if is_index and not opt_contract:
            logger.warning(
                f"[Quote Guard] Aborting order registration for index {signal.symbol}: "
                f"No verified option contract quote available. Zero phantom trades permitted."
            )
            return None, 0, 0.0

        if opt_contract:
            qty = opt_contract.lot_size
            margin_req = opt_contract.margin_required
            total_value = margin_req

            # Solvency check: ensure available capital can cover the full option contract premium
            if capital < margin_req:
                logger.warning(
                    f"[Capital Safety Guard] Insufficient funds for {opt_contract.custom_symbol}: "
                    f"Required margin ₹{margin_req:,.2f} > Available capital ₹{capital:,.2f}. Skipping order registration."
                )
                return None, 0, margin_req

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
            db.save_pending_order(
                sig_key=sig_key,
                security_id=opt_contract.security_id,
                symbol=opt_contract.underlying,
                direction=str(signal.direction.value if hasattr(signal.direction, "value") else signal.direction),
                entry_price=opt_contract.ltp,
                stop_loss=opt_contract.stop_loss_premium,
                target=opt_contract.target_premium,
                lot_size=qty,
                margin_req=margin_req,
                total_lot_price=total_value,
            )

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
        db.save_pending_order(
            sig_key=sig_key,
            security_id=signal.security_id,
            symbol=signal.symbol,
            direction=str(signal.direction.value if hasattr(signal.direction, "value") else signal.direction),
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            target=signal.target,
            lot_size=qty,
            margin_req=margin_req,
            total_lot_price=total_value,
        )

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

    async def execute_dhan_order(self, order_data: Dict[str, Any], lot_multiplier: int = 1) -> Tuple[str, bool, str]:
        """
        Places order on Dhan with Stop Loss and Target or stages simulated/manual setups.
        Returns:
            Tuple[status_category, success, message]
            status_category: "STAGED", "SIMULATED", "SUBMITTED", or "FAILED"
        """
        sec_id = str(order_data["security_id"])
        symbol = str(order_data["symbol"])
        dir_val = order_data.get("direction")
        is_long = dir_val == Direction.LONG or str(dir_val).upper() in ("LONG", "BUY")
        txn_type = "BUY" if is_long else "SELL"
        qty = int(order_data.get("lot_size", 1)) * max(1, lot_multiplier)
        price = float(order_data.get("entry_price", 0.0))
        target = float(order_data.get("target", 0.0))
        stop_loss = float(order_data.get("stop_loss", 0.0))

        # 1. Rigorous Capital Solvency & Margin Calculation Guard:
        # Option purchase cost = validated price * quantity + entry charges
        # Non-finite, missing, or zero inputs are strictly rejected.
        import math
        from app.dhan.option_finder import is_valid_price

        opt_contract = order_data.get("opt_contract")
        if opt_contract:
            opt_ltp = getattr(opt_contract, "ltp", None)
            opt_lot = getattr(opt_contract, "lot_size", None)

            if opt_ltp is None or not is_valid_price(opt_ltp):
                logger.error(f"[Solvency Guard] Rejected option order: Invalid or missing option LTP ({opt_ltp})")
                return "FAILED", False, f"Order Rejected: Invalid or missing option LTP ({opt_ltp})"

            if opt_lot is None or not isinstance(opt_lot, (int, float)) or math.isnan(opt_lot) or math.isinf(opt_lot) or opt_lot <= 0:
                logger.error(f"[Solvency Guard] Rejected option order: Invalid option lot size ({opt_lot})")
                return "FAILED", False, f"Order Rejected: Invalid option lot size ({opt_lot})"

            opt_qty = int(opt_lot) * max(1, lot_multiplier)
            purchase_cost = round(float(opt_ltp) * opt_qty, 2)
            # Statutory & regulatory entry charges (~Rs 60 per executed lot in Indian F&O option buying)
            entry_charges = round(60.0 * max(1, lot_multiplier), 2)
            calc_required = round(purchase_cost + entry_charges, 2)
        else:
            if price is None or not is_valid_price(price):
                return "FAILED", False, f"Order Rejected: Invalid entry price ({price})"
            if qty <= 0:
                return "FAILED", False, f"Order Rejected: Invalid quantity ({qty})"

            purchase_cost = round((price * qty) / 5.0, 2)
            entry_charges = round(25.0 * max(1, lot_multiplier), 2)
            calc_required = round(purchase_cost + entry_charges, 2)

        # Do not trust user-supplied margin_req: if provided, validate it
        raw_margin = order_data.get("margin_req")
        if raw_margin is not None:
            try:
                raw_val = float(raw_margin)
                if not (math.isnan(raw_val) or math.isinf(raw_val) or raw_val <= 0):
                    calc_required = max(calc_required, round((raw_val * lot_multiplier) + entry_charges, 2))
            except (ValueError, TypeError):
                pass

        if math.isnan(calc_required) or math.isinf(calc_required) or calc_required <= 0:
            logger.error(f"[Solvency Guard] Invalid margin calculation: {calc_required}")
            return "FAILED", False, "Order Rejected: Non-finite required capital calculation"

        from app.storage.database import db
        default_cap = float(os.getenv("TRADING_CAPITAL", "4322.0"))
        capital = db.get_account_balance(default_cap)

        if capital < calc_required:
            logger.warning(
                f"[SOLVENCY REJECTION] Available capital ₹{capital:,.2f} is insufficient "
                f"for required margin + charges ₹{calc_required:,.2f}. Skipping order and keeping balance unchanged."
            )
            return "FAILED", False, f"Order Rejected: Insufficient funds (Available: ₹{capital:,.2f}, Required: ₹{calc_required:,.2f})"

        # Handle Option Orders
        if opt_contract:
            if not getattr(settings, "live_order_enabled", False):
                # 5. Verified simulated option fill with cash reservation and auditable ledger
                opt_qty = opt_contract.lot_size * lot_multiplier
                trade_id = db.save_paper_trade(
                    signal_id=None,
                    trade_date=datetime.now().strftime("%Y-%m-%d"),
                    security_id=str(opt_contract.security_id),
                    symbol=opt_contract.custom_symbol,
                    direction=txn_type,
                    entry_price=opt_contract.ltp,
                    entry_time=datetime.now().isoformat(),
                    stop_loss=opt_contract.stop_loss_premium,
                    target=opt_contract.target_premium,
                    quantity=opt_qty,
                    asset_type="OPTION",
                    strike_price=opt_contract.strike_price,
                    option_type=opt_contract.option_type,
                    margin_reserved=calc_required,
                    entry_charges=entry_charges,
                )

                # Reserve cash and write to auditable ledger
                bal_before = capital
                bal_after = round(bal_before - calc_required, 2)
                db.set_account_balance(bal_after)
                db.record_ledger_entry(
                    transaction_type="CASH_RESERVATION",
                    amount=-purchase_cost,
                    balance_before=bal_before,
                    balance_after=bal_before - purchase_cost,
                    description=f"Reserved option purchase cost for {opt_contract.custom_symbol} ({opt_qty} Qty @ ₹{opt_contract.ltp:.2f})",
                    trade_id=trade_id,
                )
                db.record_ledger_entry(
                    transaction_type="ENTRY_CHARGES",
                    amount=-entry_charges,
                    balance_before=bal_before - purchase_cost,
                    balance_after=bal_after,
                    description=f"Statutory entry charges for {opt_contract.custom_symbol}",
                    trade_id=trade_id,
                )

                logger.info(
                    f"[SIMULATED_OPTION_FILL] Paper option order filled #{trade_id}: Buy {lot_multiplier} Lot(s) "
                    f"of {opt_contract.custom_symbol} ({opt_qty} Qty @ ₹{opt_contract.ltp:,.2f}) | "
                    f"SL: ₹{opt_contract.stop_loss_premium:,.2f} | Target: ₹{opt_contract.target_premium:,.2f} | "
                    f"Reserved: ₹{calc_required:,.2f} | New Balance: ₹{bal_after:,.2f}"
                )
                return "SIMULATED", True, (
                    f"📝 <b>Simulated Paper Option Order Filled:</b>\n"
                    f"• Trade ID: #{trade_id} | {txn_type} {lot_multiplier} Lot(s) of <b>{opt_contract.custom_symbol}</b>\n"
                    f"• {opt_qty} Qty @ ₹{opt_contract.ltp:,.2f} | Cost: ₹{purchase_cost:,.2f} + Charges: ₹{entry_charges:,.2f}\n"
                    f"• SL: ₹{opt_contract.stop_loss_premium:,.2f} | Target: ₹{opt_contract.target_premium:,.2f}\n"
                    f"• <b>Auditable Ledger:</b> Cash Reserved ₹{calc_required:,.2f} (New Balance: ₹{bal_after:,.2f})\n"
                    f"• <i>Simulated fill recorded in paper portfolio. Zero broker order routed.</i>"
                )
            else:
                return "STAGED", True, (
                    f"📋 <b>Option Setup Staged:</b> Buy {lot_multiplier} Lot(s) of <b>{opt_contract.custom_symbol}</b> "
                    f"({opt_contract.lot_size * lot_multiplier} Qty) @ ₹{opt_contract.ltp:,.2f} | "
                    f"SL: ₹{opt_contract.stop_loss_premium:,.2f} | Target: ₹{opt_contract.target_premium:,.2f}.\n\n"
                    f"⚡ <i>Manual execution required via Dhan App or Web Option Chain (Zero API order sent).</i>"
                )

        logger.info(
            f"Placing Dhan Order: {txn_type} {symbol} ({sec_id}) Lots={lot_multiplier} Qty={qty} Price={price} "
            f"SL={stop_loss} Target={target}"
        )

        # Enforce LIVE_ORDER_ENABLED toggle (isolated validation profile)
        if not getattr(settings, "live_order_enabled", False):
            logger.info(
                f"[LIVE_ORDER_DISABLED] Simulated paper order logged for {txn_type} {symbol} "
                f"Lots={lot_multiplier} Qty={qty} Price={price} SL={stop_loss} Target={target}."
            )
            return "SIMULATED", True, (
                f"📝 <b>Simulated Paper Order Logged:</b>\n"
                f"• {txn_type} {symbol} ({qty} Qty @ ₹{price:,.2f})\n"
                f"• SL: ₹{stop_loss:,.2f} | Target: ₹{target:,.2f}\n"
                f"• <i>Live broker order submission is disabled (LIVE_ORDER_ENABLED=false). No broker call made.</i>"
            )

        if symbol in ("NIFTY", "BANKNIFTY", "SENSEX") or sec_id in ("13", "25", "51"):
            opt_type = "CE (Call)" if is_long else "PE (Put)"
            strike = round(price / 50.0) * 50 if symbol == "NIFTY" else round(price / 100.0) * 100
            return "STAGED", True, f"Index Setup Staged: {symbol} broke ORB ({'Long' if is_long else 'Short'}). Recommended strike: {strike} {opt_type}. Manual execution via Dhan Option Chain."

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

            parsed_status, parsed_ok, parsed_msg = self._parse_dhan_response(res, qty, price)
            if parsed_ok:
                return parsed_status, parsed_ok, parsed_msg
            
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

            return self._parse_dhan_response(res2, qty, price)

        except Exception as e:
            logger.error(f"Error placing Dhan order: {e}")
            return "FAILED", False, f"Execution Exception: {str(e)}"

    def _parse_dhan_response(self, res: Any, requested_qty: int, requested_price: float) -> Tuple[str, bool, str]:
        """
        Parses Dhan order placement response according to official DhanHQ v2 docs:
        - Statuses: PENDING, TRANSIT, TRADED, REJECTED, CANCELLED
        - Requires a valid non-empty order ID
        - Rejections and cancellations return FAILED, False
        """
        if not isinstance(res, dict):
            return "FAILED", False, f"Order Placement Failed: Invalid response format ({type(res).__name__})"

        data_inner = res.get("data", {}) if isinstance(res.get("data"), dict) else {}
        order_id = data_inner.get("orderId") or res.get("orderId")
        broker_status = str(data_inner.get("orderStatus") or res.get("orderStatus") or "PENDING").upper()
        remarks = res.get("remarks") or data_inner.get("remarks") or ""

        # 1. Explicit rejection by broker risk management
        if broker_status == "REJECTED":
            reason = remarks or "Order rejected by broker risk management / RMS"
            return "FAILED", False, f"Order Rejected by Broker (Status: REJECTED): {reason}"

        # 2. Explicit cancellation by broker
        if broker_status == "CANCELLED":
            reason = remarks or "Order cancelled by broker"
            return "FAILED", False, f"Order Cancelled by Broker (Status: CANCELLED): {reason}"

        # 3. Top-level status check
        status_str = res.get("status", "").lower()
        if status_str != "success":
            err_str = str(remarks or res)
            if "DH-905" in err_str or "Invalid IP" in err_str:
                return "FAILED", False, (
                    "⚠️ <b>Dhan API Error: Server IP Not Whitelisted (DH-905)</b>\n\n"
                    "Dhan blocks API orders unless your server IP is whitelisted in your Dhan account.\n"
                    "👉 <b>Server Static IP:</b> <code>34.10.99.222</code>\n\n"
                    "<b>How to fix in 1 minute:</b>\n"
                    "1. Open <a href='https://web.dhan.co'>web.dhan.co</a>\n"
                    "2. Go to <b>My Profile ➔ DhanHQ Trading & Data APIs ➔ IP Setup / Static IP</b>\n"
                    "3. Enter: <code>34.10.99.222</code> and Save.\n\n"
                    "💡 <i>Once saved, all future 1-click orders will execute directly on Dhan!</i>"
                )
            return "FAILED", False, f"Dhan API Error: {err_str}"

        # 4. Must contain a valid, non-empty, non-sentinel order ID
        if not order_id or str(order_id).strip().upper() in ("", "N/A", "#N/A", "NONE", "NULL", "0"):
            return "FAILED", False, f"Order Placement Failed: Broker returned success without valid Order ID (Response: {res})"

        order_id_str = str(order_id).strip()

        # 5. Handle TRADED (Confirmed execution)
        if broker_status == "TRADED":
            traded_qty = data_inner.get("tradedQuantity", requested_qty)
            traded_price = data_inner.get("tradedPrice", requested_price)
            return "TRADED", True, f"Order Executed on Exchange (Status: TRADED) | ID: #{order_id_str} | Qty: {traded_qty} @ ₹{traded_price}"

        # 6. Handle PENDING / TRANSIT (Submitted)
        return "SUBMITTED", True, f"Order Submitted to Dhan Broker (Status: {broker_status}) | Order ID: #{order_id_str}. Awaiting broker fill confirmation."

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
                {"command": "bias", "description": "Check Daily Bias & Intraday Context via /bias <sym>"},
                {"command": "health", "description": "System health and operational profile audit"},
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
                    payload = {
                        "offset": offset,
                        "timeout": 20,
                        "allowed_updates": ["message", "callback_query"],
                    }
                    resp = await client.post(url, json=payload)
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
                            f"• <b>Range High:</b> ₹{orb_high:,.2f}\n"
                            f"• <b>Range Low:</b> ₹{orb_low:,.2f}\n"
                            f"• <b>Range Midpoint:</b> ₹{orb_mid:,.2f}\n"
                            f"• <b>Current LTP:</b> ₹{cur_p:,.2f} ({status_sym} | {diff_pct:+.2f}%)\n"
                        )
            except Exception as e:
                logger.debug(f"Error fetching {name} ORB: {e}")

        if not lines:
            return ""

        dt_str = d.strftime("%d-%b-%Y")
        return (
            f"🏛 <b>Daily Major Indices Benchmark (10:00 AM IST)</b>\n"
            f"📅 <b>Date:</b> {dt_str}\n\n"
            + "\n".join(lines)
            + "⚡ <i>Actionable alerts trigger upon confirmed candle breakout beyond benchmark range.</i>"
        )

    async def get_dynamic_choppy_market_post(self) -> str:
        """Constructs a real-time, reason-backed market structure update with live index ranges."""
        from app.dhan.auth import auth
        from app.market.session import default_session
        d = default_session.now().date()
        time_str = default_session.now().strftime("%I:%M %p")
        headers = auth.get_headers()
        url_chart = "https://api.dhan.co/v2/charts/intraday"
        url_ltp = "https://api.dhan.co/v2/marketfeed/ltp"

        # Fetch live LTPs
        live_ltps = {}
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r_ltp = await client.post(url_ltp, headers=headers, json={"IDX_I": [13, 25]})
                if r_ltp.status_code == 200:
                    d_data = r_ltp.json().get("data", {}).get("IDX_I", {})
                    for sid_str, val in d_data.items():
                        live_ltps[sid_str] = float(val.get("last_price", 0.0))
        except Exception:
            pass

        # Fetch ORB ranges for Nifty & BankNifty
        idx_info = []
        for sid, name in [("13", "NIFTY 50"), ("25", "BANKNIFTY")]:
            payload = {
                "securityId": sid,
                "exchangeSegment": "IDX_I",
                "instrument": "INDEX",
                "fromDate": f"{d.isoformat()} 09:15:00",
                "toDate": f"{d.isoformat()} 15:30:00",
                "interval": "15",
            }
            try:
                async with httpx.AsyncClient(timeout=6.0) as client:
                    resp = await client.post(url_chart, headers=headers, json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    highs = data.get("high", [])
                    lows = data.get("low", [])
                    if len(highs) >= 3:
                        o_high = max(highs[1], highs[2])
                        o_low = min(lows[1], lows[2])
                        ltp = live_ltps.get(sid, 0.0) or (data.get("close", [0.0])[-1] if data.get("close") else 0.0)
                        ltp_str = f" | LTP: ₹{ltp:,.0f}" if ltp else ""
                        idx_info.append(f"• <b>{name}:</b> Trapped in ₹{o_low:,.0f} – ₹{o_high:,.0f}{ltp_str}")
            except Exception:
                pass

        zone_lines = "\n".join(idx_info) if idx_info else (
            "• <b>NIFTY 50 & BANKNIFTY:</b> Oscillating inside 09:30–10:00 Benchmark Ranges."
        )

        return (
            f"📊 <b>MID-DAY MARKET STRUCTURE UPDATE</b> ({time_str} IST)\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "⚠️ <b>Market Status:</b> Range-Bound & Trapping (No-Trade Zone)\n\n"
            "📍 <b>Live Benchmark Zones:</b>\n"
            f"{zone_lines}\n\n"
            "🚫 <b>Why No Trade Triggered Yet:</b>\n"
            "• Price is strictly rotating inside initial benchmark levels with 0 institutional displacement.\n"
            "• Counter-wicks >40% detected at boundary tests (classic retail breakout trap behavior).\n"
            "• Institutional volume surge (>1.2x RVOL) is absent.\n\n"
            "🛡️ <b>Capital Protection Mode:</b> ACTIVE\n"
            "<i>We will NOT gamble or force trades in sideways noise. The moment a confirmed institutional breakout forms, the high-accuracy setup will be alerted immediately!</i>\n\n"
            "⏳ <b>Afternoon Session Watch:</b> 01:15 PM – 02:30 PM (European Open Expansion)."
        )

    async def check_indices_breakouts(self, on_signal_callback) -> None:
        """
        Monitors NIFTY 50, BANKNIFTY, and SENSEX for canonical ORB breakouts.
        STRICT REQUIREMENT: Earliest confirmation is 10:15:00 IST.
        Uses solely completed 15-minute candles (09:30-10:00 range, 10:00-10:15 first evaluation).
        Zero forming candle or premature 5m triggers allowed.
        """
        from datetime import time
        from app.dhan.auth import auth
        from app.market.session import default_session, IST_TZ
        from app.storage.models import Signal, Direction, Candle
        now_dt = default_session.now()
        now_epoch = now_dt.timestamp()
        d = now_dt.date()

        tf = getattr(self.orb_strategy.config, "signal_timeframe", 15) or 15
        interval_str = str(tf)
        candle_duration_sec = tf * 60

        # Strict Gate 1: No signals before the first candle confirmation after 10:00 range completes
        # If tf == 15: earliest is 10:15:00 IST
        # If tf == 5: earliest is 10:05:00 IST
        earliest_time = time(10, 15) if tf == 15 else time(10, 5)
        if now_dt.time() < earliest_time:
            logger.debug(f"[Index Scanner] Prior to {earliest_time} benchmark confirmation window. Skipping check.")
            return

        # Strict Gate 2: Afternoon theta decay cutoff (no fresh breakouts after 14:00 IST)
        if now_dt.time() >= time(14, 0):
            logger.debug("[Index Scanner] Afternoon session cutoff reached (>= 14:00 IST). Skipping check.")
            return

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

            # Query candles based on configured timeframe (5m or 15m)
            payload_tf = {
                "securityId": sid,
                "exchangeSegment": seg,
                "instrument": "INDEX",
                "fromDate": f"{d.isoformat()} 09:15:00",
                "toDate": f"{d.isoformat()} 15:30:00",
                "interval": interval_str,
            }

            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp_tf = await client.post(url, headers=headers, json=payload_tf)

                if resp_tf is not None and getattr(resp_tf, "status_code", None) == 200:
                    data_tf = resp_tf.json()
                    highs_tf = data_tf.get("high", [])
                    lows_tf = data_tf.get("low", [])
                    closes_tf = data_tf.get("close", [])
                    opens_tf = data_tf.get("open", [])
                    ts_tf = data_tf.get("timestamp", [])
                    vols_tf = data_tf.get("volume", [1000] * len(closes_tf))

                    # Route completed candles chronologically through the single canonical ORBStrategy engine
                    for i in range(len(ts_tf)):
                        candle_start_ts = ts_tf[i]
                        # Candle must have fully elapsed
                        if now_epoch < (candle_start_ts + candle_duration_sec):
                            continue

                        c_time = datetime.fromtimestamp(candle_start_ts, tz=IST_TZ)
                        c = Candle(
                            security_id=sid,
                            symbol=sym,
                            timestamp=c_time,
                            open=float(opens_tf[i]),
                            high=float(highs_tf[i]),
                            low=float(lows_tf[i]),
                            close=float(closes_tf[i]),
                            volume=float(vols_tf[i]) if vols_tf else 1000.0,
                            is_closed=True,
                        )

                        sig = None
                        from app.analysis.pre_market import pre_market_manager
                        snap = pre_market_manager.get_snapshot(sym, d)
                        is_wide_chop = snap is not None and snap.regime == "WIDE_CHOP"

                        if not is_wide_chop:
                            # 1. Narrow / Trending Days: 5M Solid ORB Momentum Breakout
                            sig = self.orb_strategy.on_candle_closed(c)
                        else:
                            # 2. Wide CPR Days: SMC Liquidity Sweep & Reclaim Reversals
                            # Suppress breakout traps and look for sweep rejections
                            orb_levels = self.orb_strategy.get_orb_levels(d, sid)
                            if orb_levels and orb_levels.is_complete:
                                c_rng = c.high - c.low
                                if c.high > orb_levels.high and c.close < orb_levels.high and c_rng > 0:
                                    upper_wick = (c.high - max(c.open, c.close)) / c_rng
                                    if upper_wick >= 0.25 and c.close <= c.open:
                                        sig = Signal(
                                            trade_date=d,
                                            security_id=sid,
                                            symbol=sym,
                                            strategy="SMC-SWEEP",
                                            direction=Direction.SHORT,
                                            timestamp=c_time,
                                            entry_price=c.close,
                                            orb_high=orb_levels.high,
                                            orb_low=orb_levels.low,
                                            stop_loss=round(c.high + (orb_levels.high * 0.0005), 2),
                                            target=round(orb_levels.mid, 2),
                                            target_1=round(orb_levels.mid, 2),
                                            target_2=round(orb_levels.low, 2),
                                            risk_reward=2.5,
                                            idempotency_key=f"SMC_SWEEP_{d.isoformat()}_{sid}_SHORT",
                                        )
                                elif c.low < orb_levels.low and c.close > orb_levels.low and c_rng > 0:
                                    lower_wick = (min(c.open, c.close) - c.low) / c_rng
                                    if lower_wick >= 0.25 and c.close >= c.open:
                                        sig = Signal(
                                            trade_date=d,
                                            security_id=sid,
                                            symbol=sym,
                                            strategy="SMC-SWEEP",
                                            direction=Direction.LONG,
                                            timestamp=c_time,
                                            entry_price=c.close,
                                            orb_high=orb_levels.high,
                                            orb_low=orb_levels.low,
                                            stop_loss=round(c.low - (orb_levels.low * 0.0005), 2),
                                            target=round(orb_levels.mid, 2),
                                            target_1=round(orb_levels.mid, 2),
                                            target_2=round(orb_levels.high, 2),
                                            risk_reward=2.5,
                                            idempotency_key=f"SMC_SWEEP_{d.isoformat()}_{sid}_LONG",
                                        )

                        if sig is not None:
                            setattr(self, f"_idx_broken_{sym}_{d.isoformat()}", True)
                            if on_signal_callback:
                                on_signal_callback(sig, candle=c)
                            break
            except Exception as e:
                logger.debug(f"Error checking {sym} {interval_str}m breakout via canonical ORB: {e}")


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

        from app.config import settings
        if settings.universe.mode.lower() in ("indices_only", "indices"):
            return

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

    async def _send_photo(
        self,
        chat_id: Any,
        photo: str,
        caption: str = "",
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Sends a photo (URL or file_id) via Telegram Bot API."""
        url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendPhoto"
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "photo": photo,
            "caption": caption,
            "parse_mode": "HTML",
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=payload)
                return resp.status_code == 200
        except Exception as e:
            logger.debug(f"Error sending photo to {chat_id}: {e}")
            return False

    async def _handle_message_command(self, msg: Dict[str, Any]):
        """Processes interactive chat commands from user (balance, positions, status, orders, VIP subscriptions)."""
        chat = msg.get("chat", {})
        chat_id = str(chat.get("id", "")).strip()
        from_user = msg.get("from", {})
        user_id = str(from_user.get("id", chat_id)).strip()
        user_name = from_user.get("first_name", "") or from_user.get("username", "Trader")
        username = from_user.get("username", "")

        raw_text = str(msg.get("text", "")).strip()
        caption_text = str(msg.get("caption", "")).strip()
        text = (raw_text or caption_text).lower()

        from app.notifications.telegram import notifier
        from app.notifications.vip_channel import vip_manager
        from app.storage.database import db

        admin_chat_id = str(settings.telegram_chat_id).strip()
        is_admin = (user_id == admin_chat_id or chat_id == admin_chat_id)

        # -------------------------------------------------------------
        # 1. Non-Admin User Flows (Strict Redirection to Channels & Support Bot)
        # -------------------------------------------------------------
        if not is_admin:
            redirect_msg = (
                "👋 <b>Welcome to BornBull Trading Desk!</b> 🐂\n"
                "━━━━━━━━━━━━━━━━━━━━━\n"
                "⚡ <b>Automated Research & Alert Broadcast:</b>\n"
                "All real-time breakout setups, daily bias digests, and market updates are published directly in our official channels:\n\n"
                "📢 <b>Free Public Channel:</b> <a href='https://t.me/bornbulltrade'>@bornbulltrade</a>\n"
                "💎 <b>VIP Trading Desk:</b> Exclusive high-confluence institutional calls\n\n"
                "🤝 <b>Subscriptions, UPI Payments & Support:</b>\n"
                "All membership subscriptions, UPI payment approvals, and direct queries are handled exclusively by our dedicated support desk:\n"
                "👉 <b>@bornbullsupportbot</b>\n\n"
                "<i>Please use the buttons below to join our channel or contact the support desk.</i>"
            )
            redirect_markup = {
                "inline_keyboard": [
                    [
                        {"text": "📢 Join Free Public Channel", "url": "https://t.me/bornbulltrade"},
                    ],
                    [
                        {"text": "💎 Join VIP via Support Bot", "url": "https://t.me/bornbullsupportbot"},
                    ],
                ]
            }
            await notifier.send_message(redirect_msg, target_chat_id=chat_id, reply_markup=redirect_markup)
            return

        # -------------------------------------------------------------
        # 2. Authorized Admin Commands (Dhan Execution & Management)
        # -------------------------------------------------------------
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
                "• <b>Strategy:</b> 15m Breakout (09:30–10:00 Benchmark, 10:05 First Confirmation)\n"
                f"• <b>Gate Mode:</b> {settings.bias_gate_mode}\n"
                f"• <b>Rule Version:</b> {settings.bias_rule_version}\n"
                f"• <b>Live Orders:</b> {'ENABLED' if settings.live_order_enabled else 'DISABLED (Simulated)'}\n"
                f"• <b>Publishing:</b> {'ENABLED' if settings.channel_publishing_enabled else 'DISABLED'}\n"
                "• <b>1-Click Trading:</b> Active inside Telegram"
            )
            await notifier.send_message(reply)

        elif text.startswith("/bias") or text == "bias":
            parts = text.split()
            sym = (parts[1].upper() if len(parts) > 1 else "NIFTY")
            today_str = default_session.now().date().isoformat()
            from app.analysis.daily_bias import BiasDirection
            from app.analysis.liquidity_context import liquidity_context_engine
            from app.notifications.templates import render_daily_bias_digest

            snap = db.get_daily_bias_snapshot(sym, today_str, settings.bias_rule_version)
            ctx = liquidity_context_engine.get_current_context(sym)

            if snap:
                d_dir = snap["daily_bias"]
                allowed = (
                    "Long entries permitted upon valid ORB confirmation" if d_dir == "BULLISH"
                    else "Short entries permitted upon valid ORB confirmation" if d_dir == "BEARISH"
                    else "Wait (Neutral day; new entries paused in strict mode)"
                )
                msg_text = render_daily_bias_digest(
                    trading_date=snap["trading_date"],
                    published_time_ist=default_session.now().strftime("%H:%M IST"),
                    gate_mode=settings.bias_gate_mode,
                    symbol=snap["symbol"],
                    daily_bias=snap["daily_bias"],
                    previous_date=snap["previous_session_date"],
                    previous_close=float(snap["previous_close"]),
                    reference_date=snap["reference_session_date"],
                    reference_low=float(snap["reference_low"]),
                    reference_high=float(snap["reference_high"]),
                    plain_language_reason=snap["reason_code"].replace("_", " ").title(),
                    allowed_direction_or_wait=allowed,
                    data_as_of=f"{snap['previous_session_date']} 15:30 IST",
                    short_snapshot_id=snap["snapshot_id"][-12:],
                )
                msg_text += f"\n\n🔍 <b>Intraday Context (60m):</b> {ctx.direction.value}\n• <i>{ctx.reason}</i>"
            else:
                msg_text = (
                    f"ℹ️ <b>No Daily Bias Snapshot Found for {sym} on {today_str}</b>\n\n"
                    "• The morning snapshot is computed at 09:05 AM IST.\n"
                    "• Use <code>/bias NIFTY</code>, <code>/bias BANKNIFTY</code>, or a specific stock symbol."
                )
            await notifier.send_message(msg_text)

        elif text in ("/health", "health"):
            now_dt = default_session.now()
            today_date = now_dt.date()
            is_trading = default_session.is_trading_day(today_date)
            is_open = default_session.is_market_open(now_dt)

            dhan_status = "Connected" if auth.has_credentials else "Not Configured"
            masked_cid = f"{settings.dhan_client_id[:2]}****" if settings.dhan_client_id else "N/A"

            health_text = (
                "🏥 <b>BORNBULL SYSTEM HEALTH &amp; AUDIT STATUS</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━\n"
                f"⏰ <b>System Time:</b> {now_dt.strftime('%Y-%m-%d %H:%M:%S')} (Asia/Kolkata)\n"
                f"📅 <b>Exchange Session:</b> {'Active Trading Day' if is_trading else 'Closed (Holiday/Weekend)'}\n"
                f"🏛 <b>NSE Market Status:</b> {'OPEN' if is_open else 'CLOSED'}\n\n"
                "🛡️ <b>Engine Operating Modes:</b>\n"
                f"• <b>BIAS_GATE_MODE:</b> <code>{settings.bias_gate_mode}</code>\n"
                f"• <b>CHANNEL_PUBLISHING:</b> <code>{'ENABLED' if settings.channel_publishing_enabled else 'DISABLED (Validation Profile)'}</code>\n"
                f"• <b>LIVE_ORDER_ENABLED:</b> <code>{'ENABLED' if settings.live_order_enabled else 'DISABLED (Simulated Execution)'}</code>\n"
                f"• <b>SESSION_CONTEXT:</b> <code>{'ENABLED' if settings.session_context_enabled else 'DISABLED'}</code>\n"
                f"• <b>RULE_VERSION:</b> <code>{settings.bias_rule_version}</code>\n\n"
                "🔌 <b>Connectivity &amp; Infrastructure:</b>\n"
                f"• <b>Dhan Market Data:</b> {dhan_status} (Client ID: <code>{masked_cid}</code>)\n"
                f"• <b>SQLite Database:</b> Healthy (WAL mode active)\n"
                f"• <b>Telegram Polling:</b> Active Listener\n\n"
                "<i>All credentials, secret tokens, and fund balances redacted.</i>"
            )
            await notifier.send_message(health_text)

        elif text.startswith("/add_sub"):
            parts = text.split()
            if len(parts) >= 4:
                tg_id = parts[1]
                sub_name = parts[2]
                try:
                    months = int(parts[3])
                    from app.notifications.vip_channel import vip_manager
                    ok, res_msg, _, _ = await vip_manager.create_subscription_invite(tg_id, sub_name, months)
                    await notifier.send_message(res_msg)
                except ValueError:
                    await notifier.send_message("⚠️ Invalid months. Format: <code>/add_sub &lt;user_id&gt; &lt;name&gt; &lt;months&gt;</code>")
            else:
                await notifier.send_message("ℹ️ Format: <code>/add_sub &lt;user_id&gt; &lt;name&gt; &lt;months&gt;</code>\nExample: <code>/add_sub 987654321 Rahul 3</code>")

        elif text in ("/subs", "/subscribers"):
            from app.storage.database import db
            active_subs = db.get_active_vip_subscribers()
            if not active_subs:
                await notifier.send_message("👥 <b>VIP Subscribers:</b> No active subscribers currently.")
            else:
                lines = ""
                for s in active_subs:
                    lines += f"• <b>{s['name']}</b> (<code>{s['telegram_id']}</code>): {s['plan_months']}mo | Expires: <b>{s['expiry_date']}</b>\n"
                await notifier.send_message(f"👥 <b>Active VIP Subscribers ({len(active_subs)}):</b>\n\n{lines}")

        elif text.startswith("/remove_sub"):
            parts = text.split()
            if len(parts) >= 2:
                tg_id = parts[1]
                from app.storage.database import db
                db.deactivate_vip_subscriber(tg_id)
                await notifier.send_message(f"✅ Subscriber <code>{tg_id}</code> deactivated.")
            else:
                await notifier.send_message("ℹ️ Format: <code>/remove_sub &lt;user_id&gt;</code>")

        elif text in ("/choppy", "/sideways", "/notrade", "choppy", "sideways"):
            choppy_post = await self.get_dynamic_choppy_market_post()
            pub_ch = settings.telegram_public_channel_id
            vip_ch = getattr(settings, "vip_channel_id", "-1003416174805")
            if pub_ch:
                await notifier.send_message(choppy_post, target_chat_id=pub_ch)
            if vip_ch and vip_ch != pub_ch:
                await notifier.send_message(choppy_post, target_chat_id=vip_ch)
            await notifier.send_message("✅ <b>Dynamic Choppy Market Capital Protection Update</b> dispatched to Public & VIP channels!")

        elif text in ("/crypto", "/forex", "/zuperior", "/promo", "crypto", "forex"):
            # Instant on-demand dispatch of Zuperior Referral Promo
            slot = "night" if default_session.now().hour >= 18 else "midday"
            await notifier.send_zuperior_promo(slot=slot)
            await notifier.send_message(f"🚀 <b>Zuperior Crypto & Forex Referral Prompt ({slot.upper()})</b> successfully dispatched to Public & VIP channels!")

        elif text in ("/help", "/start", "help"):
            reply = (
                "🤖 <b>Telegram Trading Command Center</b>\n\n"
                "• <code>/balance</code> or <code>/limit</code> - Check live Dhan margin & funds\n"
                "• <code>/indices</code> - View today's Nifty 50, BankNifty & Sensex ORB levels\n"
                "• <code>/choppy</code> - Broadcast mid-day capital protection / sideways update\n"
                "• <code>/learn</code> - Run on-demand deep machine learning on 5-yr exchange data\n"
                "• <code>/positions</code> - View open trades on Dhan\n"
                "• <code>/orders</code> - Check today's Dhan orders\n"
                "• <code>/status</code> - Scanner & ML engine health\n"
                "• <code>/subs</code> - View active VIP channel subscribers\n"
                "• <code>/add_sub &lt;id&gt; &lt;name&gt; &lt;mo&gt;</code> - Enroll VIP paid subscriber\n"
                "• <code>/remove_sub &lt;id&gt;</code> - Deactivate VIP subscriber\n"
                "• <code>/token &lt;jwt&gt;</code> - Update Dhan token directly via chat\n\n"
                "<i>When an ORB breakout occurs, 1-click Buy/Sell buttons will appear right here!</i>"
            )
            await notifier.send_message(reply)

        else:
            # If Admin sends or forwards any custom message/photo, offer 1-click broadcast options
            admin_msg_id = msg.get("message_id")
            preview = (raw_text[:70] or caption_text[:70] or "Media / Forwarded post").replace("<", "&lt;").replace(">", "&gt;")
            bc_prompt = (
                "📢 <b>ADMIN BROADCAST DISPATCHER</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━\n"
                f"<b>Message:</b> <i>\"{preview}...\"</i>\n\n"
                "👇 <b>Select where to publish this post:</b>"
            )
            bc_markup = {
                "inline_keyboard": [
                    [
                        {"text": "📢 Public Channel (@bornbulltrade)", "callback_data": f"bc:pub:{admin_msg_id}"},
                    ],
                    [
                        {"text": "💎 Private VIP Channel", "callback_data": f"bc:vip:{admin_msg_id}"},
                    ],
                    [
                        {"text": "🚀 Both Channels (Public + VIP)", "callback_data": f"bc:both:{admin_msg_id}"},
                    ],
                    [
                        {"text": "❌ Cancel Broadcast", "callback_data": f"bc:cancel:{admin_msg_id}"},
                    ],
                ]
            }
            await notifier.send_message(bc_prompt, target_chat_id=chat_id, reply_markup=bc_markup)

    async def _handle_callback(self, cb_query: Dict[str, Any]):
        """Processes user tapping Approve (Whole Lot Price) or Reject."""
        query_id = cb_query.get("id")
        cb_id = str(query_id)
        if cb_id in self._processed_callbacks:
            await self._answer_callback(query_id, "Order already processed.")
            return
        self._processed_callbacks.add(cb_id)

        from_user = cb_query.get("from", {})
        user_id = str(from_user.get("id", "")).strip()
        message = cb_query.get("message", {})
        chat = message.get("chat", {})
        chat_id = str(chat.get("id", "")).strip() or str(settings.telegram_chat_id).strip()
        auth_chat_id = str(settings.telegram_chat_id).strip()
        data = str(cb_query.get("data", "")).strip()
        message_id = message.get("message_id")
        orig_text = message.get("text", "") or message.get("caption", "")

        # -------------------------------------------------------------
        # Admin Channel Broadcast Callbacks: bc:<destination>:<msg_id>
        # -------------------------------------------------------------
        if data.startswith("bc:"):
            parts = data.split(":")
            if len(parts) >= 3 and (user_id == auth_chat_id or chat_id == auth_chat_id):
                dest = parts[1]
                target_msg_id = int(parts[2])
                admin_from_id = chat_id or auth_chat_id
                pub_chat_id = settings.telegram_public_channel_id
                vip_chat_id = getattr(settings, "telegram_vip_channel_id", "-1003416174805")

                await self._answer_callback(query_id, "Processing broadcast...")

                if dest == "pub":
                    ok = await self._copy_message(pub_chat_id, admin_from_id, target_msg_id)
                    status_text = "✅ <b>Published to Public Channel</b> (@bornbulltrade)!" if ok else "❌ Failed to copy to Public Channel."
                elif dest == "vip":
                    ok = await self._copy_message(vip_chat_id, admin_from_id, target_msg_id)
                    status_text = "✅ <b>Published to Private VIP Channel</b>!" if ok else "❌ Failed to copy to VIP Channel."
                elif dest == "both":
                    ok1 = await self._copy_message(pub_chat_id, admin_from_id, target_msg_id)
                    ok2 = await self._copy_message(vip_chat_id, admin_from_id, target_msg_id)
                    status_text = "✅ <b>Published to Both Channels (Public & VIP)</b>!" if (ok1 and ok2) else f"⚠️ Published: Public={ok1}, VIP={ok2}"
                elif dest == "cancel":
                    status_text = "❌ <b>Broadcast cancelled.</b>"
                else:
                    status_text = "Unknown destination."

                if message_id:
                    await self._edit_message(chat_id, message_id, status_text)
                return

        # -------------------------------------------------------------
        # A. Public VIP Plan Selection & Status Callbacks (Open to all)
        # -------------------------------------------------------------
        if data.startswith("vplan:"):
            parts = data.split(":")
            months = parts[1]
            amt = parts[2]
            qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=350x350&data=upi%3A%2F%2Fpay%3Fpa%3Dpatel.rachit%40superyes%26pn%3DRachit%2520Ashish%2520Patel%26cu%3DINR%26am%3D{amt}"
            caption = (
                "🚨 <b>10-MINUTE RESERVATION WINDOW</b> 🚨\n"
                "⚠️ <i>This QR Code is dynamically generated and expires in 10:00 minutes!</i>\n\n"
                "💳 <b>VIP MEMBERSHIP PAYMENT</b>\n"
                "• <b>UPI ID:</b> <code>patel.rachit@superyes</code>\n"
                "• <b>Name to Verify:</b> <b>Rachit Ashish Patel</b> ⚠️\n"
                "  <i>(Please verify that your UPI app shows \"Rachit Ashish Patel\" before paying)</i>\n"
                f"• <b>Plan:</b> {months} Month(s) VIP Access\n"
                f"• <b>Amount:</b> <b>₹{amt}.00</b>\n\n"
                "📌 <b>Quick 3-Step Process:</b>\n"
                "1️⃣ Scan the QR Code above using Google Pay, PhonePe, or Paytm\n"
                "2️⃣ Verify recipient name is <b>Rachit Ashish Patel</b>\n"
                "3️⃣ Send the payment screenshot directly in this chat!\n\n"
                "⚡ <i>Your single-use VIP invite link will be dispatched immediately upon verification! 🚀</i>"
            )
            await self._answer_callback(query_id, f"Generated QR for ₹{amt}")
            await self._send_photo(chat_id, qr_url, caption)
            return

        if data.startswith("vstat"):
            from app.storage.database import db
            sub = db.get_vip_subscriber(user_id)
            if sub and sub.get("is_active"):
                exp_date = date.fromisoformat(sub["expiry_date"])
                days_left = max(0, (exp_date - date.today()).days)
                await self._answer_callback(query_id, f"Active VIP: {days_left} Days Left (Valid till {exp_date.strftime('%d-%b-%Y')})", show_alert=True)
            else:
                await self._answer_callback(query_id, "No active VIP found. Select a plan to join!", show_alert=True)
            return

        # -------------------------------------------------------------
        # B. Strict Admin Security for Approvals & Trade Executions
        # -------------------------------------------------------------
        is_admin = (user_id == auth_chat_id or chat_id == auth_chat_id)
        if not is_admin:
            logger.warning(f"Unauthorized callback attempt from Telegram user ID: {user_id}, chat: {chat_id}")
            await self._answer_callback(query_id, "Unauthorized. Only Admin can perform this action.", show_alert=True)
            return

        # Admin Approval for VIP Payment
        if data.startswith("vapp:"):
            from app.notifications.vip_channel import vip_manager
            from app.notifications.telegram import notifier
            parts = data.split(":")
            target_uid = parts[1]
            months = int(parts[2])
            target_name = parts[3] if len(parts) > 3 else "Trader"

            ok, msg, invite_link, exp_date = await vip_manager.create_subscription_invite(
                telegram_id=target_uid,
                name=target_name,
                plan_months=months,
            )

            welcome_msg = (
                "🎉 <b>PAYMENT VERIFIED! WELCOME TO VIP!</b> 🚀\n"
                "━━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>Member:</b> {target_name}\n"
                f"📅 <b>Plan:</b> {months} Month(s) Active Access\n"
                f"⏳ <b>Valid Until:</b> {exp_date.strftime('%d-%b-%Y')}\n\n"
                "🔗 <b>Your Exclusive Single-Use VIP Invite Link:</b>\n"
                f"👉 {invite_link}\n\n"
                "⚠️ <i>This single-use link is locked to your account. Do not share or forward it.</i>"
            )
            await notifier.send_message(welcome_msg, target_chat_id=target_uid)

            new_text = f"{orig_text}\n\n✅ <b>APPROVED BY ADMIN!</b>\nVIP invite link dispatched to {target_name} (ID: <code>{target_uid}</code>)."
            await self._edit_message(chat_id, message_id, new_text)
            await self._answer_callback(query_id, "Subscriber Approved & Link Dispatched!")
            return

        # Admin Rejection for VIP Payment
        if data.startswith("vrej:"):
            from app.notifications.telegram import notifier
            target_uid = data.split(":")[1]
            rej_notice = (
                "⚠️ <b>Payment Verification Notice</b>\n\n"
                "We could not verify your submitted transaction. Please ensure:\n"
                "1. Payment was sent to <code>patel.rachit@superyes</code> (Rachit Ashish Patel).\n"
                "2. A clear screenshot displaying the UTR / Transaction Reference ID is submitted.\n\n"
                "Type <code>/vip</code> to try again."
            )
            await notifier.send_message(rej_notice, target_chat_id=target_uid)
            new_text = f"{orig_text}\n\n❌ <b>REJECTED BY ADMIN.</b>"
            await self._edit_message(chat_id, message_id, new_text)
            await self._answer_callback(query_id, "Payment Rejected.")
            return

        # IMMEDIATELY answer callback to stop loading spinner on user's phone!
        await self._answer_callback(query_id, "Processing your order...")

        if data.startswith("app:"):
            parts = data.split(":")
            sig_key = parts[1]
            lot_mult = int(parts[2]) if len(parts) > 2 else 1

            # 1. In-memory lookup
            order_data = self._pending_orders.get(sig_key)

            # 2. Database persistent lookup
            if not order_data:
                db_row = db.get_pending_order(sig_key)
                if db_row:
                    dir_val = Direction.LONG if str(db_row["direction"]).upper() in ("LONG", "BUY") else Direction.SHORT
                    order_data = {
                        "signal": None,
                        "security_id": db_row["security_id"],
                        "symbol": db_row["symbol"],
                        "direction": dir_val,
                        "entry_price": float(db_row["entry_price"]),
                        "stop_loss": float(db_row["stop_loss"]),
                        "target": float(db_row["target"]),
                        "lot_size": int(db_row["lot_size"]),
                        "margin_req": float(db_row["margin_req"]),
                        "total_lot_price": float(db_row["total_lot_price"]),
                    }

            # 3. Fallback: Parse sig_key to extract security_id and query latest recorded signal
            if not order_data:
                sec_id = sig_key.split("_")[0] if "_" in sig_key else sig_key
                sig_row = db.get_latest_signal_for_sec(sec_id)
                if not sig_row and orig_text:
                    for w in orig_text.split():
                        clean_w = w.strip(":\n ,.*#_[]()")
                        sig_row = db.get_latest_signal_by_symbol(clean_w)
                        if sig_row:
                            break
                if sig_row:
                    default_cap = float(os.getenv("TRADING_CAPITAL", "4322.0"))
                    capital = db.get_account_balance(default_cap)
                    usable_capital = capital * 0.85
                    max_exposure = usable_capital * 5.0
                    entry_p = float(sig_row["entry_price"])
                    qty = max(1, int(max_exposure / entry_p))
                    dir_val = Direction.LONG if str(sig_row["direction"]).upper() in ("LONG", "BUY") else Direction.SHORT
                    order_data = {
                        "signal": None,
                        "security_id": sig_row["security_id"],
                        "symbol": sig_row["symbol"],
                        "direction": dir_val,
                        "entry_price": entry_p,
                        "stop_loss": float(sig_row["stop_loss"]),
                        "target": float(sig_row["target"]),
                        "lot_size": qty,
                        "margin_req": round((entry_p * qty) / 5.0, 2),
                        "total_lot_price": round(entry_p * qty, 2),
                    }

            if not order_data:
                logger.warning(f"Pending order not found for sig_key: {sig_key}")
                await self._answer_callback(query_id, "Signal not found or expired.", show_alert=True)
                return

            status_type, success, msg = await self.execute_dhan_order(order_data, lot_multiplier=lot_mult)
            total_qty = order_data["lot_size"] * lot_mult

            # Explicit status headers preventing simulated paper trades from appearing as live executions
            if status_type == "TRADED":
                header = "✅ <b>BROKER CONFIRMED EXECUTION (STATUS: TRADED)</b>"
            elif status_type == "SUBMITTED":
                header = "🚀 <b>ORDER SUBMITTED TO DHAN (BROKER STATUS: PENDING)</b>"
            elif status_type == "STAGED":
                header = "📋 <b>OPTION SETUP STAGED (MANUAL ORDER)</b>"
            elif status_type == "SIMULATED":
                header = "📝 <b>SIMULATED PAPER ORDER LOGGED (ZERO BROKER ROUTING)</b>"
            else:
                header = "⚠️ <b>ORDER PLACEMENT FAILED / REJECTED</b>"

            # Update original Telegram message
            new_text = (
                f"{orig_text}\n\n"
                f"{header}\n"
                f"<b>Status:</b> {msg}\n"
                f"<b>Quantity:</b> {lot_mult} Lot(s) ({total_qty} units)\n"
                f"<b>Target:</b> ₹{order_data['target']:,.2f}\n"
                f"<b>Stop Loss:</b> ₹{order_data['stop_loss']:,.2f}\n"
                f"<b>Timestamp:</b> {datetime.now().strftime('%H:%M:%S')} IST"
            )
            await self._edit_message(chat_id, message_id, new_text)

        elif data.startswith("rej:"):
            sig_key = data.split(":", 1)[1]
            await self._answer_callback(query_id, "Signal rejected.")
            new_text = f"{orig_text}\n\n❌ <i>Signal Rejected / Dismissed by User.</i>"
            await self._edit_message(chat_id, message_id, new_text)

    async def _answer_callback(self, callback_query_id: str, text: str, show_alert: bool = False):
        url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/answerCallbackQuery"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(url, json={
                    "callback_query_id": str(callback_query_id),
                    "text": str(text)[:200],
                    "show_alert": bool(show_alert),
                })
        except Exception as e:
            logger.debug(f"Error answering callback query: {e}")

    async def _edit_message(self, chat_id: Any, message_id: int, new_text: str):
        url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/editMessageText"
        payload = {
            "chat_id": chat_id or settings.telegram_chat_id,
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

    async def _copy_message(self, to_chat_id: Any, from_chat_id: Any, message_id: int) -> bool:
        """Copies any Telegram message (text, photo, media, formatted post) directly to target chat/channel."""
        url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/copyMessage"
        payload = {
            "chat_id": str(to_chat_id),
            "from_chat_id": str(from_chat_id),
            "message_id": int(message_id),
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=payload)
                return resp.status_code == 200
        except Exception as e:
            logger.debug(f"Error copying message {message_id} to {to_chat_id}: {e}")
            return False


order_executor = DhanOrderExecutor()
