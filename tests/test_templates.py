"""
Tests for Telegram Message Templates & Compliance (Sections 2 & 13).
"""

import pytest

from app.notifications.templates import (
    escape_html,
    render_actionable_signal,
    render_daily_bias_digest,
    render_entry_permission_update,
    render_public_teaser,
)


def test_actionable_signal_rejects_missing_numeric_stop():
    """Actionable template MUST be rejected if stop loss is zero, negative, or missing."""
    rendered = render_actionable_signal(
        paper_or_live_status="PAPER SIGNAL",
        contract_name_with_expiry_and_option_type="NIFTY 25000 CE",
        bias="BULLISH",
        condition_and_price="Buy above ₹120.00",
        stop_price=0.0,  # Missing / invalid stop
        targets_and_whole_lot_allocation="Target ₹160.00 (1:2 R:R; Full 1-lot exit)",
        quantity=75,
        lots=1,
        lot_size=75,
        risk_rupees_and_cost_assumption="₹1,500 + ₹45 brokerage/slippage allowance",
        expiry_time_ist="15:25 IST",
        factual_passed_checks="Daily direction aligned; EMA 50 passed; RVOL 2.1x",
        signal_id="SIG_123",
        short_snapshot_id="BIAS_NIFTY_123",
    )
    assert rendered is None


def test_actionable_signal_rejects_missing_quantity_or_lots():
    """Actionable template MUST be rejected if quantity or lots are invalid."""
    rendered = render_actionable_signal(
        paper_or_live_status="PAPER SIGNAL",
        contract_name_with_expiry_and_option_type="NIFTY 25000 CE",
        bias="BULLISH",
        condition_and_price="Buy above ₹120.00",
        stop_price=100.0,
        targets_and_whole_lot_allocation="Target ₹160.00",
        quantity=0,  # Invalid
        lots=0,      # Invalid
        lot_size=75,
        risk_rupees_and_cost_assumption="₹1,500",
        expiry_time_ist="15:25 IST",
        factual_passed_checks="Daily direction aligned",
        signal_id="SIG_123",
        short_snapshot_id="BIAS_123",
    )
    assert rendered is None


def test_actionable_signal_renders_compliantly_with_whole_lot():
    """Actionable signal renders accurately with whole-lot allocation and realistic risk terms."""
    rendered = render_actionable_signal(
        paper_or_live_status="PAPER SIGNAL",
        contract_name_with_expiry_and_option_type="NIFTY 25000 CE (08-OCT-2026)",
        bias="BULLISH",
        condition_and_price="Confirmed close above ₹120.00",
        stop_price=100.0,
        targets_and_whole_lot_allocation="Target ₹160.00 (1:2 R:R; 1-lot full-position exit)",
        quantity=75,
        lots=1,
        lot_size=75,
        risk_rupees_and_cost_assumption="₹1,500 (₹20/pt max risk + ₹40 est. slippage/fees)",
        expiry_time_ist="15:25 IST",
        factual_passed_checks="Daily direction aligned; 9-EMA passed; RVOL 2.2x",
        signal_id="SIG_999",
        short_snapshot_id="BIAS_NIFTY_ABC",
    )
    assert rendered is not None
    # Verify no unsupported claims
    assert "zero-loss" not in rendered.lower()
    assert "sure-shot" not in rendered.lower()
    assert "guaranteed" not in rendered.lower()
    # Verify presence of required fields
    assert "BORNBULL | PAPER SIGNAL" in rendered
    assert "Numeric stop: <b>₹100.00</b>" in rendered
    assert "1 lot; 75 per lot" in rendered
    assert "ARMED; execution not yet confirmed" in rendered


def test_public_teaser_never_locks_stop_loss():
    """Public teaser omits entry and stop completely, never displaying paywalled stops."""
    teaser = render_public_teaser("RELIANCE", "10:05 IST")
    assert "RELIANCE" in teaser
    assert "10:05 IST" in teaser
    assert "Stop Loss:" not in teaser
    assert "🔒 VIP MEMBERS ONLY" not in teaser


def test_daily_bias_digest_matches_section_13():
    """Daily bias digest matches exact Section 13 format."""
    text = render_daily_bias_digest(
        trading_date="2026-10-06",
        published_time_ist="09:05 IST",
        gate_mode="SHADOW",
        symbol="NIFTY",
        daily_bias="BULLISH",
        previous_date="2026-10-05",
        previous_close=25050.0,
        reference_date="2026-10-01",
        reference_low=24700.0,
        reference_high=24950.0,
        plain_language_reason="Prior session closed above reference high",
        allowed_direction_or_wait="Long entries permitted upon valid ORB confirmation",
        data_as_of="2026-10-05 15:30 IST",
        short_snapshot_id="BIAS_NIFTY_1006_A1B2",
    )
    assert "BORNBULL | DAILY BIAS 2026-10-06 | 09:05 IST | SHADOW" in text
    assert "NIFTY:</b> <b>BULLISH" in text
    assert "ORB range:</b> 09:30–10:00 IST" in text
    assert "First possible 5-minute confirmation:</b> 10:05 IST" in text
    assert "Directional context only." in text
