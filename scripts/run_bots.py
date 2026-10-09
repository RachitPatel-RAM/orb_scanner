"""
Unified Telegram Bots Runner:
Runs both @Directionalertbot and @bornbullsupportbot polling listeners simultaneously.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import logger, settings
from app.notifications.support_bot import support_bot
from app.notifications.vip_channel import vip_manager
from app.trading.order_executor import order_executor


async def vip_eviction_watchdog():
    """Continuous 24x7 watchdog that removes expired VIP subscribers automatically."""
    logger.info("VIP Subscription Eviction Watchdog started (hourly check).")
    while True:
        try:
            evicted = await vip_manager.check_and_evict_expired_subscribers()
            if evicted > 0:
                logger.info(f"VIP Watchdog evicted {evicted} expired subscribers.")
        except Exception as e:
            logger.debug(f"VIP Watchdog error: {e}")
        await asyncio.sleep(3600)


async def run_both_bots():
    print("=" * 75)
    print("     STARTING BORNBULL DUAL TELEGRAM BOTS RUNNER")
    print("=" * 75)
    print(f"1. Direction Alert Bot: Connected to token {settings.telegram_bot_token[:4]}****")
    print(f"2. Support & Payment Bot: Connected to token {settings.support_bot_token[:4]}****")
    print(f"3. Public Channel: {settings.telegram_public_channel_id}")
    print(f"4. Admin ID: {settings.telegram_chat_id}")
    print("-" * 75)
    print("Both bots are now active, polling Telegram, and listening 24x7.")
    print("VIP 24x7 Auto-Eviction Watchdog is active.")
    print("Send /start to @bornbullsupportbot or @Directionalertbot to test live.")
    print("=" * 75)

    tasks = [
        asyncio.create_task(order_executor.run_telegram_listener()),
        asyncio.create_task(vip_eviction_watchdog()),
    ]
    if support_bot.is_configured:
        tasks.append(asyncio.create_task(support_bot.run_support_bot_listener()))

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(run_both_bots())
    except KeyboardInterrupt:
        print("\nBots stopped gracefully.")
