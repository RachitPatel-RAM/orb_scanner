"""
Dry-run showcase script demonstrating factual, compliant Telegram messages
across all operational lifecycle stages:
  1. 09:05 Morning Daily Bias Digest
  2. Intraday Entry Permission Update (Liquidity Sweep Context)
  3. Actionable Setup Alert (ARMED with complete numeric stop, whole-lot sizing & rupee risk)
  4. Compliant Public Teaser (omits actionable levels without paywalling stops)
  5. Shadow Mode Blocked Candidate Record
  6. End-Of-Day Verified Reconciliation Summary
"""

from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.notifications.templates import (
    render_daily_bias_digest,
    render_entry_permission_update,
    render_actionable_signal,
    render_public_teaser,
)


def showcase_all_templates():
    print("=" * 80)
    print("      BORNBULL TELEGRAM NOTIFICATION SYSTEM — DRY RUN SHOWCASE")
    print("=" * 80)

    # 1. Morning Daily Bias Digest (09:05 AM IST)
    print("\n[STAGE 1: 09:05 AM IST — MORNING DAILY BIAS DIGEST]")
    print("-" * 80)
    digest = render_daily_bias_digest(
        trading_date="2026-10-06",
        published_time_ist="09:05 IST",
        gate_mode="SHADOW",
        symbol="NIFTY",
        daily_bias="BULLISH",
        previous_date="2026-10-05",
        previous_close=25120.50,
        reference_date="2026-10-03",
        reference_low=24950.00,
        reference_high=25080.00,
        plain_language_reason="Previous session close (25,120.50) exceeded reference high (25,080.00)",
        allowed_direction_or_wait="LONG_ONLY",
        data_as_of="2026-10-05 15:30 IST",
        short_snapshot_id="BIAS_NIFTY_20261006_8a12b4",
    )
    print(digest)

    # 2. Intraday Entry Permission Update (Sweep Context)
    print("\n" + "=" * 80)
    print("[STAGE 2: 11:15 AM IST — INTRADAY ENTRY PERMISSION UPDATE]")
    print("-" * 80)
    perm_update = render_entry_permission_update(
        symbol="NIFTY",
        event_time_ist="11:15 IST",
        unchanged_daily_bias="BULLISH",
        context_and_level="Confirmed Bearish Swing Sweep at ₹25,240.00",
        paused_or_resumed="PAUSED",
        plain_language_reason="Bearish 60m sweep conflicts with bullish daily bias; new longs paused until sweep invalidated",
        event_id="CTX_SWEEP_NIFTY_111500_c9e1",
    )
    print(perm_update)

    # 3. Actionable Setup Alert (ARMED)
    print("\n" + "=" * 80)
    print("[STAGE 3: 10:05 AM IST — ARMED ACTIONABLE SETUP (VIP / ADMIN)]")
    print("-" * 80)
    actionable = render_actionable_signal(
        paper_or_live_status="PAPER SIGNAL (ARMED)",
        contract_name_with_expiry_and_option_type="NIFTY 08-OCT-2026 25100 CE",
        bias="BULLISH",
        condition_and_price="BUY ABOVE ₹142.00 (underlying breakout above 25,160.00)",
        stop_price=118.00,
        targets_and_whole_lot_allocation="T1: ₹166.00 (1:1 R), T2: ₹190.00 (1:2 R); 1 lot: full exit at T2 with BE stop at T1",
        quantity=65,
        lots=1,
        lot_size=65,
        risk_rupees_and_cost_assumption="₹1,560.00 (24 pts risk) + approx ₹40 statutory costs",
        expiry_time_ist="10:30 IST (25 mins from breakout)",
        factual_passed_checks="Daily direction aligned (BULLISH); EMA/VWAP passed; RVOL 2.3x; liquidity context NONE",
        signal_id="SIG_NIFTY_20261006_1005_CE",
        short_snapshot_id="BIAS_NIFTY_20261006_8a12b4",
    )
    print(actionable)

    # 4. Compliant Public Teaser
    print("\n" + "=" * 80)
    print("[STAGE 4: 10:05 AM IST — COMPLIANT PUBLIC TEASER (@bornbulltrade)]")
    print("-" * 80)
    teaser = render_public_teaser(
        symbol="NIFTY 50",
        event_time_ist="10:05 IST",
        setup_name="ORB-15 10:00 Breakout",
    )
    print(teaser)

    # 5. Shadow-Mode Blocked Candidate Log (Admin Chat / Internal Audit)
    print("\n" + "=" * 80)
    print("[STAGE 5: SHADOW MODE AUDIT RECORD — BLOCKED OPPOSITE CANDIDATE]")
    print("-" * 80)
    shadow_log = (
        "<b>BORNBULL | SHADOW GATE DECISION [ADMIN AUDIT]</b>\n\n"
        "• Candidate ID: <code>CAND_BANKNIFTY_20261006_1020_PE</code>\n"
        "• Symbol: BANKNIFTY | Direction: SHORT (PE)\n"
        "• Daily Bias: <b>BULLISH</b> (Prior close: 51,800 > Ref high: 51,650)\n"
        "• Candidate Gate Mode: <b>SHADOW</b>\n"
        "• Gate Decision: <b>BLOCK_OPPOSITE_DAILY</b>\n"
        "• Production Impact: <b>WOULD_BLOCK</b> (Simulated in Shadow; live order skipped)\n"
        "• Reason: Short candidate rejected on Bullish daily bias day\n"
        "• Timestamp: 2026-10-06 10:20:00 IST | Rule Version: v1.0"
    )
    print(shadow_log)

    # 6. EOD Verified Reconciliation Summary
    print("\n" + "=" * 80)
    print("[STAGE 6: 15:45 PM IST — END-OF-DAY VERIFIED RECONCILIATION SUMMARY]")
    print("-" * 80)
    eod_summary = (
        "<b>BORNBULL | EOD SESSION RECONCILIATION — 2026-10-06</b>\n\n"
        "• <b>Total Qualified ORB Setups:</b> 3\n"
        "• <b>Executed Paper Fills:</b> 1 (NIFTY 25100 CE @ ₹142.00)\n"
        "• <b>Blocked Candidates (Daily Bias Gate):</b> 2 (BANKNIFTY PE, RELIANCE PE)\n"
        "• <b>Executed Trade Outcome:</b> Target 2 Reached (+48.00 pts | ₹3,120.00 net)\n"
        "• <b>Blocked Trade Counterfactual:</b>\n"
        "   - BANKNIFTY PE: Stopped out (-35.00 pts) -> <i>Loss successfully avoided</i>\n"
        "   - RELIANCE PE: Expired untriggered\n"
        "• <b>Outstanding Exposure:</b> 0 open positions (all intraday positions squared off)\n"
        "• <b>Broker Order Status:</b> SIMULATED_PAPER (LIVE_ORDER_ENABLED=false)\n\n"
        "<i>Auditable log saved in SQLite WAL repository.</i>"
    )
    print(eod_summary)
    print("=" * 80 + "\n")


if __name__ == "__main__":
    showcase_all_templates()
