"""
Tests for Section 6: Deterministic Daily Bias Engine.
"""

from datetime import date
import pytest

from app.analysis.daily_bias import (
    BiasDirection,
    BiasReasonCode,
    DailyBiasEngine,
    DailyCandle,
    generate_snapshot_id,
)
from app.market.session import MarketSession


def test_daily_bias_bullish_boundary():
    """C > H strictly results in BULLISH bias."""
    engine = DailyBiasEngine(rule_version="v1.0")

    # Reference D-2: High 2500, Low 2400
    ref_candle = DailyCandle(
        trade_date=date(2026, 10, 1),
        open=2420.0,
        high=2500.0,
        low=2400.0,
        close=2480.0,
    )
    # Previous D-1: Close 2505 (> 2500)
    prev_candle = DailyCandle(
        trade_date=date(2026, 10, 5),
        open=2480.0,
        high=2510.0,
        low=2475.0,
        close=2505.0,
    )
    # Target session D
    snap = engine.evaluate(
        symbol="RELIANCE",
        security_id="2885",
        trading_date=date(2026, 10, 6),
        previous_candle=prev_candle,
        reference_candle=ref_candle,
    )
    assert snap.daily_bias == BiasDirection.BULLISH
    assert snap.reason_code == BiasReasonCode.CLOSE_ABOVE_REF_HIGH
    assert snap.previous_close == 2505.0
    assert snap.reference_high == 2500.0
    assert snap.reference_low == 2400.0


def test_daily_bias_bearish_boundary():
    """C < L strictly results in BEARISH bias."""
    engine = DailyBiasEngine(rule_version="v1.0")

    ref_candle = DailyCandle(
        trade_date=date(2026, 10, 1),
        open=2450.0,
        high=2500.0,
        low=2400.0,
        close=2420.0,
    )
    prev_candle = DailyCandle(
        trade_date=date(2026, 10, 5),
        open=2415.0,
        high=2420.0,
        low=2380.0,
        close=2395.0,  # < 2400
    )
    snap = engine.evaluate(
        symbol="TCS",
        security_id="11536",
        trading_date=date(2026, 10, 6),
        previous_candle=prev_candle,
        reference_candle=ref_candle,
    )
    assert snap.daily_bias == BiasDirection.BEARISH
    assert snap.reason_code == BiasReasonCode.CLOSE_BELOW_REF_LOW


def test_daily_bias_inside_range_neutral():
    """L < C < H results in NEUTRAL bias."""
    engine = DailyBiasEngine(rule_version="v1.0")

    ref_candle = DailyCandle(
        trade_date=date(2026, 10, 1),
        open=2450.0,
        high=2500.0,
        low=2400.0,
        close=2450.0,
    )
    prev_candle = DailyCandle(
        trade_date=date(2026, 10, 5),
        open=2450.0,
        high=2490.0,
        low=2420.0,
        close=2460.0,  # Inside [2400, 2500]
    )
    snap = engine.evaluate(
        symbol="INFY",
        security_id="1594",
        trading_date=date(2026, 10, 6),
        previous_candle=prev_candle,
        reference_candle=ref_candle,
    )
    assert snap.daily_bias == BiasDirection.NEUTRAL
    assert snap.reason_code == BiasReasonCode.INSIDE_REF_RANGE


def test_daily_bias_equal_boundaries_are_neutral():
    """Equal close C == H or C == L must be NEUTRAL (strictly greater/less required)."""
    engine = DailyBiasEngine()

    ref = DailyCandle(
        trade_date=date(2026, 10, 1),
        open=2450.0,
        high=2500.0,
        low=2400.0,
        close=2450.0,
    )
    # Equal High: C == 2500.0
    prev_high = DailyCandle(
        trade_date=date(2026, 10, 5),
        open=2480.0,
        high=2505.0,
        low=2470.0,
        close=2500.0,
    )
    snap_h = engine.evaluate(
        symbol="NIFTY",
        security_id="13",
        trading_date=date(2026, 10, 6),
        previous_candle=prev_high,
        reference_candle=ref,
    )
    assert snap_h.daily_bias == BiasDirection.NEUTRAL
    assert snap_h.reason_code == BiasReasonCode.EQUAL_REF_HIGH

    # Equal Low: C == 2400.0
    prev_low = DailyCandle(
        trade_date=date(2026, 10, 5),
        open=2420.0,
        high=2430.0,
        low=2395.0,
        close=2400.0,
    )
    snap_l = engine.evaluate(
        symbol="NIFTY",
        security_id="13",
        trading_date=date(2026, 10, 6),
        previous_candle=prev_low,
        reference_candle=ref,
    )
    assert snap_l.daily_bias == BiasDirection.NEUTRAL
    assert snap_l.reason_code == BiasReasonCode.EQUAL_REF_LOW


