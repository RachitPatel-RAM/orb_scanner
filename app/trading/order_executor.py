"""
Dhan 1-Click Interactive Order Execution Engine with Target & Stop Loss.

Handles Telegram inline keyboard callbacks (Approve with Whole Lot Price / Reject),
places real Super Orders (Bracket Orders with SL & Target) or Intraday MIS orders on Dhan,
and updates Telegram messages in real-time.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Dict, Optional, Tuple
import httpx
from dhanhq import DhanContext, dhanhq

from app.config import logger, settings
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
        Calculates whole lot price and returns the inline keyboard markup for Telegram.
        """
        lot_size = instrument_manager.get_lot_size(signal.security_id)
        total_lot_price = round(signal.entry_price * lot_size, 2)
        sig_key = f"{signal.security_id}_{int(signal.timestamp.timestamp())}"

        self._pending_orders[sig_key] = {
            "signal": signal,
            "security_id": signal.security_id,
            "symbol": signal.symbol,
            "direction": signal.direction,
            "entry_price": signal.entry_price,
            "stop_loss": signal.stop_loss,
            "target": signal.target,
            "lot_size": lot_size,
            "total_lot_price": total_lot_price,
            "created_at": datetime.now(),
        }

        # Format button with whole lot price as requested by user
        if signal.direction == Direction.LONG:
            btn_text = f"🟢 Buy 1 Lot (₹{total_lot_price:,.2f})"
        else:
            btn_text = f"🔴 Sell 1 Lot (₹{total_lot_price:,.2f})"

        reply_markup = {
            "inline_keyboard": [
                [
                    {"text": btn_text, "callback_data": f"app:{sig_key}"},
                    {"text": "✖ Reject", "callback_data": f"rej:{sig_key}"},
                ]
            ]
        }

        return reply_markup, lot_size, total_lot_price

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
