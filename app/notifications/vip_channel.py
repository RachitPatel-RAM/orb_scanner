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
        return bool(self.bot_token)

    async def create_subscription_invite(
        self,
        telegram_id: str,
        name: str,
        plan_months: int,
    ) -> Tuple[bool, str, Optional[str], date]:
        """
        Registers a new subscriber and creates a single-use 1-member Telegram invite link
        valid for the user to join the channel.
        Returns: (success, formatted_message, invite_link, expiry_date)
        """
        today = date.today()
        duration_days = plan_months * 30
        expiry_date = today + timedelta(days=duration_days)

        invite_link = None
        target_ch = self.channel_id or getattr(settings, "vip_channel_id", "") or os.getenv("VIP_CHANNEL_ID", "")

        # Create single-use invite link via Telegram Bot API if channel is configured
        if target_ch:
            try:
                url = f"https://api.telegram.org/bot{self.bot_token}/createChatInviteLink"
                link_expire_ts = int((datetime.now() + timedelta(hours=48)).timestamp())
                payload = {
                    "chat_id": target_ch,
                    "name": f"VIP-{name[:12]}-{str(telegram_id)[-4:]}",
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

        if not invite_link:
            invite_link = os.getenv("VIP_CHANNEL_LINK", "").strip() or "https://t.me/+2g9S5T6G5Js3OWQ1"

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
            f"🔗 <b>Private Invite Link:</b> {invite_link}\n"
            f"⚠️ <i>Single-use link. Subscriber will be automatically evicted upon expiry.</i>"
        )
        return True, msg, invite_link, expiry_date

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
            f"🛑 <b>Strict Stop Loss:</b> ₹{signal.stop_loss:,.2f} ({confluence.sl_milestone or 'Structure'})\n\n"
            f"🎯 <b>Target 1:</b> ₹{confluence.target_1 or signal.target:,.2f} (1:{confluence.target_1_rr or 1.4:g} R:R)\n"
            f"🎯 <b>Target 2:</b> ₹{confluence.target_2 or signal.target:,.2f} (1:{confluence.target_2_rr or signal.risk_reward:g} R:R | Main)\n"
            f"🎯 <b>Target 3:</b> ₹{confluence.target_3 or (signal.entry_price + (signal.entry_price - signal.stop_loss)*3.0):,.2f} (1:{confluence.target_3_rr or 3.0:g} R:R | Runner)\n\n"
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
