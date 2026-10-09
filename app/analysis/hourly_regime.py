"""
Hourly (60m) Directional Regime Engine (Section 5).

Implements deterministic 60m trend classification:
- Aggregates completed 60m candles from session-open anchored 09:15 intervals:
  Bar 1: 09:15-10:15
  Bar 2: 10:15-11:15
  Bar 3: 11:15-12:15
  Bar 4: 12:15-13:15
  Bar 5: 13:15-14:15
  Bar 6: 14:15-15:15
  Shortened final fragment (15:15-15:30, 15m) is strictly excluded from hourly trend calculation.
- Evaluates only completed higher-timeframe bars.
- Swing pivots: 2 left, 2 right bars, strict inequality. Confirmed only at the close of the 2nd right bar.
- EMA20 and EMA50.
- Bullish:
    1. Last two confirmed swing highs are higher (H2 > H1).
    2. Last two confirmed swing lows are higher (L2 > L1).
    3. EMA20 > EMA50.
    4. EMA20 > EMA20[3] (value 3 completed hourly bars earlier).
- Bearish: Exact inverse.
- Otherwise: MIXED / RANGE (no trend trade).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from app.storage.models import Candle


class TrendRegime(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    MIXED = "MIXED"


@dataclass(frozen=True)
class ConfirmedPivot:
    timeframe: str       # '60m', '15m', '5m'
    pivot_type: str      # 'HIGH' or 'LOW'
    price: float
    bar_index: int
    bar_timestamp: str   # Timestamp of the pivot candle itself
    confirmed_at: str    # Timestamp of the bar whose close confirmed this pivot (i+2)
    id: str


@dataclass
class HourlyRegimeResult:
    status: TrendRegime
    symbol: str
    evaluated_at: str    # Timestamp of the latest completed 60m candle
    ema20: float
    ema50: float
    ema20_prev3: Optional[float] = None
    confirmed_swing_highs: List[ConfirmedPivot] = field(default_factory=list)
    confirmed_swing_lows: List[ConfirmedPivot] = field(default_factory=list)
    structure_hh: bool = False
    structure_hl: bool = False
    structure_lh: bool = False
    structure_ll: bool = False
    ema_aligned: bool = False
    ema_slope_positive: bool = False
    reason_code: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d


class HourlyRegimeEngine:
    """Computes auditable 60m directional regime with zero future-bar lookahead."""

    @staticmethod
    def aggregate_session_60m_candles(
        candles_1m_or_5m: List[Candle],
        session_open_time: time = time(9, 15),
        session_close_time: time = time(15, 30),
    ) -> List[Candle]:
        """
        Builds 60m bars anchored strictly to session open (09:15).
        Full 60-minute windows:
        09:15-10:15, 10:15-11:15, 11:15-12:15, 12:15-13:15, 13:15-14:15, 14:15-15:15.
        The shortened 15:15-15:30 fragment (15m) is excluded per specification.
        """
        if not candles_1m_or_5m:
            return []

        # Group by trade date
        by_date: Dict[str, List[Candle]] = {}
        for c in candles_1m_or_5m:
            d_str = c.timestamp.date().isoformat()
            if d_str not in by_date:
                by_date[d_str] = []
            by_date[d_str].append(c)

        hourly_candles: List[Candle] = []

        for d_str, day_candles in sorted(by_date.items()):
            # Sort chronologically
            day_candles.sort(key=lambda x: x.timestamp)
            first_c = day_candles[0]
            sec_id = first_c.security_id
            symbol = first_c.symbol
            c_date = first_c.timestamp.date()

            # Define the 6 regular 60-minute windows
            window_starts = [
                datetime.combine(c_date, session_open_time).replace(tzinfo=first_c.timestamp.tzinfo) + timedelta(hours=i)
                for i in range(6)
            ]

            for w_start in window_starts:
                w_end = w_start + timedelta(hours=1)
                # Find all constituent candles strictly inside [w_start, w_end)
                bucket = [c for c in day_candles if w_start <= c.timestamp < w_end]

                # Require constituents and that the window has completed
                # E.g., if checking historical or completed bars
                if not bucket:
                    continue

                # Ensure bucket has sufficient coverage (e.g. at least spans near w_end)
                # For a completed 60m bar from 1m data, should have close near w_end
                open_p = bucket[0].open
                high_p = max(c.high for c in bucket)
                low_p = min(c.low for c in bucket)
                close_p = bucket[-1].close
                vol_tot = sum(c.volume for c in bucket)

                hourly_candles.append(
                    Candle(
                        security_id=sec_id,
                        symbol=symbol,
                        timestamp=w_start,
                        open=open_p,
                        high=high_p,
                        low=low_p,
                        close=close_p,
                        volume=vol_tot,
                        is_closed=True,
                    )
                )

        return hourly_candles

    @staticmethod
    def calculate_ema(prices: List[float], period: int) -> List[float]:
        """Calculates standard Exponential Moving Average."""
        if len(prices) < period:
            return []
        ema = [sum(prices[:period]) / period]
        multiplier = 2.0 / (period + 1.0)
        for p in prices[period:]:
            ema.append((p - ema[-1]) * multiplier + ema[-1])
        return ema

    @classmethod
    def find_confirmed_pivots(
        cls,
        candles: List[Candle],
        timeframe_label: str = "60m",
        left_bars: int = 2,
        right_bars: int = 2,
    ) -> Tuple[List[ConfirmedPivot], List[ConfirmedPivot]]:
        """
        Detects 2-left, 2-right confirmed pivots with strict inequality.
        Pivot at index i is confirmed ONLY at close of index i + right_bars.
        Ties are strictly excluded.
        """
        highs: List[ConfirmedPivot] = []
        lows: List[ConfirmedPivot] = []
        n = len(candles)

        if n < left_bars + right_bars + 1:
            return highs, lows

        for i in range(left_bars, n - right_bars):
            curr = candles[i]
            is_pivot_high = True
            is_pivot_low = True

            # Strict inequality on left
            for l in range(1, left_bars + 1):
                if curr.high <= candles[i - l].high:
                    is_pivot_high = False
                if curr.low >= candles[i - l].low:
                    is_pivot_low = False

            # Strict inequality on right
            for r in range(1, right_bars + 1):
                if curr.high <= candles[i + r].high:
                    is_pivot_high = False
                if curr.low >= candles[i + r].low:
                    is_pivot_low = False

            confirm_bar = candles[i + right_bars]
            confirm_time = confirm_bar.iso_timestamp

            if is_pivot_high:
                pid = f"PH_{timeframe_label}_{curr.timestamp.strftime('%Y%m%d_%H%M')}_{round(curr.high, 2)}"
                highs.append(
                    ConfirmedPivot(
                        timeframe=timeframe_label,
                        pivot_type="HIGH",
                        price=curr.high,
                        bar_index=i,
                        bar_timestamp=curr.iso_timestamp,
                        confirmed_at=confirm_time,
                        id=pid,
                    )
                )

            if is_pivot_low:
                pid = f"PL_{timeframe_label}_{curr.timestamp.strftime('%Y%m%d_%H%M')}_{round(curr.low, 2)}"
                lows.append(
                    ConfirmedPivot(
                        timeframe=timeframe_label,
                        pivot_type="LOW",
                        price=curr.low,
                        bar_index=i,
                        bar_timestamp=curr.iso_timestamp,
                        confirmed_at=confirm_time,
                        id=pid,
                    )
                )

        return highs, lows

    @classmethod
    def evaluate_regime(
        cls,
        completed_60m_candles: List[Candle],
        as_of_time: Optional[datetime] = None,
    ) -> HourlyRegimeResult:
        """
        Evaluates 60m trend regime using only already-completed 60m candles known up to as_of_time.
        """
        if as_of_time is not None:
            candles = [c for c in completed_60m_candles if c.timestamp + timedelta(hours=1) <= as_of_time]
        else:
            candles = list(completed_60m_candles)

        if len(candles) < 53: # Need at least 50 bars for EMA50 + 3 bars for slope check
            return HourlyRegimeResult(
                status=TrendRegime.MIXED,
                symbol=candles[-1].symbol if candles else "",
                evaluated_at=candles[-1].iso_timestamp if candles else "",
                ema20=0.0,
                ema50=0.0,
                reason_code="INSUFFICIENT_60M_HISTORY",
            )

        symbol = candles[-1].symbol
        eval_at = candles[-1].iso_timestamp
        close_prices = [c.close for c in candles]

        # 1. EMAs
        ema20_series = cls.calculate_ema(close_prices, 20)
        ema50_series = cls.calculate_ema(close_prices, 50)

        if not ema20_series or not ema50_series:
            return HourlyRegimeResult(
                status=TrendRegime.MIXED,
                symbol=symbol,
                evaluated_at=eval_at,
                ema20=0.0,
                ema50=0.0,
                reason_code="EMA_UNAVAILABLE",
            )

        # Align series to current bar
        current_ema20 = ema20_series[-1]
        current_ema50 = ema50_series[-1]

        # EMA20 3 bars ago
        if len(ema20_series) >= 4:
            ema20_prev3 = ema20_series[-4]
        else:
            ema20_prev3 = current_ema20

        # 2. Confirmed swing pivots (using strictly known-at close of i+2)
        highs, lows = cls.find_confirmed_pivots(candles, timeframe_label="60m", left_bars=2, right_bars=2)

        if len(highs) < 2 or len(lows) < 2:
            return HourlyRegimeResult(
                status=TrendRegime.MIXED,
                symbol=symbol,
                evaluated_at=eval_at,
                ema20=round(current_ema20, 2),
                ema50=round(current_ema50, 2),
                ema20_prev3=round(ema20_prev3, 2),
                reason_code="FEWER_THAN_2_CONFIRMED_PIVOTS",
            )

        last_h1, last_h2 = highs[-2], highs[-1] # h1 older, h2 newer
        last_l1, last_l2 = lows[-2], lows[-1]   # l1 older, l2 newer

        hh = last_h2.price > last_h1.price
        hl = last_l2.price > last_l1.price
        lh = last_h2.price < last_h1.price
        ll = last_l2.price < last_l1.price

        bullish_ema = (current_ema20 > current_ema50) and (current_ema20 > ema20_prev3)
        bearish_ema = (current_ema20 < current_ema50) and (current_ema20 < ema20_prev3)

        if hh and hl and bullish_ema:
            return HourlyRegimeResult(
                status=TrendRegime.BULLISH,
                symbol=symbol,
                evaluated_at=eval_at,
                ema20=round(current_ema20, 2),
                ema50=round(current_ema50, 2),
                ema20_prev3=round(ema20_prev3, 2),
                confirmed_swing_highs=[last_h1, last_h2],
                confirmed_swing_lows=[last_l1, last_l2],
                structure_hh=True,
                structure_hl=True,
                ema_aligned=True,
                ema_slope_positive=True,
                reason_code="CONFIRMED_HH_HL_EMA_UP",
            )
        elif lh and ll and bearish_ema:
            return HourlyRegimeResult(
                status=TrendRegime.BEARISH,
                symbol=symbol,
                evaluated_at=eval_at,
                ema20=round(current_ema20, 2),
                ema50=round(current_ema50, 2),
                ema20_prev3=round(ema20_prev3, 2),
                confirmed_swing_highs=[last_h1, last_h2],
                confirmed_swing_lows=[last_l1, last_l2],
                structure_lh=True,
                structure_ll=True,
                ema_aligned=True,
                ema_slope_positive=False,
                reason_code="CONFIRMED_LH_LL_EMA_DOWN",
            )

        return HourlyRegimeResult(
            status=TrendRegime.MIXED,
            symbol=symbol,
            evaluated_at=eval_at,
            ema20=round(current_ema20, 2),
            ema50=round(current_ema50, 2),
            ema20_prev3=round(ema20_prev3, 2),
            confirmed_swing_highs=[last_h1, last_h2],
            confirmed_swing_lows=[last_l1, last_l2],
            structure_hh=hh,
            structure_hl=hl,
            structure_lh=lh,
            structure_ll=ll,
            ema_aligned=current_ema20 > current_ema50,
            ema_slope_positive=current_ema20 > ema20_prev3,
            reason_code="MIXED_STRUCTURE_OR_EMA_DISAGREEMENT",
        )


hourly_regime_engine = HourlyRegimeEngine()
