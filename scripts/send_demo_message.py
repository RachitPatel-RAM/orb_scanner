"""
Broadcasts the exact requested demo message to the Public Channel and Admin Chat.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.notifications.telegram import notifier


DEMO_TEXT = (
    "⚡ <b>BORNBULL TRADE | SYSTEM STATUS & ROLE GUIDE</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━\n\n"
    "📢 <b>Public Channel (@bornbulltrade):</b>\n"
    "• Free community builder.\n"
    "• Only Trade 1 is 100% FREE with full levels.\n"
    "• Trade 2+ shows FOMO teasers.\n"
    "• Shows verified Target hits with 1-lot profits.\n"
    "• Night recap and payment proofs.\n\n"
    "💎 <b>Private VIP Channel:</b>\n"
    "• Paid premium subscribers only.\n"
    "• Receives all trades of the day (Index & Stocks).\n"
    "• Exact real-time trailing stop updates.\n"
    "• Runners guidance (T1, T2, T3).\n\n"
    "🤖 <b>Direction Alert Bot (@Directionalertbot):</b>\n"
    "• Connected to market data & scanner.\n"
    "• Broadcasts messages to Public & VIP channels.\n"
    "• Sends 1-click execution buttons to Admin.\n"
    "• No payments here: Non-admins are redirected to @bornbullsupportbot.\n\n"
    "💬 <b>Support Bot (@bornbullsupportbot):</b>\n"
    "• Dedicated customer support & membership desk.\n"
    "• Handles /plans, /upi, inquiries.\n"
    "• Accepts payment screenshots.\n"
    "• Admin 1-click approval delivers private VIP link.\n"
    "• Guarantees 12-hour response for user messages."
)


async def main():
    print(f"Sending demo message to Public Channel ({settings.telegram_public_channel_id})...")
    ok_pub = await notifier.send_message(
        DEMO_TEXT,
        target_chat_id=settings.telegram_public_channel_id,
        idempotency_key=f"demo_test_{int(asyncio.get_event_loop().time())}",
    )
    print(f"Public Channel send status: {ok_pub}")

    print(f"Sending demo message to Admin Chat ({settings.telegram_chat_id})...")
    ok_adm = await notifier.send_message(
        DEMO_TEXT,
        target_chat_id=settings.telegram_chat_id,
        idempotency_key=f"demo_adm_{int(asyncio.get_event_loop().time())}",
    )
    print(f"Admin Chat send status: {ok_adm}")


if __name__ == "__main__":
    asyncio.run(main())
