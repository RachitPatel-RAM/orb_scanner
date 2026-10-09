"""
Tests for Section 7: Intraday Liquidity Context Engine.
"""

from datetime import datetime, time, timedelta
import pytest
import zoneinfo

from app.analysis.liquidity_context import (
    Bar60m,
    ContextDirection,
    LiquidityContextEngine,
    PivotType,
    SweepStatus,
)

IST = zoneinfo.ZoneInfo("Asia/Kolkata")


def make_bar(
    symbol: str,
    day_offset: int,
    start_hour: int,
    start_min: int,
    open_: float,
    high: float,
    low: float,
    close: float,
) -> Bar60m:
    dt_start = datetime(2026, 10, 5 + day_offset, start_hour, start_min, tzinfo=IST)
    dt_end = dt_start + timedelta(hours=1)
    return Bar60m(
        bar_id=f"{symbol}_{dt_start.strftime('%Y%m%d%H%M')}",
        symbol=symbol,
        security_id="2885",
        start_time=dt_start,
        end_time=dt_end,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=10000.0,
    )


def test_60m_slot_validation():
    """Verify that only 6 complete 60m slots are valid, and 15:15-15:30 is excluded."""
    # Valid slot 1: 09:15
    slot1 = LiquidityContextEngine.is_valid_60m_slot(datetime(2026, 10, 5, 9, 20, tzinfo=IST))
    assert slot1 == (time(9, 15), time(10, 15))

    # Valid slot 6: 14:15
    slot6 = LiquidityContextEngine.is_valid_60m_slot(datetime(2026, 10, 5, 14, 30, tzinfo=IST))
    assert slot6 == (time(14, 15), time(15, 15))

    # Trailing 15-min fragment 15:15 - 15:30 is excluded
    slot_frag = LiquidityContextEngine.is_valid_60m_slot(datetime(2026, 10, 5, 15, 20, tzinfo=IST))
    assert slot_frag is None


def test_swing_high_pivot_confirmed_at_bar_i_plus_2():
    """Swing high requires 2 left and 2 right bars, confirmed when bar i+2 closes."""
    engine = LiquidityContextEngine()
    engine.reset_symbol("RELIANCE")

    # Bar 0: High 100
    b0 = make_bar("RELIANCE", 0, 9, 15, 95, 100, 90, 98)
    engine.add_60m_bar(b0)
    # Bar 1: High 102
    b1 = make_bar("RELIANCE", 0, 10, 15, 98, 102, 95, 101)
    engine.add_60m_bar(b1)
    # Bar 2: High 110 (Potential pivot)
    b2 = make_bar("RELIANCE", 0, 11, 15, 101, 110, 99, 105)
    engine.add_60m_bar(b2)
    # Bar 3: High 104 (1st right bar) - NOT yet confirmed
    b3 = make_bar("RELIANCE", 0, 12, 15, 105, 104, 100, 102)
    engine.add_60m_bar(b3)
    assert len(engine.pivots.get("RELIANCE", [])) == 0

    # Bar 4: High 103 (2nd right bar) - Confirmed NOW at close of Bar 4!
    b4 = make_bar("RELIANCE", 0, 13, 15, 102, 103, 98, 100)
    snap = engine.add_60m_bar(b4)

    pivots = engine.pivots.get("RELIANCE", [])
    assert len(pivots) == 1
    p = pivots[0]
    assert p.pivot_type == PivotType.SWING_HIGH
    assert p.price == 110.0
    assert p.confirmation_time == b4.end_time


def test_high_sweep_creates_bearish_context_and_extreme():
    """
    Breach above confirmed high, reclaimed strictly below within <= 3 bars
    creates BEARISH context.
    """
    engine = LiquidityContextEngine()
    engine.reset_symbol("TCS")

    # Set up 5 bars to confirm swing high at 110.0
    engine.add_60m_bar(make_bar("TCS", 0, 9, 15, 95, 100, 90, 98))
    engine.add_60m_bar(make_bar("TCS", 0, 10, 15, 98, 102, 95, 101))
    engine.add_60m_bar(make_bar("TCS", 0, 11, 15, 101, 110, 99, 105))  # Pivot High 110
    engine.add_60m_bar(make_bar("TCS", 0, 12, 15, 105, 104, 100, 102))
    engine.add_60m_bar(make_bar("TCS", 0, 13, 15, 102, 103, 98, 100))  # Confirmed at 14:15

    # Bar 5: Prior close was 100 (<= 110).
    # Bar 5 breaches to 112 (> 110) but closes at 108 (< 110). Reclaimed in 1 bar!
    b5 = make_bar("TCS", 0, 14, 15, 100, 112, 99, 108)
    snap = engine.add_60m_bar(b5)

    assert snap.direction == ContextDirection.BEARISH
    assert len(snap.active_events) == 1
    event = snap.active_events[0]
    assert event.sweep_direction == ContextDirection.BEARISH
    assert event.sweep_extreme == 112.0
    assert event.reclaim_bars == 1
    assert event.status == SweepStatus.ACTIVE


