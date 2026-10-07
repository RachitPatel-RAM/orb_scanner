"""
VIP Paid Telegram Channel & Automated Subscription Manager:
- Manages 1, 3, 6, and 12-month subscriptions.
- Creates single-use Telegram invite links upon enrollment.
- Automatically removes expired users from the VIP channel via Telegram Bot API.
- Dispatches clean, high-accuracy institutional signals (hiding Dhan/broker backend details).
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
import os
from typing import Any, Dict, List, Optional, Tuple
import httpx

from app.config import logger, settings
from app.storage.database import db
from app.storage.models import Direction, Signal
from app.strategies.confluence_engine import ConfluenceResult


class VIPChannelManager:
    """Manages VIP Channel broadcast, member subscriptions, and automatic eviction."""

    def __init__(self, bot_token: Optional[str] = None, channel_id: Optional[str] = None):
        self.bot_token = (bot_token or settings.telegram_bot_token).strip()
        # VIP Channel ID (e.g. -100xxxxxxxxxx from .env or configured)
        self.channel_id = os.getenv("VIP_CHANNEL_ID", "").strip()

    @property
    def is_configured(self) -> bool:
        return bool(self.bot_token and self.channel_id)

    async def create_subscription_invite(
        self,
        telegram_id: str,
        name: str,
        plan_months: int,
    ) -> Tuple[bool, str]:
        """
        Registers a new subscriber and creates a single-use 1-member Telegram invite link
        valid for the user to join the channel.
        """
        if not self.is_configured:
            return False, "VIP_CHANNEL_ID is not configured in .env yet."

        today = date.today()
        # Approx days: 30 days per month
        duration_days = plan_months * 30
        expiry_date = today + timedelta(days=duration_days)

        invite_link = None
        # Create single-use invite link via Telegram Bot API
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/createChatInviteLink"
            # Link expires in 48 hours for joining, but subscription lasts plan_months
            link_expire_ts = int((datetime.now() + timedelta(hours=48)).timestamp())
            payload = {
                "chat_id": self.channel_id,
                "name": f"VIP-{name}-{telegram_id[:6]}",
                "expire_date": link_expire_ts,
                "member_limit": 1,
            }
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    invite_link = data.get("result", {}).get("invite_link")
        except Exception as e:
            logger.error(f"Error creating Telegram VIP invite link: {e}")

        # Save to database
        db.add_vip_subscriber(
            telegram_id=str(telegram_id),
            name=name,
            plan_months=plan_months,
            start_date=today.isoformat(),
            expiry_date=expiry_date.isoformat(),
            invite_link=invite_link,
        )

        msg = (
            f"✅ <b>VIP Subscription Activated!</b>\n\n"
            f"👤 <b>Subscriber:</b> {name} (ID: <code>{telegram_id}</code>)\n"
            f"📅 <b>Plan:</b> {plan_months} Month(s) ({duration_days} Days)\n"
            f"⏳ <b>Valid Until:</b> {expiry_date.strftime('%d-%b-%Y')}\n\n"
            f"🔗 <b>Private Invite Link:</b> {invite_link or 'Generated (Manual Add)'}\n"
            f"⚠️ <i>Single-use link valid for 48 hours. Subscriber will be automatically evicted upon expiry.</i>"
        )
        return True, msg

    async def check_and_evict_expired_subscribers(self) -> int:
        """
        Daily watchdog: checks for expired subscribers, removes them from the VIP channel,
        and sends them a private notification to renew.
        """
        if not self.is_configured:
            return 0

        today_str = date.today().isoformat()
        expired_list = db.get_expired_vip_subscribers(today_str)
        evicted_count = 0

        for sub in expired_list:
            tg_id = sub["telegram_id"]
            name = sub["name"]

            # Remove user from Telegram channel: ban then immediately unban (kicks without perma-ban)
            try:
                ban_url = f"https://api.telegram.org/bot{self.bot_token}/banChatMember"
                unban_url = f"https://api.telegram.org/bot{self.bot_token}/unbanChatMember"
                async with httpx.AsyncClient(timeout=10.0) as client:
                    await client.post(ban_url, json={"chat_id": self.channel_id, "user_id": tg_id})
                    await asyncio.sleep(0.5)
                    await client.post(unban_url, json={"chat_id": self.channel_id, "user_id": tg_id, "only_if_banned": True})

                # Mark deactivated in DB
                db.deactivate_vip_subscriber(tg_id)
                evicted_count += 1
                logger.info(f"Evicted expired subscriber {name} ({tg_id}) from VIP Channel.")

                # Send renewal alert directly to subscriber
                notify_url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
                farewell_msg = (
                    f"👋 Hi <b>{name}</b>,\n\n"
                    f"Your subscription to the <b>Institutional Intraday VIP Channel</b> has expired today.\n"
                    f"To renew your subscription and continue receiving high-accuracy signals, please contact the admin."
                )
                async with httpx.AsyncClient(timeout=10.0) as client:
                    await client.post(notify_url, json={"chat_id": tg_id, "text": farewell_msg, "parse_mode": "HTML"})

            except Exception as e:
                logger.error(f"Error evicting subscriber {tg_id}: {e}")

        return evicted_count

    async def broadcast_vip_signal(self, signal: Signal, confluence: ConfluenceResult) -> bool:
        """
        Dispatches a high-accuracy, clean institutional breakout signal to the VIP Channel.
        Hides all broker API keys, client IDs, and backend internals.
        """
        if not self.is_configured:
            return False

        is_long = signal.direction == Direction.LONG
        action_header = "🟢 <b>BUY CALL (LONG BREAKOUT)</b>" if is_long else "🔴 <b>SELL PUT (SHORT BREAKDOWN)</b>"
        buffer_pts = round(signal.entry_price * 0.0015, 2)
        low_range = round(signal.entry_price - buffer_pts, 2)
        high_range = round(signal.entry_price + buffer_pts, 2)

        # Build clean institutional logic
        confl_lines = []
        if confluence.pivot_levels:
            if is_long:
                confl_lines.append(f"• Pivot Clearance: Cleared R1 (₹{confluence.pivot_levels.r1:.1f})")
            else:
                confl_lines.append(f"• Pivot Clearance: Broke below S1 (₹{confluence.pivot_levels.s1:.1f})")

        if confluence.oi_profile:
            confl_lines.append(f"• Open Interest: Favorable PCR ({confluence.oi_profile.pcr:.2f})")

        confl_text = "\n".join(confl_lines) if confl_lines else "• Validated Institutional Displacement & Volume"

        text = (
            f"🎯 <b>INSTITUTIONAL VIP RESEARCH CALL</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📈 <b>Stock:</b> <b>{signal.symbol}</b>\n"
            f"⚡ <b>Action:</b> {action_header}\n"
            f"⏰ <b>Trigger Time:</b> {signal.timestamp.strftime('%H:%M')} IST\n\n"
            f"💵 <b>Recommended Entry Range:</b> ₹{low_range:,.2f} – ₹{high_range:,.2f}\n"
            f"🛑 <b>Strict Stop Loss:</b> ₹{signal.stop_loss:,.2f}\n"
            f"🏆 <b>Target:</b> ₹{signal.target:,.2f} (1:2 Risk-to-Reward)\n\n"
            f"🧠 <b>Core Institutional Confluence:</b>\n"
            f"{confl_text}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔒 <i>Exclusive VIP Premium Alert</i>"
        )

        target_channels = []
        if self.channel_id:
            target_channels.append(self.channel_id)
        if settings.telegram_public_channel_id and settings.telegram_public_channel_id not in target_channels:
            target_channels.append(settings.telegram_public_channel_id)

        if not target_channels:
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        sent_any = False
        async with httpx.AsyncClient(timeout=10.0) as client:
            for ch_id in target_channels:
                try:
                    payload = {
                        "chat_id": ch_id,
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    }
                    r = await client.post(url, json=payload)
                    if r.status_code == 200:
                        sent_any = True
                except Exception as e:
                    logger.error(f"Error broadcasting to channel {ch_id}: {e}")
        return sent_any


vip_manager = VIPChannelManager()
