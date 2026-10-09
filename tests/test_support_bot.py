"""
Unit tests for BornBull Support & Payment Bot (@bornbullsupportbot).
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from app.notifications.support_bot import BornBullSupportBot


@pytest.fixture
def mock_support_bot():
    bot = BornBullSupportBot(bot_token="1234567890:ABCdefGHIjklMNOpqrsTUVwxyz")
    bot.admin_chat_id = "7817594155"
    return bot


@pytest.mark.asyncio
async def test_support_bot_configuration(mock_support_bot):
    assert mock_support_bot.is_configured is True
    assert mock_support_bot.admin_chat_id == "7817594155"
    assert "patel.rachit@superyes" in mock_support_bot.upi_id


@pytest.mark.asyncio
async def test_support_bot_start_command(mock_support_bot):
    msg = {
        "chat": {"id": 112233},
        "from": {"id": 112233, "first_name": "Rohan", "username": "rohan_trader"},
        "text": "/start",
    }
    with patch.object(mock_support_bot, "send_message", new_callable=AsyncMock) as mock_send:
        await mock_support_bot._handle_message(msg)
        mock_send.assert_called_once()
        args, kwargs = mock_send.call_args
        assert args[0] == "112233"
        assert "Welcome to BornBull Trade Support Desk!" in args[1]
        assert "reply_markup" in kwargs


@pytest.mark.asyncio
async def test_support_bot_photo_submission_forwards_to_admin(mock_support_bot):
    msg = {
        "chat": {"id": 112233},
        "from": {"id": 112233, "first_name": "Rohan", "username": "rohan_trader"},
        "photo": [{"file_id": "low_res"}, {"file_id": "high_res_photo_id"}],
    }
    with patch.object(mock_support_bot, "forward_photo", new_callable=AsyncMock) as mock_fwd, \
         patch.object(mock_support_bot, "send_message", new_callable=AsyncMock) as mock_send:
        await mock_support_bot._handle_message(msg)
        
        # Verify forwarded to Admin
        mock_fwd.assert_called_once()
        fwd_args, fwd_kwargs = mock_fwd.call_args
        assert fwd_kwargs["chat_id"] == "7817594155"
        assert fwd_kwargs["photo_file_id"] == "high_res_photo_id"
        assert "NEW VIP PAYMENT SUBMISSION" in fwd_kwargs["caption"]
        assert "reply_markup" in fwd_kwargs

        # Verify acknowledgment to user
        mock_send.assert_called_once()
        user_args, _ = mock_send.call_args
        assert user_args[0] == "112233"
        assert "Payment Screenshot Received!" in user_args[1]


@pytest.mark.asyncio
async def test_support_bot_text_query_routes_to_admin(mock_support_bot):
    msg = {
        "chat": {"id": 112233},
        "from": {"id": 112233, "first_name": "Rohan", "username": "rohan_trader"},
        "text": "How do you calculate the ORB benchmark range?",
    }
    with patch.object(mock_support_bot, "send_message", new_callable=AsyncMock) as mock_send:
        await mock_support_bot._handle_message(msg)
        assert mock_send.call_count == 2
        # First call: alert to admin
        call_admin = mock_send.call_args_list[0]
        assert call_admin[0][0] == "7817594155"
        assert "NEW USER SUPPORT INQUIRY" in call_admin[0][1]

        # Second call: ack to user with 12 hour timeline
        call_user = mock_send.call_args_list[1]
        assert call_user[0][0] == "112233"
        assert "12 hours" in call_user[0][1]


@pytest.mark.asyncio
async def test_support_bot_admin_approval_callback(mock_support_bot):
    cb = {
        "id": "cb_query_999",
        "data": "sb_app:112233:Rohan:3",
        "message": {"chat": {"id": 7817594155}, "message_id": 4455},
    }
    from datetime import date
    with patch("app.notifications.support_bot.vip_manager.create_subscription_invite", new_callable=AsyncMock) as mock_sub, \
         patch.object(mock_support_bot, "send_message", new_callable=AsyncMock) as mock_send, \
         patch.object(mock_support_bot, "edit_message_text", new_callable=AsyncMock) as mock_edit:
        
        mock_sub.return_value = (True, "OK", "https://t.me/+TEST_INVITE_LINK", date(2027, 1, 7))
        await mock_support_bot._handle_callback(cb)

        # Verified subscriber enrolled
        mock_sub.assert_called_once_with(telegram_id="112233", name="Rohan", plan_months=3)

        # Verified invite link delivered to user
        mock_send.assert_called_once()
        user_call = mock_send.call_args[0]
        assert user_call[0] == "112233"
        assert "https://t.me/+TEST_INVITE_LINK" in user_call[1]
        assert "PAYMENT APPROVED" in user_call[1]

        # Verified admin UI message edited to approved
        mock_edit.assert_called_once()
        edited_text = mock_edit.call_args[1].get("text") or mock_edit.call_args[0][2]
        assert "APPROVED" in edited_text


@pytest.mark.asyncio
async def test_support_bot_start_menu_single_long_plans_button(mock_support_bot):
    """Verifies VIP Membership Plans is a single long button on row 1 and Pay via UPI is removed from row 1."""
    markup = mock_support_bot._get_main_menu_markup()
    keyboard = markup["inline_keyboard"]
    # Row 1 must have exactly 1 button: 💎 VIP Membership Plans
    assert len(keyboard[0]) == 1
    assert keyboard[0][0]["text"] == "💎 VIP Membership Plans"
    assert keyboard[0][0]["callback_data"] == "sb_plans"
    # Verify Pay via UPI is NOT on the main menu
    all_texts = [btn["text"] for row in keyboard for btn in row]
    assert not any("Pay via UPI" in t for t in all_texts)


@pytest.mark.asyncio
async def test_support_bot_plans_callback_handling(mock_support_bot):
    """Verifies tapping VIP Membership Plans answers callback immediately and shows plans menu."""
    cb = {
        "id": "cb_query_101",
        "data": "sb_plans",
        "from": {"id": 998877},
        "message": {"chat": {"id": 998877}, "message_id": 5544},
    }
    with patch.object(mock_support_bot, "answer_callback_query", new_callable=AsyncMock) as mock_ans, \
         patch.object(mock_support_bot, "edit_message_text", new_callable=AsyncMock) as mock_edit:
        mock_edit.return_value = True
        await mock_support_bot._handle_callback(cb)
        await asyncio.sleep(0.05)

        # Verified callback answered
        mock_ans.assert_called_once_with("cb_query_101")
        # Verified message edited to show VIP plans
        mock_edit.assert_called_once()
        call_args = mock_edit.call_args
        assert call_args[0][0] == "998877"
        assert call_args[0][1] == 5544
        assert "BORNBULL VIP TRADING DESK PLANS" in call_args[0][2]
        assert "1 Month:" in call_args[0][2]
        assert "12 Months:" in call_args[0][2]


@pytest.mark.asyncio
async def test_support_bot_rejection_reason_dispatch(mock_support_bot):
    """Verifies selecting UTR rejection reason notifies user to resend screenshot with 12-digit UTR."""
    cb = {
        "id": "cb_query_202",
        "data": "sb_rutr:998877",
        "from": {"id": 7817594155},
        "message": {"chat": {"id": 7817594155}, "message_id": 5545},
    }
    with patch.object(mock_support_bot, "send_message", new_callable=AsyncMock) as mock_send, \
         patch.object(mock_support_bot, "edit_caption_or_text", new_callable=AsyncMock) as mock_edit:
        mock_edit.return_value = True
        await mock_support_bot._handle_callback(cb)

        # Verified notice sent to customer
        mock_send.assert_called_once()
        user_call = mock_send.call_args[0]
        assert user_call[0] == "998877"
        assert "Payment Verification Notice" in user_call[1]
        assert "12-digit UTR" in user_call[1]

        # Verified admin UI updated
        mock_edit.assert_called_once()
        assert "REJECTED" in mock_edit.call_args[0][2]


@pytest.mark.asyncio
async def test_support_bot_admin_custom_reply_command(mock_support_bot):
    """Verifies Admin can type /reply <user_id> <message> to send custom note to user."""
    msg = {
        "chat": {"id": 7817594155},
        "from": {"id": 7817594155, "first_name": "Admin"},
        "text": "/reply 998877 Please send payment from Google Pay instead of Paytm",
    }
    with patch.object(mock_support_bot, "send_message", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = True
        await mock_support_bot._handle_message(msg)

        assert mock_send.call_count == 2
        # First call: delivered to user
        user_call = mock_send.call_args_list[0][0]
        assert user_call[0] == "998877"
        assert "Please send payment from Google Pay" in user_call[1]

        # Second call: confirmation to Admin
        admin_call = mock_send.call_args_list[1][0]
        assert admin_call[0] == "7817594155"
        assert "Message delivered to User" in admin_call[1]


@pytest.mark.asyncio
async def test_support_bot_admin_reply_with_angle_brackets(mock_support_bot):
    """Verifies Admin typing /reply 5366514811 <message> strips brackets and safely delivers."""
    msg = {
        "chat": {"id": 7817594155},
        "from": {"id": 7817594155, "first_name": "Admin"},
        "text": "/reply 5366514811 <This is your link to join; it will close once it has been activated.>",
    }
    with patch.object(mock_support_bot, "send_message", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = True
        await mock_support_bot._handle_message(msg)

        assert mock_send.call_count == 2
        user_call = mock_send.call_args_list[0][0]
        assert user_call[0] == "5366514811"
        # Outer brackets stripped cleanly
        assert "This is your link to join; it will close once it has been activated." in user_call[1]
        assert "<This is your link" not in user_call[1]


@pytest.mark.asyncio
async def test_support_bot_send_message_html_fallback(mock_support_bot):
    """Verifies that if Telegram API returns 400 can't parse entities, it automatically retries without parse_mode."""
    with patch("httpx.AsyncClient.post") as mock_post:
        resp_fail = MagicMock()
        resp_fail.status_code = 400
        resp_fail.text = '{"ok":false,"error_code":400,"description":"Bad Request: can\'t parse entities: Unsupported start tag"}'

        resp_ok = MagicMock()
        resp_ok.status_code = 200

        mock_post.side_effect = [resp_fail, resp_ok]

        result = await mock_support_bot.send_message("5366514811", "<This is invalid HTML tag>")
        assert result is True
        assert mock_post.call_count == 2
        # First call has parse_mode
        first_payload = mock_post.call_args_list[0][1]["json"]
        assert first_payload.get("parse_mode") == "HTML"
        # Second call retries without parse_mode
        second_payload = mock_post.call_args_list[1][1]["json"]
        assert "parse_mode" not in second_payload


