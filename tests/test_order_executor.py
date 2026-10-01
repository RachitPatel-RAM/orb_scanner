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
