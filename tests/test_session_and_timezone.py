"""
Unit Tests for Session Timings and Asia/Kolkata Timezone Handling.
"""

from datetime import datetime, date, time
import pytest

from app.market.session import default_session, IST_TZ


def test_timezone_is_kolkata():
    now = default_session.now()
    assert str(now.tzinfo) == "Asia/Kolkata"


def test_orb_period_boundaries():
    d = date(2026, 10, 1)  # Thursday (trading day)

    # 09:15:00 -> False (09:15-09:30 candle is ignored)
    t_open = datetime.combine(d, time(9, 15, 0), tzinfo=IST_TZ)
    assert default_session.is_orb_period(t_open) is False

    # 09:29:59 -> False
    t_before = datetime.combine(d, time(9, 29, 59), tzinfo=IST_TZ)
    assert default_session.is_orb_period(t_before) is False

    # 09:30:00 -> True (inclusive start of 09:30 candle)
    t_start = datetime.combine(d, time(9, 30, 0), tzinfo=IST_TZ)
    assert default_session.is_orb_period(t_start) is True

    # 09:59:59 -> True (inside 09:45 candle)
    t_inside = datetime.combine(d, time(9, 59, 59), tzinfo=IST_TZ)
    assert default_session.is_orb_period(t_inside) is True

    # 10:00:00 -> False (range ended, entries start)
    t_end = datetime.combine(d, time(10, 0, 0), tzinfo=IST_TZ)
    assert default_session.is_orb_period(t_end) is False


def test_entry_window_boundaries():
    d = date(2026, 10, 1)

    # 09:59:59 -> Not allowed yet (range still forming)
    t_before = datetime.combine(d, time(9, 59, 59), tzinfo=IST_TZ)
    assert default_session.is_entry_allowed(t_before) is False

    # 10:00:00 -> Allowed (breakout window starts)
    t_start = datetime.combine(d, time(10, 0, 0), tzinfo=IST_TZ)
    assert default_session.is_entry_allowed(t_start) is True

    # 15:25:00 -> Allowed (last entry second)
    t_cutoff = datetime.combine(d, time(15, 25, 0), tzinfo=IST_TZ)
    assert default_session.is_entry_allowed(t_cutoff) is True

    # 15:25:01 -> Not allowed
    t_late = datetime.combine(d, time(15, 25, 1), tzinfo=IST_TZ)
    assert default_session.is_entry_allowed(t_late) is False


def test_trading_day_weekend_logic():
    # 2026-10-03 is Saturday
    saturday = date(2026, 10, 3)
    assert default_session.is_trading_day(saturday) is False

    # 2026-10-04 is Sunday
    sunday = date(2026, 10, 4)
    assert default_session.is_trading_day(sunday) is False

    # 2026-10-02 is Friday (trading day)
    friday = date(2026, 10, 2)
    assert default_session.is_trading_day(friday) is True
