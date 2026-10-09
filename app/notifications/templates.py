"""
Standardized, Factual Telegram Message Renderers (Sections 2 & 13).

Strict compliance rules:
- No unsupported claims (zero loss, sure-shot, fixed profit, unmeasured win rates).
- Stop-to-entry renamed to break-even stop adjustment with execution risk disclosure.
- Actionable calls require numeric stop, entry condition, validity, whole-lot allocation, and risk amount.
- Rejects rendered actionable template if any required risk/stop field is missing.
- Escapes Telegram HTML formatting safely.
"""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any, Dict, Optional


def escape_html(text: Any) -> str:
    """Escapes special characters for safe Telegram HTML parsing."""
    if text is None:
        return ""
    return html.escape(str(text))


def render_daily_bias_digest(
    trading_date: str,
    published_time_ist: str,
    gate_mode: str,
    symbol: str,
    daily_bias: str,
    previous_date: str,
    previous_close: float,
    reference_date: str,
    reference_low: float,
    reference_high: float,
    plain_language_reason: str,
    allowed_direction_or_wait: str,
    data_as_of: str,
    short_snapshot_id: str,
) -> str:
    """
    Renders Section 13 Daily Bias morning digest.
    """
    return (
        f"<b>BORNBULL | DAILY BIAS {escape_html(trading_date)} | {escape_html(published_time_ist)} | {escape_html(gate_mode)}</b>\n\n"
        f"<b>{escape_html(symbol)}:</b> <b>{escape_html(daily_bias)}</b>\n"
        f"• Prior session ({escape_html(previous_date)}) close: ₹{previous_close:,.2f}\n"
        f"• Reference session ({escape_html(reference_date)}) range: ₹{reference_low:,.2f} – ₹{reference_high:,.2f}\n"
        f"• Reason: {escape_html(plain_language_reason)}\n"
        f"• Entry permission: <b>{escape_html(allowed_direction_or_wait)}</b>\n\n"
        f"⏳ <b>ORB range:</b> 09:30–10:00 IST\n"
        f"⚡ <b>First possible 5-minute confirmation:</b> 10:05 IST\n\n"
        f"<i>Directional context only. Entry still requires the full setup and risk checks.</i>\n"
        f"Data as of: {escape_html(data_as_of)} | Ref: <code>{escape_html(short_snapshot_id)}</code>"
    )


def render_entry_permission_update(
    symbol: str,
    event_time_ist: str,
    unchanged_daily_bias: str,
    context_and_level: str,
    paused_or_resumed: str,
    plain_language_reason: str,
    event_id: str,
) -> str:
    """
    Renders Section 13 Entry Permission Update notice.
    """
    return (
        f"<b>BORNBULL | ENTRY PERMISSION UPDATE</b>\n"
        f"<b>{escape_html(symbol)}</b> | {escape_html(event_time_ist)}\n\n"
        f"• Morning daily bias: <b>{escape_html(unchanged_daily_bias)}</b>\n"
        f"• Intraday context: {escape_html(context_and_level)}\n"
        f"• New entries: <b>{escape_html(paused_or_resumed)}</b>\n"
        f"• Reason: {escape_html(plain_language_reason)}\n\n"
        f"<i>Existing positions continue under their declared stop/exit rules.</i>\n"
        f"Reference: <code>{escape_html(event_id)}</code>"
    )


def render_actionable_signal(
    paper_or_live_status: str,
    contract_name_with_expiry_and_option_type: str,
    bias: str,
    condition_and_price: str,
    stop_price: float,
    targets_and_whole_lot_allocation: str,
    quantity: int,
    lots: int,
    lot_size: int,
    risk_rupees_and_cost_assumption: str,
    expiry_time_ist: str,
    factual_passed_checks: str,
    signal_id: str,
    short_snapshot_id: str,
) -> Optional[str]:
    """
    Renders Section 13 Actionable Signal template.
    Strictly validates required numeric stop, quantity, and risk fields.
    Returns None if any required field is missing or invalid.
    """
    # Validation checks
    if not stop_price or stop_price <= 0:
        return None
    if not quantity or quantity <= 0:
        return None
    if not lots or lots <= 0 or not lot_size or lot_size <= 0:
        return None
    if not contract_name_with_expiry_and_option_type or not condition_and_price:
        return None
    if not risk_rupees_and_cost_assumption or not expiry_time_ist:
        return None

    return (
        f"<b>BORNBULL | {escape_html(paper_or_live_status)}</b>\n"
        f"<b>{escape_html(contract_name_with_expiry_and_option_type)}</b>\n\n"
        f"• Underlying daily bias: <b>{escape_html(bias)}</b>\n"
        f"• Entry condition: <b>{escape_html(condition_and_price)}</b>\n"
        f"• Numeric stop: <b>₹{stop_price:,.2f}</b> (Execution risk: slippage, fees & spread apply)\n"
        f"• Targets / exit policy: {escape_html(targets_and_whole_lot_allocation)}\n"
        f"• Quantity: {quantity} ({lots} lot{'s' if lots > 1 else ''}; {lot_size} per lot)\n"
        f"• Planned risk: <b>{escape_html(risk_rupees_and_cost_assumption)}</b>\n"
        f"• Valid until: {escape_html(expiry_time_ist)}\n"
        f"• Checks: {escape_html(factual_passed_checks)}\n\n"
        f"📌 <b>Status:</b> ARMED; execution not yet confirmed\n"
        f"Signal: <code>{escape_html(signal_id)}</code> | Bias: <code>{escape_html(short_snapshot_id)}</code>"
    )


def render_public_teaser(
    symbol: str,
    event_time_ist: str,
    setup_name: str = "High-Momentum Breakout",
) -> str:
    """
    Renders a punchy, high-FOMO public teaser for Trade 2+ with expressive emojis
    inviting users to join VIP desk.
    """
    import os
    bot_user = os.getenv("SUPPORT_BOT_USERNAME", "bornbullsupportbot").lstrip("@")
    return (
        f"🚨🔥 <b>NEW LIVE SETUP DETECTED</b> 🔥🚨\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"💎 <b>Index:</b> <code>{escape_html(symbol)}</code>\n"
        f"⏰ <b>Time:</b> {escape_html(event_time_ist)}\n"
        f"⚡ <b>Status:</b> Firing Now in VIP!\n\n"
        f"👀 <i>Trade 1 was FREE on Public!</i>\n"
        f"👑 <i>Trades 2, 3 + Live Trailing SL are exclusive to VIP members.</i>\n\n"
        f"🚀 <b>Join VIP Desk Instantly:</b>\n"
        f"👉 Message @{bot_user} or send /start to unlock VIP access! 💎"
    )
