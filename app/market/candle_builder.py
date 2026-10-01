"""
Deterministic 1-Minute and 5-Minute Candle Engine.

Aggregates incoming ticks into 1-minute candles, and rolls completed 1-minute
candles into closed 5-minute candles with full Asia/Kolkata alignment.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional
import zoneinfo

from app.config import logger
from app.market.session import default_session
from app.storage.database import db
from app.storage.models import Candle


@dataclass
class TickData:
    security_id: str
    symbol: str
    ltp: float
    timestamp: datetime
    volume: float = 0.0


class CandleBuilder:
    """Builds 1-minute candles from ticks, rolls to 5-minute, and rolls to 15-minute candles."""

    def __init__(
        self,
        on_1m_candle_closed: Optional[Callable[[Candle], None]] = None,
        on_5m_candle_closed: Optional[Callable[[Candle], None]] = None,
        on_15m_candle_closed: Optional[Callable[[Candle], None]] = None,
        persist_to_db: bool = True,
    ):
        self.on_1m_candle_closed = on_1m_candle_closed
        self.on_5m_candle_closed = on_5m_candle_closed
        self.on_15m_candle_closed = on_15m_candle_closed
        self.persist_to_db = persist_to_db

        # Active building 1m candle per security_id
        # security_id -> Candle
        self.current_1m: Dict[str, Candle] = {}

        # Accumulated closed 1m candles for forming 5m candle
        # security_id -> List[Candle]
        self.accumulated_1m_for_5m: Dict[str, List[Candle]] = {}

        # Accumulated closed 5m candles for forming 15m candle
        # security_id -> List[Candle]
        self.accumulated_5m_for_15m: Dict[str, List[Candle]] = {}

        # Last closed timestamps to prevent duplicates
        self.last_closed_5m_ts: Dict[str, datetime] = {}
        self.last_closed_15m_ts: Dict[str, datetime] = {}

    def _floor_to_minute(self, dt: datetime) -> datetime:
        """Aligns timestamp down to the nearest minute boundary (0 seconds, 0 microseconds)."""
        dt_local = default_session.localize(dt)
        return dt_local.replace(second=0, microsecond=0)

    def _floor_to_5minute(self, dt: datetime) -> datetime:
        """Aligns timestamp down to the nearest 5-minute boundary."""
        dt_local = default_session.localize(dt)
        minute = (dt_local.minute // 5) * 5
        return dt_local.replace(minute=minute, second=0, microsecond=0)

    def _floor_to_15minute(self, dt: datetime) -> datetime:
        """Aligns timestamp down to the nearest 15-minute boundary."""
        dt_local = default_session.localize(dt)
        minute = (dt_local.minute // 15) * 15
        return dt_local.replace(minute=minute, second=0, microsecond=0)

    def process_tick(self, tick: TickData) -> None:
        """Ingests a real-time tick and updates candle aggregations."""
        tick_time = default_session.localize(tick.timestamp)
        candle_start = self._floor_to_minute(tick_time)
        sec_id = tick.security_id

        current = self.current_1m.get(sec_id)

        if current is None:
            # Start first 1m candle
            self.current_1m[sec_id] = Candle(
                security_id=sec_id,
                symbol=tick.symbol,
                timestamp=candle_start,
                open=tick.ltp,
                high=tick.ltp,
                low=tick.ltp,
                close=tick.ltp,
                volume=tick.volume,
                is_closed=False,
            )
            return

        if candle_start == current.timestamp:
            # Update currently building 1m candle
            current.high = max(current.high, tick.ltp)
            current.low = min(current.low, tick.ltp)
            current.close = tick.ltp
            current.volume += tick.volume
        elif candle_start > current.timestamp:
            # Previous 1m candle has closed
            closed_1m = current
            closed_1m.is_closed = True
            self._handle_1m_closed(closed_1m)

            # Start new 1m candle
            self.current_1m[sec_id] = Candle(
                security_id=sec_id,
                symbol=tick.symbol,
                timestamp=candle_start,
                open=tick.ltp,
                high=tick.ltp,
                low=tick.ltp,
                close=tick.ltp,
                volume=tick.volume,
                is_closed=False,
            )

    def _handle_1m_closed(self, candle_1m: Candle) -> None:
        """Processes a finalized 1-minute candle and aggregates into 5-minute candles."""
        sec_id = candle_1m.security_id

        if self.persist_to_db:
            db.save_candle_1m(
                security_id=candle_1m.security_id,
                symbol=candle_1m.symbol,
                timestamp=candle_1m.iso_timestamp,
                open_=candle_1m.open,
                high=candle_1m.high,
                low=candle_1m.low,
                close=candle_1m.close,
                volume=candle_1m.volume,
                is_closed=True,
            )

        if self.on_1m_candle_closed:
            try:
                self.on_1m_candle_closed(candle_1m)
            except Exception as e:
                logger.error(f"Error in on_1m_candle_closed callback for {candle_1m.symbol}: {e}")

        # Add to 5-minute accumulation bucket
        if sec_id not in self.accumulated_1m_for_5m:
            self.accumulated_1m_for_5m[sec_id] = []
        self.accumulated_1m_for_5m[sec_id].append(candle_1m)

        # Check if this 1m candle completes a 5m block
        # A 1m candle starting at minute ending in 4 or 9 (e.g., 09:19, 09:24, 09:29, 09:34)
        # represents the last minute of that 5m interval!
        # When 09:19 candle closes, the 09:15-09:20 5-minute candle is complete.
        if candle_1m.timestamp.minute % 5 == 4:
            self._close_5m_candle(sec_id)

    def _close_5m_candle(self, sec_id: str) -> Optional[Candle]:
        """Rolls up accumulated 1m candles into a finalized 5m candle."""
        candles = self.accumulated_1m_for_5m.get(sec_id, [])
        if not candles:
            return None

        # Determine 5m candle start time from the first candle or floor of last candle
        last_c = candles[-1]
        five_m_start = self._floor_to_5minute(last_c.timestamp)

        # Filter candles belonging strictly to this 5m bucket [five_m_start, five_m_start + 5min)
        five_m_end = five_m_start + timedelta(minutes=5)
        bucket_candles = [c for c in candles if five_m_start <= c.timestamp < five_m_end]

        if not bucket_candles:
            return None

        # Remove used candles from accumulation
        self.accumulated_1m_for_5m[sec_id] = [c for c in candles if c.timestamp >= five_m_end]

        symbol = bucket_candles[0].symbol
        open_price = bucket_candles[0].open
        close_price = bucket_candles[-1].close
        high_price = max(c.high for c in bucket_candles)
        low_price = min(c.low for c in bucket_candles)
        total_vol = sum(c.volume for c in bucket_candles)

        closed_5m = Candle(
            security_id=sec_id,
            symbol=symbol,
            timestamp=five_m_start,
            open=open_price,
            high=high_price,
            low=low_price,
            close=close_price,
            volume=total_vol,
            is_closed=True,
        )

        # Idempotency check on 5m timestamp
        if self.last_closed_5m_ts.get(sec_id) == five_m_start:
            logger.debug(f"5m candle for {symbol} at {five_m_start} already emitted.")
            return closed_5m

        self.last_closed_5m_ts[sec_id] = five_m_start

        if self.persist_to_db:
            db.save_candle_5m(
                security_id=closed_5m.security_id,
                symbol=closed_5m.symbol,
                timestamp=closed_5m.iso_timestamp,
                open_=closed_5m.open,
                high=closed_5m.high,
                low=closed_5m.low,
                close=closed_5m.close,
                volume=closed_5m.volume,
                is_closed=True,
            )

        if self.on_5m_candle_closed:
            try:
                self.on_5m_candle_closed(closed_5m)
            except Exception as e:
                logger.error(f"Error in on_5m_candle_closed callback for {symbol}: {e}")

        # Accumulate into 15-minute bucket
        if sec_id not in self.accumulated_5m_for_15m:
            self.accumulated_5m_for_15m[sec_id] = []
        self.accumulated_5m_for_15m[sec_id].append(closed_5m)

        # Check if 15-minute boundary completed (e.g. 09:10+5=09:15, 09:25+5=09:30, 09:40+5=09:45)
        if (five_m_start.minute + 5) % 15 == 0:
            self._close_15m_candle(sec_id)

        return closed_5m

    def _close_15m_candle(self, sec_id: str) -> Optional[Candle]:
        """Rolls up accumulated 5m candles into a finalized 15m candle."""
        candles = self.accumulated_5m_for_15m.get(sec_id, [])
        if not candles:
            return None

        last_c = candles[-1]
        fifteen_m_start = self._floor_to_15minute(last_c.timestamp)
        fifteen_m_end = fifteen_m_start + timedelta(minutes=15)
        bucket_candles = [c for c in candles if fifteen_m_start <= c.timestamp < fifteen_m_end]

        if not bucket_candles:
            return None

        self.accumulated_5m_for_15m[sec_id] = [c for c in candles if c.timestamp >= fifteen_m_end]

        symbol = bucket_candles[0].symbol
        open_price = bucket_candles[0].open
        close_price = bucket_candles[-1].close
        high_price = max(c.high for c in bucket_candles)
        low_price = min(c.low for c in bucket_candles)
        total_vol = sum(c.volume for c in bucket_candles)

        closed_15m = Candle(
            security_id=sec_id,
            symbol=symbol,
            timestamp=fifteen_m_start,
            open=open_price,
            high=high_price,
            low=low_price,
            close=close_price,
            volume=total_vol,
            is_closed=True,
        )

        if self.last_closed_15m_ts.get(sec_id) == fifteen_m_start:
            return closed_15m

        self.last_closed_15m_ts[sec_id] = fifteen_m_start

        if self.persist_to_db:
            db.save_candle_15m(
                security_id=closed_15m.security_id,
                symbol=closed_15m.symbol,
                timestamp=closed_15m.iso_timestamp,
                open_=closed_15m.open,
                high=closed_15m.high,
                low=closed_15m.low,
                close=closed_15m.close,
                volume=closed_15m.volume,
                is_closed=True,
            )

        if self.on_15m_candle_closed:
            try:
                self.on_15m_candle_closed(closed_15m)
            except Exception as e:
                logger.error(f"Error in on_15m_candle_closed callback for {symbol}: {e}")

        return closed_15m

    def flush_stale_candles(self, current_time: Optional[datetime] = None) -> None:
        """
        Closes any in-progress 1m candle if the current clock time has moved to a later minute.
        Useful when no ticks arrive for a symbol.
        """
        now = default_session.localize(current_time or default_session.now())
        now_minute = self._floor_to_minute(now)

        for sec_id, candle in list(self.current_1m.items()):
            if now_minute > candle.timestamp:
                closed_1m = candle
                closed_1m.is_closed = True
                del self.current_1m[sec_id]
                self._handle_1m_closed(closed_1m)
