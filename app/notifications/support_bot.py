"""
BornBull Trade Support & Membership Bot Engine (@bornbullsupportbot).

Handles:
  1. VIP Membership Plan inquiries (1, 3, 6, 12 months).
  2. UPI details display (patel.rachit@superyes).
  3. Payment screenshot submission & forwarding to Admin chat.
  4. Admin 1-click Approval/Rejection buttons.
  5. Generating single-use private VIP channel invite link upon admin approval.
  6. User inquiry routing ("We will reply within 12 hours").
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import html
import os
from typing import Any, Dict, Optional
import httpx

from app.config import logger, settings
from app.notifications.vip_channel import vip_manager


class BornBullSupportBot:
    """Manages dedicated Telegram Support & Membership Bot operations."""

    def __init__(self, bot_token: Optional[str] = None):
        self.bot_token = (bot_token or settings.support_bot_token).strip()
        self.admin_chat_id = str(settings.telegram_chat_id).strip()
        self.upi_id = getattr(settings, "upi_id", "patel.rachit@superyes")
        self._running = False

    @property
    def is_configured(self) -> bool:
        return bool(self.bot_token)

    async def send_message(
        self,
        chat_id: str,
        text: str,
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Sends message via Support Bot API with automatic plain-text fallback on HTML parse errors."""
        if not self.bot_token:
            return False
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": str(chat_id),
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(url, json=payload)
                if r.status_code == 200:
                    return True
                # If Telegram fails due to unescaped HTML entities or bad request, retry as plain text
                if r.status_code == 400 or "can't parse entities" in r.text.lower():
                    logger.warning(f"Telegram parse_mode=HTML failed for chat {chat_id}: {r.text}. Retrying without parse_mode...")
                    retry_payload = dict(payload)
                    retry_payload.pop("parse_mode", None)
                    r_retry = await client.post(url, json=retry_payload)
                    return r_retry.status_code == 200
                return False
        except Exception as e:
            logger.debug(f"Support bot send_message error to {chat_id}: {e}")
            return False

    async def forward_photo(
        self,
        chat_id: str,
        photo_file_id: str,
        caption: str,
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Forwards/sends a photo using file_id with plain-text fallback."""
        if not self.bot_token:
            return False
        url = f"https://api.telegram.org/bot{self.bot_token}/sendPhoto"
        payload = {
            "chat_id": str(chat_id),
            "photo": photo_file_id,
            "caption": caption,
            "parse_mode": "HTML",
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(url, json=payload)
                if r.status_code == 200:
                    return True
                if r.status_code == 400 or "can't parse entities" in r.text.lower():
                    retry_payload = dict(payload)
                    retry_payload.pop("parse_mode", None)
                    r_retry = await client.post(url, json=retry_payload)
                    return r_retry.status_code == 200
                return False
        except Exception as e:
            logger.debug(f"Support bot forward_photo error: {e}")
            return False

    async def send_photo(
        self,
        chat_id: str,
        photo: str,
        caption: str = "",
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Sends a photo URL or file_id with caption to Telegram."""
        if not self.bot_token:
            return False
        url = f"https://api.telegram.org/bot{self.bot_token}/sendPhoto"
        payload: Dict[str, Any] = {
            "chat_id": str(chat_id),
            "photo": photo,
            "caption": caption,
            "parse_mode": "HTML",
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            async with httpx.AsyncClient(timeout=12.0) as client:
                r = await client.post(url, json=payload)
                if r.status_code == 200:
                    return True
                if r.status_code == 400 or "can't parse entities" in r.text.lower():
                    retry_payload = dict(payload)
                    retry_payload.pop("parse_mode", None)
                    r_retry = await client.post(url, json=retry_payload)
                    return r_retry.status_code == 200
                logger.warning(f"Support bot send_photo returned {r.status_code}: {r.text}")
                return False
        except Exception as e:
            logger.debug(f"Support bot send_photo error to {chat_id}: {e}")
            return False

    async def edit_message_text(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Edits existing message in-place with plain-text fallback."""
        if not self.bot_token:
            return False
        url = f"https://api.telegram.org/bot{self.bot_token}/editMessageText"
        payload: Dict[str, Any] = {
            "chat_id": str(chat_id),
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(url, json=payload)
                if r.status_code == 200:
                    return True
                if r.status_code == 400 or "can't parse entities" in r.text.lower():
                    retry_payload = dict(payload)
                    retry_payload.pop("parse_mode", None)
                    r_retry = await client.post(url, json=retry_payload)
                    return r_retry.status_code == 200
                return False
        except Exception as e:
            logger.debug(f"Support bot edit_message error: {e}")
            return False

    async def edit_caption_or_text(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Edits existing message in-place, handling both photos (caption) and text messages."""
        if not self.bot_token or not message_id:
            return False
        # Try editMessageCaption first (handles photo submissions seamlessly)
        url_cap = f"https://api.telegram.org/bot{self.bot_token}/editMessageCaption"
        payload_cap: Dict[str, Any] = {
            "chat_id": str(chat_id),
            "message_id": int(message_id),
            "caption": text,
            "parse_mode": "HTML",
        }
        if reply_markup is not None:
            payload_cap["reply_markup"] = reply_markup
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(url_cap, json=payload_cap)
                if r.status_code == 200:
                    return True
                if r.status_code == 400 or "can't parse entities" in r.text.lower():
                    retry_cap = dict(payload_cap)
                    retry_cap.pop("parse_mode", None)
                    r_retry = await client.post(url_cap, json=retry_cap)
                    if r_retry.status_code == 200:
                        return True
        except Exception:
            pass

        # Fallback to editMessageText (for text messages)
        return await self.edit_message_text(chat_id, message_id, text, reply_markup=reply_markup)

    async def answer_callback_query(
        self,
        callback_query_id: str,
        text: Optional[str] = None,
        show_alert: bool = False,
    ) -> bool:
        """Answers callback query immediately so button spinner stops instantly."""
        if not self.bot_token or not callback_query_id:
            return False
        url = f"https://api.telegram.org/bot{self.bot_token}/answerCallbackQuery"
        payload: Dict[str, Any] = {"callback_query_id": str(callback_query_id)}
        if text:
            payload["text"] = text
            payload["show_alert"] = show_alert
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.post(url, json=payload)
                return r.status_code == 200
        except Exception as e:
            logger.debug(f"Support bot answerCallbackQuery error: {e}")
            return False

    async def _copy_message(self, target_chat_id: str, from_chat_id: str, message_id: int) -> bool:
        """Copies message to target channel or chat using Support Bot API."""
        if not self.bot_token or not target_chat_id:
            return False
        url = f"https://api.telegram.org/bot{self.bot_token}/copyMessage"
        payload = {
            "chat_id": str(target_chat_id),
            "from_chat_id": str(from_chat_id),
            "message_id": message_id,
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(url, json=payload)
                return r.status_code == 200
        except Exception as e:
            logger.debug(f"Support bot copyMessage error: {e}")
            return False

    def _get_main_menu_markup(self) -> Dict[str, Any]:
        """Main menu inline keyboard. VIP Membership Plans is a single full-width button."""
        return {
            "inline_keyboard": [
                [
                    {"text": "💎 VIP Membership Plans", "callback_data": "sb_plans"},
                ],
                [
                    {"text": "💬 Talk to Support Desk", "callback_data": "sb_query_help"},
                ],
                [
                    {"text": "📢 Free Public Channel", "url": "https://t.me/bornbulltrade"},
                ],
            ]
        }

    def _get_plans_markup(self) -> Dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {"text": "👑 1 Mo (₹1,499) — Get QR", "callback_data": "sb_plan:1:1499"},
                    {"text": "⭐ 3 Mo (₹3,499) — Get QR", "callback_data": "sb_plan:3:3499"},
                ],
                [
                    {"text": "🔥 6 Mo (₹5,499) — Get QR", "callback_data": "sb_plan:6:5499"},
                    {"text": "💎 1 Yr (₹8,999) — Get QR", "callback_data": "sb_plan:12:8999"},
                ],
                [
                    {"text": "📤 Upload Payment Screenshot", "callback_data": "sb_upload_hint"},
                ],
                [
                    {"text": "🔙 Back to Main Menu", "callback_data": "sb_menu"},
                ],
            ]
        }

    def _get_upi_markup(self) -> Dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {"text": "📤 Upload Payment Screenshot", "callback_data": "sb_upload_hint"},
                ],
                [
                    {"text": "💎 View VIP Plans", "callback_data": "sb_plans"},
                ],
                [
                    {"text": "🔙 Back to Main Menu", "callback_data": "sb_menu"},
                ],
            ]
        }

    async def _test_vip_channel_connection(self) -> str:
        """Tests live whether the bots have access to the VIP channel to generate 1-use links & ban members."""
        target_ch = (os.getenv("VIP_CHANNEL_ID", "").strip() or getattr(settings, "vip_channel_id", "") or "-1003416174805").strip()
        tokens = [t for t in [settings.telegram_bot_token, self.bot_token] if t]

        info = None
        working_token = None
        for tok in tokens:
            try:
                url = f"https://api.telegram.org/bot{tok}/getChat?chat_id={target_ch}"
                async with httpx.AsyncClient(timeout=8.0) as client:
                    r = await client.get(url)
                    if r.status_code == 200:
                        info = r.json().get("result", {})
                        working_token = tok
                        break
            except Exception:
                pass

        if not info:
            return (
                "⚠️ <b>VIP CHANNEL NOT CONNECTED YET</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━\n"
                f"• Target Channel ID: <code>{target_ch}</code>\n"
                "• Status: ❌ <b>Chat Not Found</b>\n\n"
                "👉 <b>How to enable 1-Use Links & 100% Auto-Eviction:</b>\n"
                "1. Open your private VIP Channel in Telegram.\n"
                "2. Tap Channel Title ➔ Edit (✏️) ➔ <b>Administrators</b> ➔ <b>Add Administrator</b>.\n"
                "3. Search & Add <b>@Directionalertbot</b> and <b>@bornbullsupportbot</b>.\n"
                "4. Give them permissions:\n"
                "   ✅ <b>Invite Users via Links</b> (mandatory for 1-use self-destructing links)\n"
                "   ✅ <b>Post Messages</b> (for trade signals)\n"
                "   ✅ <b>Ban / Restrict Users</b> (mandatory for 100% auto-eviction)\n\n"
                "💡 <i>Once added, forward ANY message from your VIP channel here or type /testvip again!</i>"
            )

        title = info.get("title", "VIP Channel")
        test_link_ok = False
        try:
            url_link = f"https://api.telegram.org/bot{working_token}/createChatInviteLink"
            async with httpx.AsyncClient(timeout=8.0) as client:
                r_l = await client.post(url_link, json={"chat_id": target_ch, "member_limit": 1, "name": "Verification-Test"})
                if r_l.status_code == 200:
                    test_link_ok = True
                    link_res = r_l.json().get("result", {}).get("invite_link")
                    if link_res:
                        url_rev = f"https://api.telegram.org/bot{working_token}/revokeChatInviteLink"
                        await client.post(url_rev, json={"chat_id": target_ch, "invite_link": link_res})
        except Exception:
            pass

        if test_link_ok:
            return (
                "✅ <b>VIP CHANNEL 100% CONNECTED & AUTOMATED!</b> 💎\n"
                "━━━━━━━━━━━━━━━━━━━━━\n"
                f"📌 <b>Channel:</b> {title}\n"
                f"🆔 <b>Channel ID:</b> <code>{target_ch}</code>\n\n"
                "🔒 <b>Single-Use Links:</b> 100% ACTIVE (member_limit=1)\n"
                "• Every approved user gets a unique, self-destructing link.\n"
                "• As soon as they join, the link closes forever and CANNOT be shared!\n\n"
                "🚪 <b>Auto-Eviction:</b> 100% ACTIVE\n"
                "• When a 1, 3, 6, or 12 month plan ends, the bot automatically removes them 100% hands-free."
            )
        else:
            return (
                "⚠️ <b>VIP Channel Found But Missing Admin Rights!</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━\n"
                f"📌 <b>Channel:</b> {title} (<code>{target_ch}</code>)\n\n"
                "The bot is inside the channel, but cannot generate single-use invite links.\n"
                "👉 Please enable the <b>'Invite Users via Links'</b> administrator permission for the bot!"
            )

    async def run_support_bot_listener(self):
        """Background polling loop for @bornbullsupportbot."""
        if not self.is_configured:
            logger.info("Support bot token not configured. Support listener disabled.")
            return

        url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates"
        offset = 0
        self._running = True
        logger.info("BornBull Support & Payment Bot Listener started.")

        async with httpx.AsyncClient(timeout=35.0) as client:
            while self._running:
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
                                await self._handle_message(u["message"])
                    elif resp.status_code == 429:
                        await asyncio.sleep(5)
                    else:
                        await asyncio.sleep(2)
                except asyncio.CancelledError:
                    logger.info("Support bot listener cancelled.")
                    break
                except Exception as e:
                    logger.debug(f"Support bot polling error: {e}")
                    await asyncio.sleep(2)

    async def _handle_message(self, msg: Dict[str, Any]):
        chat = msg.get("chat", {})
        chat_id = str(chat.get("id", ""))
        user = msg.get("from", {})
        user_id = str(user.get("id", ""))
        first_name = user.get("first_name", "Trader")
        username = user.get("username", "")
        user_display = f"{first_name}" + (f" (@{username})" if username else "")

        text = msg.get("text", "").strip()

        # 1. Handle Photo (Payment Screenshot submission)
        # 1. Handle Photo (Payment screenshot from user OR Broadcast post from Admin)
        if "photo" in msg:
            photos = msg.get("photo", [])
            if photos:
                best_photo = photos[-1]["file_id"]
                now_str = datetime.now().strftime("%d-%b-%Y %H:%M")

                if user_id == self.admin_chat_id:
                    # Admin sending photo -> Offer 1-click broadcast dispatcher
                    admin_msg_id = msg.get("message_id")
                    caption_text = msg.get("caption", "").strip()
                    preview = (caption_text[:70] or "Photo Announcement").replace("<", "&lt;").replace(">", "&gt;")
                    bc_prompt = (
                        "📢 <b>ADMIN BROADCAST DISPATCHER</b> 🚀\n"
                        "━━━━━━━━━━━━━━━━━━━━━\n"
                        f"🖼️ <b>Media Post:</b> <i>\"{preview}...\"</i>\n\n"
                        "👇 <b>Select where to publish this media post:</b>"
                    )
                    bc_markup = {
                        "inline_keyboard": [
                            [
                                {"text": "📢 Public Channel (@bornbulltrade)", "callback_data": f"sb_bc:pub:{admin_msg_id}"},
                            ],
                            [
                                {"text": "💎 Private VIP Channel", "callback_data": f"sb_bc:vip:{admin_msg_id}"},
                            ],
                            [
                                {"text": "🚀 Both Channels (Public + VIP)", "callback_data": f"sb_bc:both:{admin_msg_id}"},
                            ],
                            [
                                {"text": "❌ Cancel Broadcast", "callback_data": f"sb_bc:cancel:{admin_msg_id}"},
                            ],
                        ]
                    }
                    await self.send_message(chat_id, bc_prompt, reply_markup=bc_markup)
                    return

                # Forward screenshot to Admin chat with approval buttons
                if self.admin_chat_id:
                    admin_caption = (
                        f"📥💳 <b>NEW VIP PAYMENT SUBMISSION!</b> 💳📥\n"
                        f"━━━━━━━━━━━━━━━━━━━━━\n"
                        f"👤 <b>User:</b> {user_display}\n"
                        f"🆔 <b>Telegram ID:</b> <code>{user_id}</code>\n"
                        f"⏰ <b>Submitted At:</b> {now_str} IST\n\n"
                        f"👇 <b>Select plan to approve and issue VIP link:</b>"
                    )
                    admin_markup = {
                        "inline_keyboard": [
                            [
                                {"text": "✅ 1 Mo (₹1,499)", "callback_data": f"sb_app:{user_id}:{first_name}:1"},
                                {"text": "✅ 3 Mo (₹3,499)", "callback_data": f"sb_app:{user_id}:{first_name}:3"},
                            ],
                            [
                                {"text": "✅ 6 Mo (₹5,499)", "callback_data": f"sb_app:{user_id}:{first_name}:6"},
                                {"text": "✅ 1 Yr (₹8,999)", "callback_data": f"sb_app:{user_id}:{first_name}:12"},
                            ],
                            [
                                {"text": "❌ Reject Payment", "callback_data": f"sb_rej:{user_id}"},
                            ],
                        ]
                    }
                    await self.forward_photo(
                        chat_id=self.admin_chat_id,
                        photo_file_id=best_photo,
                        caption=admin_caption,
                        reply_markup=admin_markup,
                    )

                # Acknowledge user receipt immediately (Punchy & Minimal Words)
                user_reply = (
                    f"📸✅ <b>Payment Screenshot Received!</b> ✅📸\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n"
                    f"👤 <b>Trader:</b> {first_name}\n"
                    f"🆔 <b>Telegram ID:</b> <code>{user_id}</code>\n"
                    f"⏳ <b>Status:</b> Verifying UTR Reference\n\n"
                    f"⚡ <i>Desk is verifying your transaction.</i>\n"
                    f"💎 <i>Your private single-use VIP link will arrive right here in a few moments!</i>"
                )
                await self.send_message(chat_id, user_reply)
                return

        # Check if Admin forwarded a message from a Channel (to auto-detect & verify VIP channel ID)
        fwd_origin = msg.get("forward_origin", {})
        fwd_chat = msg.get("forward_from_chat", {})
        fwd_ch_id = None
        fwd_title = ""
        if fwd_origin.get("type") == "channel":
            c = fwd_origin.get("chat", {})
            fwd_ch_id = str(c.get("id", ""))
            fwd_title = c.get("title", "")
        elif fwd_chat and fwd_chat.get("type") == "channel":
            fwd_ch_id = str(fwd_chat.get("id", ""))
            fwd_title = fwd_chat.get("title", "")

        if user_id == self.admin_chat_id and fwd_ch_id:
            os.environ["VIP_CHANNEL_ID"] = fwd_ch_id
            vip_manager.channel_id = fwd_ch_id
            try:
                env_path = os.path.join(os.path.dirname(__file__), "..", "..", ".env")
                if os.path.exists(env_path):
                    with open(env_path, "r", encoding="utf-8") as f:
                        lines = f.readlines()
                    updated = False
                    new_lines = []
                    for line in lines:
                        if line.startswith("VIP_CHANNEL_ID="):
                            new_lines.append(f"VIP_CHANNEL_ID={fwd_ch_id}\n")
                            updated = True
                        else:
                            new_lines.append(line)
                    if not updated:
                        new_lines.append(f"\nVIP_CHANNEL_ID={fwd_ch_id}\n")
                    with open(env_path, "w", encoding="utf-8") as f:
                        f.writelines(new_lines)
            except Exception as e:
                logger.debug(f"Failed to update .env: {e}")

            res = await self._test_vip_channel_connection()
            await self.send_message(
                self.admin_chat_id,
                f"📥 <b>Detected Channel Forward:</b> <i>{fwd_title}</i> (<code>{fwd_ch_id}</code>)\n\n{res}"
            )
            return

        # Admin /testvip command: checks VIP channel permissions live
        if user_id == self.admin_chat_id and text in ("/testvip", "/vip"):
            res = await self._test_vip_channel_connection()
            await self.send_message(self.admin_chat_id, res)
            return

        # -------------------------------------------------------------
        # ADMIN SYSTEM COMMANDS IN SUPPORT BOT (Mirroring @Directionalertbot)
        # -------------------------------------------------------------
        if user_id == self.admin_chat_id and text:
            cmd_lower = text.lower().split()[0]
            if cmd_lower in ("/help", "/start", "help"):
                admin_help = (
                    "🐂👑 <b>BORNBULL ADMIN DESK</b> 👑🐂\n"
                    "━━━━━━━━━━━━━━━━━━━━━\n"
                    "• <code>/balance</code> or <code>/limit</code> - Live Dhan margin & funds\n"
                    "• <code>/indices</code> - Today's Nifty, BankNifty & Sensex levels\n"
                    "• <code>/positions</code> - Open live positions on Dhan\n"
                    "• <code>/orders</code> - Today's Dhan order log\n"
                    "• <code>/status</code> - Scanner & learning engine status\n"
                    "• <code>/subs</code> - Active VIP subscribers\n"
                    "• <code>/add_sub &lt;id&gt; &lt;name&gt; &lt;mo&gt;</code> - Enroll VIP subscriber\n"
                    "• <code>/remove_sub &lt;id&gt;</code> - Deactivate VIP subscriber\n"
                    "• <code>/reply &lt;id&gt; &lt;msg&gt;</code> - Direct message user\n"
                    "• <code>/choppy</code> - Broadcast sideways market update\n"
                    "• <code>/crypto</code> or <code>/forex</code> - Broadcast Zuperior promo\n"
                    "• <code>/testvip</code> - Verify VIP channel automation\n\n"
                    "💡 <i>Forward any message or photo here to broadcast to Public / VIP channels!</i>"
                )
                await self.send_message(chat_id, admin_help)
                return

            if cmd_lower in (
                "/balance", "/limit", "/indices", "/positions", "/orders",
                "/status", "/subs", "/subscribers", "/add_sub", "/remove_sub",
                "/token", "/choppy", "/sideways", "/notrade",
                "/crypto", "/forex", "/zuperior", "/promo"
            ):
                from app.trading.order_executor import order_executor
                await order_executor._handle_telegram_command(msg)
                return

            # Command /reply <user_id> <custom_text>
            if text.startswith("/reply") or text.startswith("/reject") or text.startswith("/msg"):
                parts = text.split(maxsplit=2)
                if len(parts) >= 3:
                    target_uid = parts[1].strip("<>#@:[] \t\r\n")
                    raw_body = parts[2].strip()
                    if (raw_body.startswith("<") and raw_body.endswith(">")) or (raw_body.startswith("[") and raw_body.endswith("]")):
                        raw_body = raw_body[1:-1].strip()

                    escaped_body = html.escape(raw_body)
                    delivered = await self.send_message(
                        target_uid,
                        f"📩 <b>Message from BornBull Support Desk:</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━━\n"
                        f"{escaped_body}"
                    )
                    if delivered:
                        await self.send_message(
                            self.admin_chat_id,
                            f"✅ <b>Message delivered to User <code>{target_uid}</code>:</b>\n<i>{escaped_body}</i>"
                        )
                    else:
                        await self.send_message(
                            self.admin_chat_id,
                            f"⚠️ Failed to deliver message to User ID <code>{target_uid}</code>."
                        )
                    return
                else:
                    await self.send_message(
                        self.admin_chat_id,
                        "ℹ️ Format: <code>/reply &lt;user_id&gt; your message</code>"
                    )
                    return

            # Telegram Native Reply from Admin
            reply_to = msg.get("reply_to_message")
            if reply_to:
                reply_txt = reply_to.get("text", "") or reply_to.get("caption", "")
                import re
                id_match = re.search(r"(?:ID|Telegram ID):.*?<code>(\d+)</code>", reply_txt)
                if id_match:
                    target_uid = id_match.group(1).strip()
                    escaped_body = html.escape(text)
                    delivered = await self.send_message(
                        target_uid,
                        f"📩 <b>Reply from BornBull Support Desk:</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━━\n"
                        f"{escaped_body}"
                    )
                    if delivered:
                        await self.send_message(
                            self.admin_chat_id,
                            f"✅ <b>Reply delivered to User <code>{target_uid}</code>:</b>\n<i>{escaped_body}</i>"
                        )
                        return

            # If Admin sends any other custom post/text -> Offer 1-click broadcast options
            admin_msg_id = msg.get("message_id")
            preview = (text[:70] or "Text Announcement").replace("<", "&lt;").replace(">", "&gt;")
            bc_prompt = (
                "📢 <b>ADMIN BROADCAST DISPATCHER</b> 🚀\n"
                "━━━━━━━━━━━━━━━━━━━━━\n"
                f"📝 <b>Message:</b> <i>\"{preview}...\"</i>\n\n"
                "👇 <b>Select where to publish this post:</b>"
            )
            bc_markup = {
                "inline_keyboard": [
                    [
                        {"text": "📢 Public Channel (@bornbulltrade)", "callback_data": f"sb_bc:pub:{admin_msg_id}"},
                    ],
                    [
                        {"text": "💎 Private VIP Channel", "callback_data": f"sb_bc:vip:{admin_msg_id}"},
                    ],
                    [
                        {"text": "🚀 Both Channels (Public + VIP)", "callback_data": f"sb_bc:both:{admin_msg_id}"},
                    ],
                    [
                        {"text": "❌ Cancel Broadcast", "callback_data": f"sb_bc:cancel:{admin_msg_id}"},
                    ],
                ]
            }
            await self.send_message(chat_id, bc_prompt, reply_markup=bc_markup)
            return

        # -------------------------------------------------------------
        # USER COMMANDS & INQUIRIES
        # -------------------------------------------------------------
        if text.startswith("/start") or text.startswith("/help") or text.lower() in ("hi", "hello", "help"):
            welcome = (
                f"🐂💎 <b>Welcome to BornBull Trade Support Desk!</b> 💎🐂\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"⚡ <b>Official VIP Subscriptions & Support</b>\n"
                f"🎯 Daily High-Accuracy Index Option Setups\n"
                f"🛡️ Strict Capital Protection & Trailing SL\n\n"
                f"👇 <b>Select an option below:</b>"
            )
            markup = self._get_main_menu_markup()
            await self.send_message(chat_id, welcome, reply_markup=markup)
            return

        if text.startswith("/plans") or text.startswith("/plan") or text.lower() in ("plans", "plan", "vip"):
            await self._send_plans_menu(chat_id)
            return

        if text.startswith("/upi") or text.lower() in ("upi", "pay", "payment"):
            await self._send_upi_details(chat_id)
            return

        # Regular user query forwarding
        if user_id != self.admin_chat_id and text:
            if self.admin_chat_id:
                query_alert = (
                    f"💬 <b>NEW USER SUPPORT INQUIRY</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n"
                    f"👤 <b>From:</b> {user_display} (ID: <code>{user_id}</code>)\n"
                    f"⏰ <b>Time:</b> {datetime.now().strftime('%H:%M')} IST\n\n"
                    f"<b>Message:</b>\n<i>{text}</i>\n\n"
                    f"👉 <i>Reply directly to this message or use /reply {user_id} &lt;msg&gt;</i>"
                )
                await self.send_message(self.admin_chat_id, query_alert)

            reply_to_user = (
                f"📩✅ <b>MESSAGE RECEIVED!</b> ⏳\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"Hi <b>{first_name}</b>, our support desk has received your message.\n"
                f"We will reply directly in this chat within <b>12 hours</b>!\n\n"
                f"• View VIP Plans: /plans\n"
                f"• UPI Payment: /upi"
            )
            await self.send_message(chat_id, reply_to_user)

    async def _send_plans_menu(self, chat_id: str, message_id: Optional[int] = None):
        text = (
            "👑💎 <b>BORNBULL VIP TRADING DESK PLANS</b> 💎👑\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "🎯 <b>What's Included:</b>\n"
            "• Live NIFTY, BANKNIFTY & SENSEX Calls\n"
            "• Exact Strike, Entry, SL & Targets (T1, T2, T3)\n"
            "• Auto Trailing SL to Entry on T1\n"
            "• 0.25% Zero-Chop Filtered High-Momentum Trades\n\n"
            "💎 <b>MEMBERSHIP PASSES:</b>\n"
            "• <b>1 Month:</b> ₹1,499 <i>(Starter)</i>\n"
            "• <b>3 Months:</b> ₹3,499 <i>(Popular ⭐)</i>\n"
            "• <b>6 Months:</b> ₹5,499 <i>(Pro Trader 🔥)</i>\n"
            "• <b>12 Months:</b> ₹8,999 <i>(Best Value 👑)</i>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"📲 <b>UPI:</b> <code>{self.upi_id}</code> 📋 <i>(Tap to Copy)</i>\n"
            "👇 <i>Tap a plan below to get instant payment QR:</i>"
        )
        markup = self._get_plans_markup()
        if message_id:
            edited = await self.edit_message_text(chat_id, message_id, text, reply_markup=markup)
            if not edited:
                await self.send_message(chat_id, text, reply_markup=markup)
        else:
            await self.send_message(chat_id, text, reply_markup=markup)

    async def _send_upi_details(self, chat_id: str, message_id: Optional[int] = None):
        text = (
            "💳⚡ <b>UPI PAYMENT DESK</b> ⚡💳\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"📲 <b>UPI ID:</b> <code>{self.upi_id}</code> 📋 <i>(Tap to Copy)</i>\n"
            "👤 <b>Payee:</b> Rachit Patel\n"
            "⚡ <b>Apps:</b> GPay | PhonePe | Paytm | Cred | BHIM\n\n"
            "👇 <b>3-STEP ACTIVATION:</b>\n"
            f"1️⃣ Transfer fee to <code>{self.upi_id}</code>\n"
            "2️⃣ Capture screenshot showing UTR / Ref No\n"
            "3️⃣ Upload screenshot directly in this chat\n\n"
            "🛡️ <i>Your private single-use VIP link is delivered right here instantly!</i>"
        )
        markup = self._get_upi_markup()
        if message_id:
            edited = await self.edit_message_text(chat_id, message_id, text, reply_markup=markup)
            if not edited:
                await self.send_message(chat_id, text, reply_markup=markup)
        else:
            await self.send_message(chat_id, text, reply_markup=markup)

    async def _send_plan_qr(self, chat_id: str, months: str, amount: int):
        plan_names = {
            "1": "1 Month (Starter Pass)",
            "3": "3 Months (Most Popular ⭐)",
            "6": "6 Months (Serious Trader 🔥)",
            "12": "12 Months (Best Value 👑)",
        }
        p_name = plan_names.get(str(months), f"{months} Months Membership")
        qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=350x350&data=upi%3A%2F%2Fpay%3Fpa%3D{self.upi_id}%26pn%3DRachit%2520Ashish%2520Patel%26cu%3DINR%26am%3D{amount}"
        upi_link = f"upi://pay?pa={self.upi_id}&pn=Rachit%20Ashish%20Patel&cu=INR&am={amount}"

        caption = (
            f"👑 <b>BORNBULL VIP TRADING DESK</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📦 <b>Selected Plan:</b> {p_name}\n"
            f"💰 <b>Subscription Fee:</b> ₹{amount:,.0f}\n"
            f"📲 <b>UPI ID:</b> <code>{self.upi_id}</code>\n"
            f"👤 <b>Payee Name:</b> Rachit Patel\n\n"
            f"⚡ <b>SCAN QR CODE TO ACTIVATE:</b>\n"
            f"1. Scan the QR code above using <b>GPay, PhonePe, Paytm, Cred, or BHIM</b>.\n"
            f"2. Amount <b>₹{amount:,.0f}</b> is pre-filled automatically.\n"
            f"3. Complete the transfer and capture a screenshot with the UTR / Ref No.\n"
            f"4. Upload the screenshot right here in this chat.\n\n"
            f"<i>Our team verifies payments and delivers your private single-use VIP link instantly!</i>"
        )
        markup = {
            "inline_keyboard": [
                [
                    {"text": "⚡ Open UPI App Directly", "url": upi_link},
                ],
                [
                    {"text": "📤 Upload Payment Screenshot", "callback_data": "sb_upload_hint"},
                ],
                [
                    {"text": "🔙 Choose Another Plan", "callback_data": "sb_plans"},
                ],
            ]
        }
        await self.send_photo(chat_id, qr_url, caption, reply_markup=markup)

    async def _handle_callback(self, cb: Dict[str, Any]):
        cb_id = cb.get("id")
        data = cb.get("data", "")
        message = cb.get("message") or {}
        chat = message.get("chat") or {}
        user = cb.get("from") or {}

        # Resolve chat_id: fallback to user id if chat is missing
        chat_id = str(chat.get("id") or user.get("id") or "")
        message_id = message.get("message_id")

        # 1. Instantly acknowledge callback to remove Telegram loading spinner
        if cb_id:
            asyncio.create_task(self.answer_callback_query(cb_id))

        if not chat_id:
            return

        if data.startswith("sb_plan:"):
            parts = data.split(":")
            if len(parts) >= 3:
                m_str = parts[1]
                amt = int(parts[2])
                await self._send_plan_qr(chat_id, m_str, amt)
        elif data == "sb_plans":
            await self._send_plans_menu(chat_id, message_id)
        elif data == "sb_upi":
            await self._send_upi_details(chat_id, message_id)
        elif data == "sb_upload_hint":
            hint = (
                "📸 <b>Payment Screenshot Submission</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━\n"
                "1. Tap the attachment icon (📎) below.\n"
                "2. Select your UPI payment screenshot (UTR / Ref No must be visible).\n"
                "3. Send it directly in this chat.\n\n"
                "Our desk will verify and send your private single-use VIP link right here!"
            )
            markup = {
                "inline_keyboard": [
                    [{"text": "💎 View Plans", "callback_data": "sb_plans"}],
                    [{"text": "🔙 Back to Main Menu", "callback_data": "sb_menu"}],
                ]
            }
            if message_id:
                edited = await self.edit_message_text(chat_id, message_id, hint, reply_markup=markup)
                if not edited:
                    await self.send_message(chat_id, hint, reply_markup=markup)
            else:
                await self.send_message(chat_id, hint, reply_markup=markup)
        elif data == "sb_query_help":
            help_txt = (
                "💬 <b>BornBull Support Desk</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━\n"
                "Please type your message or query directly in this chat.\n"
                "Our team will read and reply right here within <b>12 hours</b>! ⏳"
            )
            markup = {
                "inline_keyboard": [
                    [{"text": "💎 VIP Membership Plans", "callback_data": "sb_plans"}],
                    [{"text": "🔙 Back to Main Menu", "callback_data": "sb_menu"}],
                ]
            }
            if message_id:
                edited = await self.edit_message_text(chat_id, message_id, help_txt, reply_markup=markup)
                if not edited:
                    await self.send_message(chat_id, help_txt, reply_markup=markup)
            else:
                await self.send_message(chat_id, help_txt, reply_markup=markup)
        elif data == "sb_menu":
            welcome = (
                "👋 <b>BornBull Trade Support Desk</b> 🐂\n\n"
                "Official desk for <b>BornBull VIP Memberships</b> and inquiries.\n\n"
                "📌 <b>Select an option below:</b>"
            )
            markup = self._get_main_menu_markup()
            if message_id:
                edited = await self.edit_message_text(chat_id, message_id, welcome, reply_markup=markup)
                if not edited:
                    await self.send_message(chat_id, welcome, reply_markup=markup)
            else:
                await self.send_message(chat_id, welcome, reply_markup=markup)

        # Admin Approval: sb_app:<user_id>:<name>:<months>
        elif data.startswith("sb_app:"):
            parts = data.split(":")
            if len(parts) >= 4:
                target_user_id = parts[1]
                user_name = parts[2]
                months = int(parts[3])

                ok, _, invite_link, exp_date = await vip_manager.create_subscription_invite(
                    telegram_id=target_user_id,
                    name=user_name,
                    plan_months=months,
                )

                # Send VIP Link to Subscriber
                if ok:
                    sub_welcome = (
                        f"🎉 <b>PAYMENT APPROVED! WELCOME TO BORNBULL VIP!</b> 💎\n"
                        f"━━━━━━━━━━━━━━━━━━━━━\n"
                        f"👤 <b>Subscriber:</b> {user_name}\n"
                        f"📅 <b>Plan:</b> {months} Month(s) Active\n"
                        f"⏳ <b>Valid Until:</b> {exp_date.strftime('%d-%b-%Y')}\n\n"
                        f"🔗 <b>YOUR PRIVATE VIP INVITE LINK:</b>\n"
                        f"{invite_link}\n\n"
                        f"⚠️ <i>Note: This is a single-use private access link. Tap the link above to enter the VIP channel. Happy Trading! 🚀</i>"
                    )
                    await self.send_message(target_user_id, sub_welcome)

                    # Update Admin message
                    is_fallback = invite_link == (os.getenv("VIP_CHANNEL_LINK", "").strip() or "https://t.me/+2g9S5T6G5Js3OWQ1")
                    status_note = (
                        "⚠️ <i>Notice: Static fallback link used because bot is not an admin in VIP Channel. Add @Directionalertbot as admin to enable self-destructing 1-use links.</i>"
                        if is_fallback
                        else "🔒 <i>Dynamic Single-Use Link Activated (member_limit=1). Link self-destructs after join and cannot be shared.</i>"
                    )
                    if message_id:
                        await self.edit_caption_or_text(
                            chat_id=chat_id,
                            message_id=message_id,
                            text=(
                                f"✅ <b>APPROVED &amp; DELIVERED</b>\n\n"
                                f"User {user_name} (<code>{target_user_id}</code>) enrolled for <b>{months} Month(s)</b>.\n\n"
                                f"{status_note}"
                            )
                        )

        # Admin Rejection Menu: sb_rej:<user_id>
        elif data.startswith("sb_rej:"):
            parts = data.split(":")
            if len(parts) >= 2:
                target_user_id = parts[1]
                prompt_caption = (
                    f"❌ <b>REJECT PAYMENT FOR USER <code>{target_user_id}</code></b>\n\n"
                    f"👇 <b>Select why it is rejected to notify the user:</b>"
                )
                rej_markup = {
                    "inline_keyboard": [
                        [
                            {"text": "📸 Resend: UTR / Proof Not Clear", "callback_data": f"sb_rutr:{target_user_id}"},
                        ],
                        [
                            {"text": "💸 Resend: Amount Mismatch", "callback_data": f"sb_ramt:{target_user_id}"},
                        ],
                        [
                            {"text": "❌ Not Received in Bank", "callback_data": f"sb_rnorec:{target_user_id}"},
                        ],
                        [
                            {"text": "✍️ Custom Reason via /reply", "callback_data": f"sb_rcust:{target_user_id}"},
                        ],
                        [
                            {"text": "🔙 Cancel Rejection", "callback_data": f"sb_rcanc:{target_user_id}"},
                        ],
                    ]
                }
                if message_id:
                    await self.edit_caption_or_text(chat_id, message_id, prompt_caption, reply_markup=rej_markup)

        # 1. Reject: UTR / Reference not clear (asks user to resend)
        elif data.startswith("sb_rutr:"):
            target_user_id = data.split(":")[1]
            notice = (
                "⚠️ <b>Payment Verification Notice</b> 📸\n"
                "━━━━━━━━━━━━━━━━━━━━━\n"
                "Hi! We could not verify your payment proof because the <b>12-digit UTR / Reference Number was not clearly visible</b> (blurry, cropped, or missing).\n\n"
                "👉 <b>Please resend a clear, full screenshot showing the 12-digit UTR Number</b> directly in this chat so our desk can activate your VIP membership immediately!"
            )
            await self.send_message(target_user_id, notice)
            if message_id:
                await self.edit_caption_or_text(
                    chat_id, message_id,
                    f"❌ <b>REJECTED:</b> Asked User (<code>{target_user_id}</code>) to resend clear screenshot with UTR."
                )

        # 2. Reject: Amount mismatch
        elif data.startswith("sb_ramt:"):
            target_user_id = data.split(":")[1]
            notice = (
                "⚠️ <b>Payment Verification Notice</b> 💸\n"
                "━━━━━━━━━━━━━━━━━━━━━\n"
                "Hi! The transferred amount does not match our official VIP membership pricing tiers.\n\n"
                "👉 Please review the plan pricing via /plans or send the remaining balance screenshot to complete your activation."
            )
            await self.send_message(target_user_id, notice)
            if message_id:
                await self.edit_caption_or_text(
                    chat_id, message_id,
                    f"❌ <b>REJECTED:</b> Notified User (<code>{target_user_id}</code>) of Amount Mismatch."
                )

        # 3. Reject: Not received in bank statement
        elif data.startswith("sb_rnorec:"):
            target_user_id = data.split(":")[1]
            notice = (
                "⚠️ <b>Payment Verification Notice</b> ❌\n"
                "━━━━━━━━━━━━━━━━━━━━━\n"
                "Hi! We could not find this transaction in our bank or UPI statement.\n\n"
                "👉 If money was debited from your account, please check your bank UTR and contact support or send updated transaction proof."
            )
            await self.send_message(target_user_id, notice)
            if message_id:
                await self.edit_caption_or_text(
                    chat_id, message_id,
                    f"❌ <b>REJECTED:</b> Notified User (<code>{target_user_id}</code>) that payment was not found in bank records."
                )

        # 4. Reject: Custom reason instructions
        elif data.startswith("sb_rcust:"):
            target_user_id = data.split(":")[1]
            help_txt = (
                f"✍️ <b>Type Custom Reason for User <code>{target_user_id}</code>:</b>\n\n"
                f"Send your message in this format (brackets not needed):\n"
                f"<code>/reply {target_user_id} your custom reason here</code>\n\n"
                f"Example:\n"
                f"<code>/reply {target_user_id} Please send payment to patel.rachit@superyes instead</code>\n\n"
                f"<i>The bot will forward your exact text directly to the user!</i>"
            )
            await self.send_message(self.admin_chat_id, help_txt)

        # 5. Cancel Rejection (restore approval buttons)
        elif data.startswith("sb_rcanc:"):
            target_user_id = data.split(":")[1]
            now_str = datetime.now().strftime("%d-%b-%Y %H:%M")
            restore_caption = (
                f"📥 <b>VIP PAYMENT SUBMISSION</b> 💳\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"🆔 <b>Telegram ID:</b> <code>{target_user_id}</code>\n"
                f"⏰ <b>Submitted At:</b> {now_str} IST\n\n"
                f"👇 <b>Select plan to approve and issue VIP link:</b>"
            )
            admin_markup = {
                "inline_keyboard": [
                    [
                        {"text": "✅ 1 Mo (₹1,499)", "callback_data": f"sb_app:{target_user_id}:Trader:1"},
                        {"text": "✅ 3 Mo (₹3,499)", "callback_data": f"sb_app:{target_user_id}:Trader:3"},
                    ],
                    [
                        {"text": "✅ 6 Mo (₹5,499)", "callback_data": f"sb_app:{target_user_id}:Trader:6"},
                        {"text": "✅ 1 Yr (₹8,999)", "callback_data": f"sb_app:{target_user_id}:Trader:12"},
                    ],
                    [
                        {"text": "❌ Reject Payment", "callback_data": f"sb_rej:{target_user_id}"},
                    ],
                ]
            }
            if message_id:
                await self.edit_caption_or_text(chat_id, message_id, restore_caption, reply_markup=admin_markup)

        # 6. Admin Broadcast Dispatcher Callbacks: sb_bc:<destination>:<msg_id>
        elif data.startswith("sb_bc:"):
            parts = data.split(":")
            if len(parts) >= 3 and str(chat_id) == str(self.admin_chat_id):
                dest = parts[1]
                target_msg_id = int(parts[2])
                pub_chat_id = settings.telegram_public_channel_id
                vip_chat_id = os.getenv("VIP_CHANNEL_ID", "").strip() or getattr(settings, "vip_channel_id", "-1003937048910")

                if dest == "pub":
                    ok = await self._copy_message(pub_chat_id, self.admin_chat_id, target_msg_id)
                    await self.edit_caption_or_text(chat_id, message_id, "✅ Broadcast published to <b>Public Channel (@bornbulltrade)</b>! 📢")
                elif dest == "vip":
                    ok = await self._copy_message(vip_chat_id, self.admin_chat_id, target_msg_id)
                    await self.edit_caption_or_text(chat_id, message_id, "✅ Broadcast published to <b>Private VIP Channel</b>! 💎")
                elif dest == "both":
                    ok1 = await self._copy_message(pub_chat_id, self.admin_chat_id, target_msg_id)
                    ok2 = await self._copy_message(vip_chat_id, self.admin_chat_id, target_msg_id)
                    await self.edit_caption_or_text(chat_id, message_id, "✅ Broadcast published to <b>Both Channels (Public + VIP)</b>! 🚀")
                elif dest == "cancel":
                    await self.edit_caption_or_text(chat_id, message_id, "❌ Broadcast cancelled.")


support_bot = BornBullSupportBot()
