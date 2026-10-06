"""
Market Session and Trading Hours Manager for NSE (Asia/Kolkata).
"""

from __future__ import annotations

from datetime import datetime, time, date, timedelta
from typing import Optional, Tuple
import zoneinfo

from app.config import settings

IST_TZ = zoneinfo.ZoneInfo("Asia/Kolkata")


def parse_time_str(t_str: str) -> time:
    """Parses 'HH:MM' or 'HH:MM:SS' into a datetime.time object."""
    parts = [int(p) for p in t_str.split(":")]
    if len(parts) == 2:
        return time(parts[0], parts[1], 0)
    elif len(parts) == 3:
        return time(parts[0], parts[1], parts[2])
    raise ValueError(f"Invalid time format: {t_str}")


NSE_HOLIDAYS = {
    # 2026 Official NSE Market Holidays
    date(2026, 1, 26),   # Republic Day
    date(2026, 3, 3),    # Holi
    date(2026, 3, 20),   # Id-Ul-Fitr
    date(2026, 3, 27),   # Ram Navami
    date(2026, 4, 3),    # Good Friday
    date(2026, 4, 14),   # Dr. Ambedkar Jayanti
    date(2026, 5, 1),    # Maharashtra Day
    date(2026, 5, 27),   # Bakri Id
    date(2026, 6, 26),   # Muharram
    date(2026, 8, 15),   # Independence Day
    date(2026, 10, 2),   # Mahatma Gandhi Jayanti (Tomorrow)
    date(2026, 10, 20),  # Dussehra
    date(2026, 11, 8),   # Diwali Laxmi Pujan
    date(2026, 11, 10),  # Diwali Balipratipada
    date(2026, 11, 24),  # Gurunanak Jayanti
    date(2026, 12, 25),  # Christmas
}


class MarketSession:
    """Encapsulates trading session timings and calendar logic."""

    def __init__(
        self,
        market_open: str = "09:15",
        orb_start: str = "09:30",
        orb_end: str = "10:00",
        entry_end: str = "15:25",
        market_close: str = "15:30",
        tz_name: str = "Asia/Kolkata",
    ):
        self.tz = zoneinfo.ZoneInfo(tz_name)
        self.market_open_time = parse_time_str(market_open)
        self.orb_start_time = parse_time_str(orb_start)
        self.orb_end_time = parse_time_str(orb_end)
        self.entry_end_time = parse_time_str(entry_end)
        self.market_close_time = parse_time_str(market_close)

    def now(self) -> datetime:
        """Returns the current localized datetime in Asia/Kolkata."""
        return datetime.now(self.tz)

    def localize(self, dt: datetime) -> datetime:
        """Ensures a datetime is aware and localized to Asia/Kolkata."""
        if dt.tzinfo is None:
            return dt.replace(tzinfo=self.tz)
        return dt.astimezone(self.tz)

    def is_trading_day(self, d: Optional[date] = None) -> bool:
        """Returns True if the given date is a weekday and not an official NSE holiday."""
        check_date = d or self.now().date()
        # Saturday (5) or Sunday (6)
        if check_date.weekday() >= 5:
            return False
        # Official NSE holiday check
        return check_date not in NSE_HOLIDAYS

    def is_market_open(self, dt: Optional[datetime] = None) -> bool:
        """Returns True if current time is within 09:15:00 and 15:30:00 on a weekday (NSE Equity)."""
        check_dt = self.localize(dt or self.now())
        if not self.is_trading_day(check_dt.date()):
            return False
        t = check_dt.time()
        return self.market_open_time <= t < self.market_close_time

    def is_commodity_market_open(self, dt: Optional[datetime] = None) -> bool:
        """Commodity scanner disabled by user preference (focused 100% on NSE Equity & Indices)."""
        return False

    def is_any_market_open(self, dt: Optional[datetime] = None) -> bool:
        """Only NSE Equity and Major Indices are actively traded."""
        return self.is_market_open(dt)

    def is_orb_period(self, dt: datetime) -> bool:
        """
        Returns True if the timestamp falls strictly in the Benchmark Range:
        09:30:00 inclusive to 10:00:00 exclusive.
        (09:15:00 - 09:30:00 candle is ignored).
        """
        check_dt = self.localize(dt)
        t = check_dt.time()
        return self.orb_start_time <= t < self.orb_end_time

    def is_entry_allowed(self, dt: datetime) -> bool:
        """
        Returns True if new breakout entries are allowed:
        From 10:00:00 inclusive up to 15:25:00 inclusive.
        """
        check_dt = self.localize(dt)
        t = check_dt.time()
        return self.orb_end_time <= t <= self.entry_end_time

    def is_eod_squareoff(self, dt: datetime) -> bool:
        """Returns True if market has reached or passed EOD cutoff (15:25:00)."""
        check_dt = self.localize(dt)
        t = check_dt.time()
        return t >= self.entry_end_time

    def get_session_datetimes(self, d: date) -> Tuple[datetime, datetime, datetime, datetime]:
        """Returns (open_dt, orb_end_dt, entry_end_dt, close_dt) for a date."""
        open_dt = datetime.combine(d, self.market_open_time, tzinfo=self.tz)
        orb_end_dt = datetime.combine(d, self.orb_end_time, tzinfo=self.tz)
        entry_end_dt = datetime.combine(d, self.entry_end_time, tzinfo=self.tz)
        close_dt = datetime.combine(d, self.market_close_time, tzinfo=self.tz)
        return open_dt, orb_end_dt, entry_end_dt, close_dt


default_session = MarketSession(
    market_open=settings.strategy.session.market_open,
    orb_start=settings.strategy.session.orb_start,
    orb_end=settings.strategy.session.orb_end,
    entry_end=settings.strategy.session.entry_end,
    market_close=settings.strategy.session.market_close,
    tz_name=settings.timezone_name,
)
