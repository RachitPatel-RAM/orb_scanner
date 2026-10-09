"""
Unit Tests for Traditional Daily Pivot Points Engine & Milestone Trailing Exits.
"""

from datetime import date, datetime
import pytest

from app.analysis.pivot_points import (
    calculate_traditional_pivots,
    pivot_engine,
)
from app.market.session import IST_TZ
from app.storage.models import Candle, Direction, ExitReason, Signal
from app.trading.paper_tracker import PaperTracker


def test_traditional_pivot_formula_exact_match():
    """
    Validates Traditional Floor Pivot Points formula matches TradingView:
      P = (H + L + C) / 3
      R1 = 2P - L, S1 = 2P - H
      R2 = P + (H - L), S2 = P - (H - L)
      R3 = H + 2(P - L), S3 = L - 2(H - P)
    """
    # Sample from real NIFTY chart: High 22546.35, Low 22375.00, Close 22431.65
    high = 22546.35
    low = 22375.00
    close = 22431.65

    pts = calculate_traditional_pivots(high, low, close)

    expected_p = round((high + low + close) / 3.0, 2)
    expected_r1 = round(2.0 * expected_p - low, 2)
    expected_s1 = round(2.0 * expected_p - high, 2)
    expected_r2 = round(expected_p + (high - low), 2)
    expected_s2 = round(expected_p - (high - low), 2)
    expected_r3 = round(high + 2.0 * (expected_p - low), 2)
    expected_s3 = round(low - 2.0 * (high - expected_p), 2)

    assert pts.pivot == expected_p
    assert pts.r1 == expected_r1
    assert pts.s1 == expected_s1
    assert pts.r2 == expected_r2
    assert pts.s2 == expected_s2
    assert pts.r3 == expected_r3


def test_pivot_trap_rejection():
    """
    Trades that slam directly into pivot levels (less than min threshold room)
    must be blocked by the Pivot Trap Filter.
    """
    pts = calculate_traditional_pivots(high=22500.0, low=22300.0, close=22400.0)
    # S1 is 2P - H = 2(22400) - 22500 = 22300

    # Short breakout where price closed right at 22305 (only 5 pts above S1)
    is_ok, reason, sl, t1, t2, t3 = pivot_engine.determine_targets_and_sl(
        symbol="NIFTY",
        direction=Direction.SHORT,
        entry_price=22305.0,
        orb_high=22450.0,
        orb_low=22380.0,
        orb_mid=22415.0,
        pivots=pts,
    )
    assert not is_ok
    assert "Pivot Support Trap" in reason


def test_milestone_target1_and_target2_trailing_stop():
    """
    Validates that:
    1. Reaching Target 1 triggers milestone callback and moves Stop Loss to Entry (Break-even).
    2. Reaching Target 2 triggers milestone callback and trails Stop Loss to Target 1.
    3. Reaching Final Target (Target 3) completes trade.
    """
    milestone_events = []
    target_hits = []

    def on_milestone(trade, m_num, price):
        milestone_events.append((m_num, price, trade.stop_loss))

    tracker = PaperTracker(
        on_milestone_hit=on_milestone,
        on_target_hit=lambda t: target_hits.append(t),
    )

    sig = Signal(
        trade_date=date(2026, 10, 8),
        security_id="13",
        symbol="NIFTY",
        strategy="ORB-5m Traditional Pivots",
        direction=Direction.SHORT,
        timestamp=datetime(2026, 10, 8, 10, 5, 0, tzinfo=IST_TZ),
        entry_price=22400.0,
        orb_high=22480.0,
        orb_low=22420.0,
        stop_loss=22450.0,
        target=22320.0,
        risk_reward=1.6,
        idempotency_key="nifty_short_1",
        target_1=22350.0,  # S1
        target_2=22280.0,  # S2
        target_3=22200.0,  # S3
    )

    trade = tracker.open_trade_from_signal(sig)
    assert trade.stop_loss == 22450.0
    assert not trade.target_1_hit

    # Candle 1: Breaches Target 1 (Low reaches 22340)
    c1 = Candle(
        security_id="13",
        symbol="NIFTY",
        timestamp=datetime(2026, 10, 8, 10, 15, 0, tzinfo=IST_TZ),
        open=22390.0,
        high=22410.0,
        low=22340.0,
        close=22360.0,
        volume=1000.0,
    )
    closed = tracker.update_with_candle(c1)
    assert len(closed) == 0  # Trade remains open
    assert trade.target_1_hit
    # Stop loss moved to Entry (Break-even: 22400.0)
    assert trade.stop_loss == 22400.0
    assert len(milestone_events) == 1
    assert milestone_events[0][0] == 1

    # Candle 2: Breaches Target 2 (Low reaches 22270)
    c2 = Candle(
        security_id="13",
        symbol="NIFTY",
        timestamp=datetime(2026, 10, 8, 10, 30, 0, tzinfo=IST_TZ),
        open=22350.0,
        high=22360.0,
        low=22270.0,
        close=22290.0,
        volume=1000.0,
    )
    closed = tracker.update_with_candle(c2)
    assert len(closed) == 0
    assert trade.target_2_hit
    # Stop loss trailed to Target 1 (22350.0)
    assert trade.stop_loss == 22350.0
    assert len(milestone_events) == 2
    assert milestone_events[1][0] == 2

    # Candle 3: Hits Target 3 (Low reaches 22190)
    c3 = Candle(
        security_id="13",
        symbol="NIFTY",
        timestamp=datetime(2026, 10, 8, 11, 0, 0, tzinfo=IST_TZ),
        open=22280.0,
        high=22290.0,
        low=22190.0,
        close=22210.0,
        volume=1000.0,
    )
    closed = tracker.update_with_candle(c3)
    assert len(closed) == 1
    assert closed[0].exit_reason == ExitReason.TARGET
    assert closed[0].exit_price == 22200.0
    assert len(target_hits) == 1
