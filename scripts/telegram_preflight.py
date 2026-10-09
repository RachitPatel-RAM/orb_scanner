"""
Telegram Bot Capability Preflight Tool (Section 12).

Performs a read-only capability preflight:
  1. getMe: verifies bot identity & active API connectivity.
  2. getChat: verifies target channel existence & metadata for Public and VIP channels.
  3. getChatMember: verifies bot's administrator role and specific rights (can_post_messages, can_invite_users).

Security & Redaction Rules:
  - NEVER logs or displays raw bot token, secret invitation URLs, or query parameters.
  - Token is masked in all reports (e.g. 1234****:****).
  - Can be run safely in CI or terminal without history pollution.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from app.config import settings


def mask_token(token: str) -> str:
    """Masks bot token to ensure secret privacy in all console outputs."""
    if not token or len(token) < 10:
        return "[NOT_CONFIGURED]"
    prefix = token[:4]
    suffix = token[-4:]
    return f"{prefix}****:****{suffix}"


async def run_preflight(token_override: Optional[str] = None) -> bool:
    token = token_override or settings.telegram_bot_token
    if not token:
        print("\n[ERROR] TELEGRAM_BOT_TOKEN is not configured in .env or environment.")
        return False

    base_url = f"https://api.telegram.org/bot{token}"
    masked = mask_token(token)

    print("=" * 75)
    print("      BORNBULL TELEGRAM READ-ONLY CAPABILITY PREFLIGHT")
    print("=" * 75)
    print(f"Bot Token (Masked): {masked}")
    print(f"Public Channel:    {settings.telegram_public_channel_id or '[NOT SET]'}")
    print(f"VIP Channel:       {settings.vip_channel_id or '[NOT SET]'}")
    print(f"Admin User ID:     {settings.telegram_chat_id or '[NOT SET]'}")
    print("-" * 75)

    all_passed = True

    async with httpx.AsyncClient(timeout=10.0) as client:
        # 1. getMe
        print("\n[1/3] Verifying Bot Identity via getMe...")
        try:
            resp = await client.get(f"{base_url}/getMe")
            if resp.status_code == 200:
                data = resp.json().get("result", {})
                bot_id = data.get("id")
                username = data.get("username")
                first_name = data.get("first_name")
                print(f"  [PASS] Bot Connected: @{username} (ID: {bot_id}, Name: {first_name})")
            else:
                print(f"  [FAIL] getMe returned HTTP {resp.status_code}: {resp.text}")
                return False
        except Exception as e:
            print(f"  [FAIL] Network connection error on getMe: {e}")
            return False

        # 2. Check Public Channel
        pub_channel = settings.telegram_public_channel_id
        if pub_channel:
            print(f"\n[2/3] Verifying Public Channel ({pub_channel})...")
            try:
                c_resp = await client.get(f"{base_url}/getChat", params={"chat_id": pub_channel})
                if c_resp.status_code == 200:
                    chat_data = c_resp.json().get("result", {})
                    chat_id = chat_data.get("id")
                    title = chat_data.get("title", "")
                    chat_type = chat_data.get("type", "")
                    print(f"  [PASS] Channel Found: '{title}' (ID: {chat_id}, Type: {chat_type})")

                    # Check member status for bot
                    m_resp = await client.get(
                        f"{base_url}/getChatMember",
                        params={"chat_id": pub_channel, "user_id": bot_id},
                    )
                    if m_resp.status_code == 200:
                        member_data = m_resp.json().get("result", {})
                        status = member_data.get("status")
                        can_post = member_data.get("can_post_messages", False)
                        print(f"  [PASS] Bot Role: '{status}' | can_post_messages: {can_post}")
                        if status != "administrator" or not can_post:
                            print("  [WARN] Bot is not an administrator with posting rights in public channel!")
                            all_passed = False
                    else:
                        print(f"  [FAIL] getChatMember error: HTTP {m_resp.status_code} - {m_resp.text}")
                        all_passed = False
                else:
                    print(f"  [FAIL] getChat error for {pub_channel}: HTTP {c_resp.status_code} - {c_resp.text}")
                    all_passed = False
            except Exception as e:
                print(f"  [FAIL] Exception checking public channel: {e}")
                all_passed = False
        else:
            print("\n[2/3] Public Channel: Skipped (TELEGRAM_CHANNEL_ID not set)")

        # 3. Check VIP Channel
        vip_channel = settings.vip_channel_id
        if vip_channel:
            print(f"\n[3/3] Verifying VIP Channel ({vip_channel})...")
            try:
                c_resp = await client.get(f"{base_url}/getChat", params={"chat_id": vip_channel})
                if c_resp.status_code == 200:
                    chat_data = c_resp.json().get("result", {})
                    chat_id = chat_data.get("id")
                    title = chat_data.get("title", "")
                    chat_type = chat_data.get("type", "")
                    print(f"  [PASS] VIP Channel Found: '{title}' (ID: {chat_id}, Type: {chat_type})")

                    m_resp = await client.get(
                        f"{base_url}/getChatMember",
                        params={"chat_id": vip_channel, "user_id": bot_id},
                    )
                    if m_resp.status_code == 200:
                        member_data = m_resp.json().get("result", {})
                        status = member_data.get("status")
                        can_post = member_data.get("can_post_messages", False)
                        can_invite = member_data.get("can_invite_users", False)
                        print(f"  [PASS] Bot Role: '{status}' | can_post: {can_post} | can_invite: {can_invite}")
                        if status != "administrator" or not can_post:
                            print("  [WARN] Bot is not an administrator with posting rights in VIP channel!")
                            all_passed = False
                    else:
                        print(f"  [FAIL] getChatMember error for VIP: HTTP {m_resp.status_code} - {m_resp.text}")
                        all_passed = False
                else:
                    print(f"  [FAIL] getChat error for VIP {vip_channel}: HTTP {c_resp.status_code} - {c_resp.text}")
                    all_passed = False
            except Exception as e:
                print(f"  [FAIL] Exception checking VIP channel: {e}")
                all_passed = False
        else:
            print("\n[3/3] VIP Channel: Skipped (TELEGRAM_VIP_CHANNEL_ID not set)")

    print("-" * 75)
    if all_passed:
        print("[PREFLIGHT RESULT] ALL CHECKS PASSED. Bot credentials & rights are verified.")
    else:
        print("[PREFLIGHT RESULT] SOME CHECKS FAILED OR WARNED. Review configuration above.")
    print("=" * 75)
    return all_passed


if __name__ == "__main__":
    passed = asyncio.run(run_preflight())
    sys.exit(0 if passed else 1)
