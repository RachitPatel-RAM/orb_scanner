"""
Unit Tests for Candle Builder Engine.
"""

from datetime import datetime
import pytest
import zoneinfo

from app.market.candle_builder import CandleBuilder, TickData
from app.market.session import IST_TZ
from app.storage.models import Candle


def test_1m_candle_building():
    closed_1m_candles = []
    builder = CandleBuilder(
        on_1m_candle_closed=lambda c: closed_1m_candles.append(c),
        persist_to_db=False,
    )

    t1 = TickData("100", "RELIANCE", 2500.0, datetime(2026, 10, 1, 9, 15, 5, tzinfo=IST_TZ), 10.0)
    t2 = TickData("100", "RELIANCE", 2510.0, datetime(2026, 10, 1, 9, 15, 25, tzinfo=IST_TZ), 20.0)
    t3 = TickData("100", "RELIANCE", 2495.0, datetime(2026, 10, 1, 9, 15, 45, tzinfo=IST_TZ), 15.0)
    t4 = TickData("100", "RELIANCE", 2505.0, datetime(2026, 10, 1, 9, 15, 58, tzinfo=IST_TZ), 5.0)

    # First minute ticks
    builder.process_tick(t1)
    builder.process_tick(t2)
    builder.process_tick(t3)
    builder.process_tick(t4)

    assert len(closed_1m_candles) == 0, "Current 1m candle is still open"

    # Tick for next minute arrives -> closes 09:15 candle
    t5 = TickData("100", "RELIANCE", 2508.0, datetime(2026, 10, 1, 9, 16, 2, tzinfo=IST_TZ), 50.0)
    builder.process_tick(t5)

    assert len(closed_1m_candles) == 1
    c = closed_1m_candles[0]
    assert c.timestamp.minute == 15
    assert c.open == 2500.0
    assert c.high == 2510.0
    assert c.low == 2495.0
    assert c.close == 2505.0
    assert c.volume == 50.0  # 10 + 20 + 15 + 5
    assert c.is_closed is True


def test_5m_candle_aggregation():
    closed_5m_candles = []
    builder = CandleBuilder(
        on_5m_candle_closed=lambda c: closed_5m_candles.append(c),
        persist_to_db=False,
    )

    # Feed 1m ticks spanning 09:15 to 09:20
    # Minutes: 15, 16, 17, 18, 19
    for m in range(15, 20):
        # Open of minute
        builder.process_tick(TickData("100", "RELIANCE", 2500.0 + m, datetime(2026, 10, 1, 9, m, 1, tzinfo=IST_TZ), 10.0))
        # High of minute
        builder.process_tick(TickData("100", "RELIANCE", 2505.0 + m, datetime(2026, 10, 1, 9, m, 30, tzinfo=IST_TZ), 10.0))
        # Close of minute
        builder.process_tick(TickData("100", "RELIANCE", 2502.0 + m, datetime(2026, 10, 1, 9, m, 59, tzinfo=IST_TZ), 10.0))

    # Tick for 09:20 arrives
    builder.process_tick(TickData("100", "RELIANCE", 2530.0, datetime(2026, 10, 1, 9, 20, 1, tzinfo=IST_TZ), 10.0))

    # 09:19 1m candle closed -> completes 09:15-09:20 5m candle!
    assert len(closed_5m_candles) == 1
    c5 = closed_5m_candles[0]
    assert c5.timestamp.minute == 15
    assert c5.open == 2515.0  # First tick at 09:15
    assert c5.close == 2521.0  # Last tick at 09:19 (2502 + 19)
    assert c5.is_closed is True


def test_15m_candle_aggregation():
    closed_15m_candles = []
    builder = CandleBuilder(
        on_15m_candle_closed=lambda c: closed_15m_candles.append(c),
        persist_to_db=False,
    )

    # Feed ticks spanning 09:15 to 09:30 (15 minutes)
    for m in range(15, 30):
        builder.process_tick(TickData("100", "RELIANCE", 2500.0 + m, datetime(2026, 10, 1, 9, m, 1, tzinfo=IST_TZ), 10.0))
        builder.process_tick(TickData("100", "RELIANCE", 2505.0 + m, datetime(2026, 10, 1, 9, m, 30, tzinfo=IST_TZ), 10.0))
        builder.process_tick(TickData("100", "RELIANCE", 2502.0 + m, datetime(2026, 10, 1, 9, m, 59, tzinfo=IST_TZ), 10.0))

    # Tick for 09:30 arrives
    builder.process_tick(TickData("100", "RELIANCE", 2550.0, datetime(2026, 10, 1, 9, 30, 1, tzinfo=IST_TZ), 10.0))

    assert len(closed_15m_candles) == 1
    c15 = closed_15m_candles[0]
    assert c15.timestamp.minute == 15
    assert c15.open == 2515.0  # Open at 09:15:01
    assert c15.close == 2531.0  # Close at 09:29:59 (2502 + 29)
    assert c15.is_closed is True