def test_daily_bias_missing_data_insufficient_history():
    """Missing either reference or previous session returns DATA_UNAVAILABLE."""
    engine = DailyBiasEngine()

    candle = DailyCandle(
        trade_date=date(2026, 10, 5),
        open=100.0,
        high=105.0,
        low=95.0,
        close=102.0,
    )
    snap1 = engine.evaluate(
        symbol="HDFCBANK",
        security_id="1333",
        trading_date=date(2026, 10, 6),
        previous_candle=None,
        reference_candle=candle,
    )
    assert snap1.daily_bias == BiasDirection.DATA_UNAVAILABLE
    assert snap1.reason_code == BiasReasonCode.INSUFFICIENT_HISTORY

    snap2 = engine.evaluate(
        symbol="HDFCBANK",
        security_id="1333",
        trading_date=date(2026, 10, 6),
        previous_candle=candle,
        reference_candle=None,
    )
    assert snap2.daily_bias == BiasDirection.DATA_UNAVAILABLE


def test_daily_bias_rejects_non_chronological_dates():
    """Chronology: reference < previous < trading_date strictly enforced."""
    engine = DailyBiasEngine()

    c1 = DailyCandle(trade_date=date(2026, 10, 5), open=100.0, high=110.0, low=90.0, close=105.0)
    c2 = DailyCandle(trade_date=date(2026, 10, 2), open=100.0, high=110.0, low=90.0, close=105.0)

    # Inverted: ref (10-05) > prev (10-02)
    snap = engine.evaluate(
        symbol="SBIN",
        security_id="3045",
        trading_date=date(2026, 10, 6),
        previous_candle=c2,
        reference_candle=c1,
    )
    assert snap.daily_bias == BiasDirection.DATA_UNAVAILABLE
    assert snap.reason_code == BiasReasonCode.NON_CHRONOLOGICAL_DATES


def test_daily_bias_rejects_invalid_candles():
    """Candle validation checks low <= high, open/close inside range."""
    engine = DailyBiasEngine()

    # Corrupted: high < low
    bad_candle = DailyCandle(trade_date=date(2026, 10, 1), open=100.0, high=90.0, low=110.0, close=95.0)
    valid_candle = DailyCandle(trade_date=date(2026, 10, 5), open=100.0, high=110.0, low=90.0, close=105.0)

    snap = engine.evaluate(
        symbol="ITC",
        security_id="1660",
        trading_date=date(2026, 10, 6),
        previous_candle=valid_candle,
        reference_candle=bad_candle,
    )
    assert snap.daily_bias == BiasDirection.DATA_UNAVAILABLE
    assert snap.reason_code == BiasReasonCode.INVALID_CANDLE_DATA


def test_market_session_get_previous_trading_days_handles_weekends_and_holidays():
    """
    Verifies that get_previous_trading_days correctly finds D-1 and D-2,
    skipping weekends and official holidays like Gandhi Jayanti (2026-10-02).
    """
    ms = MarketSession()

    # Monday 2026-10-05:
    # 2026-10-04 is Sun, 2026-10-03 is Sat.
    # 2026-10-02 is Gandhi Jayanti (Holiday).
    # Preceding trading days: D-1 = Thursday 2026-10-01, D-2 = Wednesday 2026-09-30.
    days = ms.get_previous_trading_days(reference_date=date(2026, 10, 5), count=2)
    assert len(days) == 2
    assert days[0] == date(2026, 10, 1)  # D-1
    assert days[1] == date(2026, 9, 30)  # D-2
    # Today (2026-10-05) is never included!
    assert date(2026, 10, 5) not in days
