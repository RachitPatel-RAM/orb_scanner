"""
Unit tests for Dhan 1-Click Interactive Order Execution Engine and Telegram Buttons.
"""

from datetime import date, datetime
import pytest

from app.storage.models import Direction, Signal
from app.trading.order_executor import DhanOrderExecutor


def test_telegram_approval_button_displays_whole_lot_price():
    executor = DhanOrderExecutor()
    d = date(2026, 10, 1)

    signal = Signal(
        trade_date=d,
        security_id="100",
        symbol="RELIANCE",
        timestamp=datetime(2026, 10, 1, 10, 15),
        strategy="ORB-15",
        direction=Direction.LONG,
        entry_price=1000.0,
        orb_high=990.0,
        orb_low=970.0,
        stop_loss=980.0,
        target=1040.0,
        risk_reward=2.0,
        idempotency_key="2026-10-01_100_TEST",
    )

    markup, qty, margin_req = executor.register_signal_for_approval(signal)

    # Validate quantity & margin calculation
    assert qty >= 1
    assert margin_req > 0

    # Validate Telegram inline keyboard buttons
    buttons = markup["inline_keyboard"][0]
    approve_button = buttons[0]
    reject_button = buttons[1]

    # Verify button contains exact qty and margin required
    assert f"Buy {qty} Qty" in approve_button["text"]
    assert f"₹{margin_req:,.0f}" in approve_button["text"]
    assert approve_button["callback_data"].startswith("app:")

    # Verify reject button
    assert "Reject" in reject_button["text"]
    assert reject_button["callback_data"].startswith("rej:")


def test_short_signal_displays_sell_lot_price():
    executor = DhanOrderExecutor()
    d = date(2026, 10, 1)

    signal = Signal(
        trade_date=d,
        security_id="100",
        symbol="NMDC",
        timestamp=datetime(2026, 10, 1, 10, 15),
        strategy="ORB-15",
        direction=Direction.SHORT,
        entry_price=80.0,
        orb_high=82.0,
        orb_low=80.5,
        stop_loss=81.25,
        target=77.5,
        risk_reward=2.0,
        idempotency_key="2026-10-01_100_SHORT_TEST",
    )

    markup, qty, margin_req = executor.register_signal_for_approval(signal)
    buttons = markup["inline_keyboard"][0]
    approve_button = buttons[0]

    assert f"Sell {qty} Qty" in approve_button["text"]
    assert f"₹{margin_req:,.0f}" in approve_button["text"]


@pytest.mark.asyncio
async def test_admin_broadcast_message_dispatch():
    from unittest.mock import AsyncMock, patch
    from app.config import settings

    executor = DhanOrderExecutor()
    admin_id = str(settings.telegram_chat_id)
    msg = {
        "message_id": 9999,
        "chat": {"id": int(admin_id)},
        "from": {"id": int(admin_id), "first_name": "Admin"},
        "text": "Special expiry setup for BANKNIFTY today!",
    }

    with patch("app.notifications.telegram.notifier.send_message", new_callable=AsyncMock) as mock_send:
        await executor._handle_message_command(msg)
        mock_send.assert_called_once()
        args, kwargs = mock_send.call_args
        assert "ADMIN BROADCAST DISPATCHER" in args[0]
        assert "Special expiry setup" in args[0]
        assert "reply_markup" in kwargs
        markup = kwargs["reply_markup"]["inline_keyboard"]
        assert any(b["callback_data"] == "bc:pub:9999" for row in markup for b in row)
        assert any(b["callback_data"] == "bc:vip:9999" for row in markup for b in row)
        assert any(b["callback_data"] == "bc:both:9999" for row in markup for b in row)


@pytest.mark.asyncio
async def test_admin_broadcast_callback_copy():
    from unittest.mock import AsyncMock, patch
    from app.config import settings

    executor = DhanOrderExecutor()
    admin_id = str(settings.telegram_chat_id)
    cb = {
        "id": "cb_bc_123",
        "data": "bc:pub:9999",
        "from": {"id": int(admin_id)},
        "message": {"chat": {"id": int(admin_id)}, "message_id": 8888},
    }

    with patch.object(executor, "_answer_callback", new_callable=AsyncMock) as mock_ans, \
         patch.object(executor, "_copy_message", new_callable=AsyncMock) as mock_copy, \
         patch.object(executor, "_edit_message", new_callable=AsyncMock) as mock_edit:
        mock_copy.return_value = True

        await executor._handle_callback(cb)

        mock_ans.assert_called_once()
        mock_copy.assert_called_once_with(
            settings.telegram_public_channel_id,
            admin_id,
            9999,
        )
        mock_edit.assert_called_once()
        assert "Published to Public Channel" in mock_edit.call_args[0][2]

