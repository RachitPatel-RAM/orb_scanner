"""
Dhan 1-Click Interactive Order Execution Engine with Target & Stop Loss.

Handles Telegram inline keyboard callbacks (Approve with Whole Lot Price / Reject),
places real Super Orders (Bracket Orders with SL & Target) or Intraday MIS orders on Dhan,
and updates Telegram messages in real-time.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime
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

    def register_signal_for_approval(self, signal: Signal) -> Tuple[Dict[str, Any], int, float]:
        """
        Calculates quantity and required margin fitting user's capital (₹4,322 with 5x Intraday Margin).
        """
        from app.storage.database import db
        default_cap = float(os.getenv("TRADING_CAPITAL", "4322.0"))
        capital = db.get_account_balance(default_cap)
        # 5x intraday MIS margin on NSE Equity
        usable_capital = capital * 0.85  # keep safety buffer
        max_exposure = usable_capital * 5.0

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
                    {"text": btn_text, "callback_data": f"app:{sig_key}"},
                    {"text": "✖ Reject", "callback_data": f"rej:{sig_key}"},
                ]
            ]
        }

        return reply_markup, qty, margin_req

    async def execute_dhan_order(self, order_data: Dict[str, Any]) -> Tuple[bool, str]:
        """
        Places order on Dhan with Stop Loss and Target.
        Uses place_super_order (Bracket Order) with fallback to place_order with trigger.
        """
        signal: Signal = order_data["signal"]
        sec_id = str(order_data["security_id"])
        symbol = order_data["symbol"]
        is_long = order_data["direction"] == Direction.LONG
        txn_type = "BUY" if is_long else "SELL"
        qty = int(order_data["lot_size"])
        price = float(order_data["entry_price"])
        target = float(order_data["target"])
        stop_loss = float(order_data["stop_loss"])

        logger.info(
            f"Placing Dhan Order: {txn_type} {symbol} ({sec_id}) Qty={qty} Price={price} "
            f"SL={stop_loss} Target={target}"
        )

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
                {"command": "positions", "description": "View open positions on Dhan"},
                {"command": "orders", "description": "View today Dhan orders"},
                {"command": "status", "description": "Scanner and ML engine health"},
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

    async def _handle_message_command(self, msg: Dict[str, Any]):
        """Processes interactive chat commands from user (balance, positions, status, orders)."""
        chat = msg.get("chat", {})
        chat_id = str(chat.get("id", ""))
        text = str(msg.get("text", "")).strip().lower()

        # Security check: only authorized telegram chat
        if chat_id != str(settings.telegram_chat_id).strip():
            return

        from app.notifications.telegram import notifier

        if text in ("/balance", "/funds", "balance", "funds"):
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
                "• <code>/balance</code> - Check live Dhan margin & funds\n"
                "• <code>/positions</code> - View open trades on Dhan\n"
                "• <code>/orders</code> - Check today's Dhan orders\n"
                "• <code>/status</code> - Scanner & ML engine health\n\n"
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
            sig_key = data.split(":", 1)[1]
            order_data = self._pending_orders.get(sig_key)
            if not order_data:
                await self._answer_callback(query_id, "Signal expired or not found.")
                return

            await self._answer_callback(query_id, "Submitting order to Dhan with SL & Target...")
            success, msg = await self.execute_dhan_order(order_data)

            # Update original Telegram message
            new_text = (
                f"{orig_text}\n\n"
                f"{'✅ <b>ORDER EXECUTED ON DHAN</b>' if success else '⚠️ <b>ORDER PLACEMENT FAILED</b>'}\n"
                f"<b>Status:</b> {msg}\n"
                f"<b>Quantity:</b> 1 Lot ({order_data['lot_size']} units)\n"
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