def test_low_sweep_creates_bullish_context():
    """
    Breach below confirmed low, reclaimed strictly above within <= 3 bars
    creates BULLISH context.
    """
    engine = LiquidityContextEngine()
    engine.reset_symbol("INFY")

    # Form swing low at 90.0
    engine.add_60m_bar(make_bar("INFY", 0, 9, 15, 105, 110, 100, 102))
    engine.add_60m_bar(make_bar("INFY", 0, 10, 15, 102, 104, 95, 98))
    engine.add_60m_bar(make_bar("INFY", 0, 11, 15, 98, 100, 90, 96))   # Pivot Low 90
    engine.add_60m_bar(make_bar("INFY", 0, 12, 15, 96, 98, 93, 95))
    engine.add_60m_bar(make_bar("INFY", 0, 13, 15, 95, 97, 92, 94))   # Confirmed at 14:15

    # Breach low: touches 88 (< 90), closes at 92 (> 90). Immediate reclaim.
    b5 = make_bar("INFY", 0, 14, 15, 94, 96, 88, 92)
    snap = engine.add_60m_bar(b5)

    assert snap.direction == ContextDirection.BULLISH
    assert len(snap.active_events) == 1
    assert snap.active_events[0].sweep_direction == ContextDirection.BULLISH
    assert snap.active_events[0].sweep_extreme == 88.0


def test_invalidation_when_bar_closes_beyond_sweep_extreme():
    """Bearish context is invalidated if subsequent bar closes strictly above sweep extreme."""
    engine = LiquidityContextEngine()
    engine.reset_symbol("HDFC")

    # Day 0: Create swing high at 110.0 (Pivot bar at index 2)
    engine.add_60m_bar(make_bar("HDFC", 0, 9, 15, 95, 100, 90, 98))   # index 0 (left 2)
    engine.add_60m_bar(make_bar("HDFC", 0, 10, 15, 98, 102, 95, 101)) # index 1 (left 1)
    engine.add_60m_bar(make_bar("HDFC", 0, 11, 15, 101, 110, 99, 105)) # index 2 (Pivot High 110.0)
    engine.add_60m_bar(make_bar("HDFC", 0, 12, 15, 105, 104, 100, 102)) # index 3 (right 1)
    engine.add_60m_bar(make_bar("HDFC", 0, 13, 15, 102, 103, 98, 100)) # index 4 (right 2, confirmed at 14:15)

    # Bar 5 (14:15-15:15): Breaches to 112, closes at 108 -> Bearish context confirmed at 15:15
    b_sw = make_bar("HDFC", 0, 14, 15, 100, 112, 99, 108)
    snap = engine.add_60m_bar(b_sw)
    assert snap.direction == ContextDirection.BEARISH
    assert len(snap.active_events) == 1
    assert snap.active_events[0].sweep_extreme == 112.0

    # Bar 6 (next session 09:15-10:15): Closes at 115 (> 112 extreme) -> INVALIDATED!
    # Update TTL check so check_time uses bar's end_time:
    b_inv = make_bar("HDFC", 1, 9, 15, 108, 116, 106, 115)
    snap2 = engine.add_60m_bar(b_inv)
    assert snap2.direction == ContextDirection.NONE
    assert engine.active_sweeps["HDFC"][0].status == SweepStatus.INVALIDATED


def test_candidate_fails_to_reclaim_in_3_bars_expires():
    """If breach does not reclaim within 3 bars, it expires and is NOT a sweep."""
    engine = LiquidityContextEngine()
    engine.reset_symbol("SBIN")

    engine.add_60m_bar(make_bar("SBIN", 0, 9, 15, 95, 100, 90, 98))
    engine.add_60m_bar(make_bar("SBIN", 0, 10, 15, 98, 102, 95, 101))
    engine.add_60m_bar(make_bar("SBIN", 0, 11, 15, 101, 110, 99, 105))  # High 110
    engine.add_60m_bar(make_bar("SBIN", 0, 12, 15, 105, 104, 100, 102))
    engine.add_60m_bar(make_bar("SBIN", 0, 13, 15, 102, 103, 98, 100))

    # Bar 1 of breach: closes at 112 (> 110)
    engine.add_60m_bar(make_bar("SBIN", 0, 14, 15, 100, 114, 99, 112))
    # Bar 2 of breach: closes at 113 (> 110)
    engine.add_60m_bar(make_bar("SBIN", 1, 9, 15, 112, 115, 110.5, 113))
    # Bar 3 of breach: closes at 111 (> 110) -> Fails to reclaim in 3 bars!
    snap = engine.add_60m_bar(make_bar("SBIN", 1, 10, 15, 113, 114, 109, 111))

    assert snap.direction == ContextDirection.NONE
    assert len(engine.active_sweeps.get("SBIN", [])) == 0
    assert engine._pending_high_sweep.get("SBIN") is None
