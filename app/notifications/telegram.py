"""
Telegram Notification Engine for ORB Breakouts, Targets, Stops, and System Alerts.

Includes formatting, retry logic, error isolation, credential safety, and idempotency tracking.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Dict, Optional
import httpx

from app.config import logger, settings
from app.market.session import default_session
from app.storage.database import db
from app.storage.models import Direction, ExitReason, PaperTrade, Signal


class TelegramNotifier:
    """Dispatches asynchronous alerts to Telegram channel/group."""

    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        max_retries: int = 3,
    ):
        self.bot_token = (bot_token or settings.telegram_bot_token).strip()
        self.chat_id = (chat_id or settings.telegram_chat_id).strip()
        self.max_retries = max_retries

        # Rate limiting & spam suppression
        self._last_error_message: Optional[str] = None
        self._last_error_time: Optional[datetime] = None

    @property
    def is_configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    async def discover_chat_id(self) -> Optional[str]:
        """
        Polls getUpdates API to automatically discover chat_id from recent messages.
        Returns the chat_id if found.
        """
        if not self.bot_token:
            return None
        url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    results = data.get("result", [])
                    if results:
                        # Grab the latest chat_id
                        latest_msg = results[-1].get("message", {}) or results[-1].get("channel_post", {})
                        chat = latest_msg.get("chat", {})
                        discovered_id = str(chat.get("id", "")).strip()
                        if discovered_id:
                            self.chat_id = discovered_id
                            return discovered_id
        except Exception as e:
            logger.warning(f"Error querying getUpdates: {e}")
        return None

    async def send_message(
        self,
        text: str,
        idempotency_key: Optional[str] = None,
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Sends a Markdown-formatted message to Telegram.
        Catches all network and API exceptions to ensure market engine never crashes.
        """
        if not self.is_configured:
            logger.warning("Telegram notifier not configured. Message suppressed:\n" + text)
            return False

        # Idempotency check
        if idempotency_key and db.is_alert_sent(idempotency_key):
            logger.info(f"Duplicate Telegram alert suppressed for key: {idempotency_key}")
            return True

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload: Dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup

        success = False
        last_err: Optional[str] = None
        sent_msg_id: Optional[int] = None

        for attempt in range(1, self.max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(url, json=payload)
                    if resp.status_code == 200:
                        success = True
                        data = resp.json() if callable(resp.json) else resp.json
                        if isinstance(data, dict):
                            raw_mid = data.get("result", {}).get("message_id") if isinstance(data.get("result"), dict) else None
                            if raw_mid is not None and str(raw_mid).isdigit():
                                sent_msg_id = int(raw_mid)
                        break
                    elif resp.status_code == 429:
                        # Rate limit backoff
                        retry_after = int(resp.headers.get("Retry-After", 2))
                        logger.warning(f"Telegram 429 Rate Limit. Sleeping {retry_after}s...")
                        await asyncio.sleep(retry_after)
                    else:
                        last_err = f"HTTP {resp.status_code}: {resp.text}"
                        logger.error(f"Telegram API error (attempt {attempt}): {last_err}")
            except Exception as e:
                # Mask token in case URL was logged in exception
                err_str = str(e).replace(self.bot_token, "BOT_TOKEN_REDACTED")
                last_err = err_str
                logger.error(f"Telegram connection exception (attempt {attempt}): {err_str}")

            if attempt < self.max_retries:
                await asyncio.sleep(attempt * 1.5)

        # Store alert with message_id for 24h auto-deletion
        alert_key = idempotency_key or f"msg_{datetime.now().timestamp()}"
        db.record_alert(
            idempotency_key=alert_key,
            message=text,
            success=success,
            error_message=last_err if not success else None,
            message_id=sent_msg_id,
            chat_id=self.chat_id,
        )

        return success

    async def cleanup_old_messages(self, older_than_hours: int = 24) -> int:
        """
        Deletes messages older than 24 hours from Telegram chat using deleteMessage API.
        Keeps Telegram chat clean automatically.
        """
        if not self.bot_token:
            return 0

        old_alerts = db.get_uncleaned_alerts(older_than_hours=older_than_hours)
        if not old_alerts:
            return 0

        logger.info(f"Running Telegram message cleanup: found {len(old_alerts)} messages > {older_than_hours}h old.")
        deleted_count = 0

        async with httpx.AsyncClient(timeout=10.0) as client:
            for alert in old_alerts:
                alert_id = alert["id"]
                msg_id = alert["message_id"]
                chat_id = alert["chat_id"]

                del_url = f"https://api.telegram.org/bot{self.bot_token}/deleteMessage"
                try:
                    resp = await client.post(del_url, json={"chat_id": chat_id, "message_id": msg_id})
                    if resp.status_code == 200 or resp.status_code == 400:
                        # 400 usually means message was already deleted by user or expired
                        db.mark_alert_deleted(alert_id)
                        deleted_count += 1
                except Exception as e:
                    logger.debug(f"Failed to delete Telegram message {msg_id}: {e}")

        logger.info(f"Cleaned up {deleted_count} old Telegram messages.")
        return deleted_count

    def send_message_sync(self, text: str, idempotency_key: Optional[str] = None) -> bool:
        """Synchronous helper for non-async contexts."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # In running loop, schedule task
                asyncio.create_task(self.send_message(text, idempotency_key))
                return True
            else:
                return loop.run_until_complete(self.send_message(text, idempotency_key))
        except RuntimeError:
            return asyncio.run(self.send_message(text, idempotency_key))

    async def send_startup_message(
        self,
        stocks_count: int,
        strategy_name: str = "ORB-15",
        dhan_connected: bool = True,
        feed_connected: bool = True,
    ) -> bool:
        """Formats and sends the standard startup notification."""
        msg = (
            "<b>✅ ORB Scanner Online</b>\n\n"
            f"<b>Dhan:</b> {'Connected' if dhan_connected else 'Disconnected'}\n"
            f"<b>Data Feed:</b> {'Connected' if feed_connected else 'Disconnected'}\n"
            f"<b>Universe:</b> {stocks_count} stocks\n"
            f"<b>Strategy:</b> {strategy_name}\n"
            "<b>Mode:</b> ALERT ONLY\n"
            "<b>Order Placement:</b> DISABLED\n"
            f"<b>Time:</b> {default_session.now().strftime('%Y-%m-%d %H:%M:%S')} IST"
        )
        return await self.send_message(msg)

    async def send_signal(self, signal: Signal) -> bool:
        """Dispatches rich breakout alert with 1-click execution button showing whole lot price."""
        from app.trading.order_executor import order_executor
        reply_markup, lot_size, total_lot_price = order_executor.register_signal_for_approval(signal)

        is_long = signal.direction == Direction.LONG
        header = "🟢 <b>ORB LONG BREAKOUT</b>" if is_long else "🔴 <b>ORB SHORT BREAKOUT</b>"
        time_str = signal.timestamp.strftime("%H:%M")

        text = (
            f"{header}\n\n"
            f"<b>Stock:</b> {signal.symbol}\n"
            f"<b>Exchange:</b> NSE\n"
            f"<b>Time:</b> {time_str} IST\n\n"
            f"<b>Entry:</b> ₹{signal.entry_price:,.2f}\n"
            f"<b>ORB High:</b> ₹{signal.orb_high:,.2f}\n"
            f"<b>ORB Low:</b> ₹{signal.orb_low:,.2f}\n"
            f"<b>Stop Loss:</b> ₹{signal.stop_loss:,.2f}\n"
            f"<b>Target:</b> ₹{signal.target:,.2f}\n"
            f"<b>Risk Reward:</b> 1:{signal.risk_reward:g}\n\n"
            f"<b>Lot Size:</b> {lot_size} units\n"
            f"<b>Total Lot Value:</b> ₹{total_lot_price:,.2f}\n\n"
            f"<b>Confirmation:</b> {settings.strategy.signal_timeframe}-minute candle close\n"
            f"<b>Strategy:</b> {signal.strategy}\n\n"
            f"<i>Tap the button below to execute 1 lot with Target & Stop Loss attached:</i>"
        )
        return await self.send_message(
            text,
            idempotency_key=signal.idempotency_key,
            reply_markup=reply_markup,
        )

    async def send_target_hit(self, trade: PaperTrade) -> bool:
        """Sends alert when a paper trade reaches its target."""
        time_str = trade.exit_time.strftime("%H:%M") if trade.exit_time else "N/A"
        idemp = f"{trade.trade_date.isoformat()}_{trade.security_id}_TARGET_{trade.direction.value}"

        text = (
            "🎯 <b>TARGET HIT</b>\n\n"
            f"<b>Stock:</b> {trade.symbol}\n"
            f"<b>Direction:</b> {trade.direction.value}\n"
            f"<b>Exit Price:</b> ₹{trade.exit_price:,.2f}\n"
            f"<b>Target:</b> ₹{trade.target:,.2f}\n"
            f"<b>PnL:</b> +₹{trade.pnl:,.2f} (+{trade.r_multiple:.2f}R)\n"
            f"<b>Time:</b> {time_str} IST"
        )
        return await self.send_message(text, idempotency_key=idemp)

    async def send_stop_hit(self, trade: PaperTrade) -> bool:
        """Sends alert when a paper trade is stopped out."""
        time_str = trade.exit_time.strftime("%H:%M") if trade.exit_time else "N/A"
        idemp = f"{trade.trade_date.isoformat()}_{trade.security_id}_STOP_{trade.direction.value}"

        text = (
            "🛑 <b>STOP LOSS HIT</b>\n\n"
            f"<b>Stock:</b> {trade.symbol}\n"
            f"<b>Direction:</b> {trade.direction.value}\n"
            f"<b>Exit Price:</b> ₹{trade.exit_price:,.2f}\n"
            f"<b>Stop Loss:</b> ₹{trade.stop_loss:,.2f}\n"
            f"<b>PnL:</b> ₹{trade.pnl:,.2f} ({trade.r_multiple:.2f}R)\n"
            f"<b>Time:</b> {time_str} IST"
        )
        return await self.send_message(text, idempotency_key=idemp)

    async def send_error(self, error_msg: str, cooldown_minutes: int = 15) -> bool:
        """Sends an operational error alert with spam suppression."""
        now = default_session.now()

        # Suppress identical messages within cooldown period
        if (
            self._last_error_message == error_msg
            and self._last_error_time
            and (now - self._last_error_time).total_seconds() < (cooldown_minutes * 60)
        ):
            logger.info("Suppressed repeated error Telegram alert.")
            return False

        self._last_error_message = error_msg
        self._last_error_time = now

        text = (
            "⚠️ <b>ORB Scanner Warning</b>\n\n"
            f"<b>Details:</b> {error_msg}\n"
            f"<b>Time:</b> {now.strftime('%H:%M:%S')} IST"
        )
        return await self.send_message(text)

    async def send_daily_summary(self, summary: Dict[str, Any]) -> bool:
        """Sends daily performance and summary report after market close."""
        date_str = summary.get("date", default_session.now().date().isoformat())
        pnl = summary.get("pnl", 0.0)
        pnl_prefix = "+" if pnl > 0 else ""

        text = (
            "📊 <b>ORB DAILY SUMMARY</b>\n\n"
            f"<b>Date:</b> {date_str}\n"
            f"<b>Stocks monitored:</b> {summary.get('monitored_stocks', 0)}\n"
            f"<b>Signals generated:</b> {summary.get('total_signals', 0)}\n"
            f"<b>Long:</b> {summary.get('long_signals', 0)}\n"
            f"<b>Short:</b> {summary.get('short_signals', 0)}\n"
            f"<b>Targets hit:</b> {summary.get('targets_hit', 0)}\n"
            f"<b>Stops hit:</b> {summary.get('stops_hit', 0)}\n"
            f"<b>Open/EOD exits:</b> {summary.get('eod_exits', 0)}\n"
            f"<b>Paper P&amp;L:</b> {pnl_prefix}₹{pnl:,.2f}\n"
            f"<b>Win rate:</b> {summary.get('win_rate', 0.0):.1f}%"
        )
        idemp = f"{date_str}_DAILY_SUMMARY"
        return await self.send_message(text, idempotency_key=idemp)


notifier = TelegramNotifier()
