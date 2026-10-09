"""
Standard Traditional Daily Pivot Points Engine for Major Indices (NIFTY, BANKNIFTY, SENSEX).

Calculates standard Floor / Traditional Pivot levels:
  P = (H + L + C) / 3
  R1 = 2P - L, S1 = 2P - H
  R2 = P + (H - L), S2 = P - (H - L)
  R3 = H + 2(P - L), S3 = L - 2(H - P)
  R4 = R3 + (H - L), S4 = S3 - (H - L)

Derives Target 1, Target 2, Target 3 and Stop Loss for index ORB breakouts,
and rejects setups that slam directly into pivot levels (False Breakout / Trap Filter).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple
import httpx

from app.config import logger
from app.market.session import default_session
from app.storage.models import Direction


@dataclass
class TraditionalPivotPoints:
    pivot: float        # P
    r1: float           # R1
    r2: float           # R2
    r3: float           # R3
    r4: float           # R4
    s1: float           # S1
    s2: float           # S2
    s3: float           # S3
    s4: float           # S4
    high: float
    low: float
    close: float
    trade_date: date

    def get_support_levels(self) -> List[float]:
        """Returns support levels in descending order (highest support first)."""
        return sorted([self.s1, self.s2, self.s3, self.s4], reverse=True)

    def get_resistance_levels(self) -> List[float]:
        """Returns resistance levels in ascending order (lowest resistance first)."""
        return sorted([self.r1, self.r2, self.r3, self.r4])


def calculate_traditional_pivots(
    high: float,
    low: float,
    close: float,
    trade_date: Optional[date] = None,
) -> TraditionalPivotPoints:
    """Calculates Standard Floor / Traditional Pivots from session High, Low, Close."""
    p = round((high + low + close) / 3.0, 2)
    rng = high - low

    r1 = round(2.0 * p - low, 2)
    s1 = round(2.0 * p - high, 2)

    r2 = round(p + rng, 2)
    s2 = round(p - rng, 2)

    r3 = round(high + 2.0 * (p - low), 2)
    s3 = round(low - 2.0 * (high - p), 2)

    r4 = round(r3 + rng, 2)
    s4 = round(s3 - rng, 2)

    t_date = trade_date or default_session.now().date()
    return TraditionalPivotPoints(
        pivot=p,
        r1=r1,
        r2=r2,
        r3=r3,
        r4=r4,
        s1=s1,
        s2=s2,
        s3=s3,
        s4=s4,
        high=high,
        low=low,
        close=close,
        trade_date=t_date,
    )


class PivotPointsEngine:
    """Fetches prior day bar for indices and manages Traditional Pivot Point lookups."""

    INDEX_YAHOO_TICKERS = {
        "NIFTY": "^NSEI",
        "NIFTY 50": "^NSEI",
        "BANKNIFTY": "^NSEBANK",
        "SENSEX": "^BSESN",
    }

    def __init__(self):
        self._cache: Dict[str, Tuple[date, TraditionalPivotPoints]] = {}

    async def get_index_pivots(
        self,
        security_id: str,
        symbol: str,
        reference_date: Optional[date] = None,
    ) -> Optional[TraditionalPivotPoints]:
        """
        Retrieves Traditional Pivots for an index based on the prior trading session.
        Uses Dhan official data first, then falls back to Yahoo Finance.
        """
        ref_d = reference_date or default_session.now().date()
        cache_key = f"{symbol}_{ref_d.isoformat()}"
        if cache_key in self._cache:
            c_date, pts = self._cache[cache_key]
            if c_date == ref_d:
                return pts

        prev_days = default_session.get_previous_trading_days(reference_date=ref_d, count=1)
        if not prev_days:
            return None
        prev_date = prev_days[0]

        # 1. Fetch via HistoricalDataManager (checks local DB & Dhan charts/historical)
        try:
            from app.dhan.historical import historical_manager
            candle = await historical_manager.get_daily_candle(security_id, symbol, prev_date)
            if candle and candle.high > 0 and candle.low > 0 and candle.close > 0:
                pivots = calculate_traditional_pivots(candle.high, candle.low, candle.close, ref_d)
                self._cache[cache_key] = (ref_d, pivots)
                logger.info(
                    f"[Pivots] {symbol} for {ref_d}: P=₹{pivots.pivot:,.2f}, "
                    f"R1=₹{pivots.r1:,.2f}, S1=₹{pivots.s1:,.2f} from Dhan historical bar."
                )
                return pivots
        except Exception as e:
            logger.debug(f"Historical manager pivot fetch note for {symbol}: {e}")

        # 2. Fallback: Yahoo Finance query
        ticker = self.INDEX_YAHOO_TICKERS.get(symbol.upper(), f"{symbol}.NS")
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=5d"
            headers = {"User-Agent": "Mozilla/5.0"}
            async with httpx.AsyncClient(timeout=6.0) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 200:
                    d = resp.json().get("chart", {}).get("result", [{}])[0]
                    quote = d.get("indicators", {}).get("quote", [{}])[0]
                    highs = quote.get("high", [])
                    lows = quote.get("low", [])
                    closes = quote.get("close", [])
                    if len(highs) >= 2 and highs[-2] is not None:
                        pivots = calculate_traditional_pivots(
                            float(highs[-2]), float(lows[-2]), float(closes[-2]), ref_d
                        )
                        self._cache[cache_key] = (ref_d, pivots)
                        logger.info(
                            f"[Pivots Fallback] {symbol} for {ref_d}: P=₹{pivots.pivot:,.2f}, "
                            f"R1=₹{pivots.r1:,.2f}, S1=₹{pivots.s1:,.2f} from Yahoo Finance."
                        )
                        return pivots
        except Exception as e:
            logger.debug(f"Yahoo finance pivot fetch note for {symbol}: {e}")

        return None

    def determine_targets_and_sl(
        self,
        symbol: str,
        direction: Direction,
        entry_price: float,
        orb_high: float,
        orb_low: float,
        orb_mid: float,
        pivots: Optional[TraditionalPivotPoints],
    ) -> Tuple[bool, Optional[str], float, float, float, float]:
        """
        Determines Stop Loss and 3-Tier Traditional Pivot Targets.
        Rejects trades that slam directly into pivot levels (Trap Filter).

        Returns: (is_valid, rejection_reason, stop_loss, target_1, target_2, target_3)
        """
        stop_loss = round(orb_mid, 2)
        rng = orb_high - orb_low

        if direction == Direction.SHORT:
            if pivots:
                # Find pivot levels strictly below entry price in descending order
                all_levels = [pivots.pivot, pivots.s1, pivots.s2, pivots.s3, pivots.s4]
                below = [lvl for lvl in sorted(all_levels, reverse=True) if lvl < entry_price]

                if not below:
                    # Entry is below S4, use range extensions
                    t1 = round(entry_price - (rng * 0.8), 2)
                    t2 = round(entry_price - (rng * 1.5), 2)
                    t3 = round(entry_price - (rng * 2.2), 2)
                    return True, None, stop_loss, t1, t2, t3

                # TRAP FILTER: If nearest pivot is less than 15 pts below entry price,
                # the breakout candle slammed directly into support. Rejection / bounce risk is extreme.
                dist_to_next = entry_price - below[0]
                min_threshold = 20.0 if "BANK" in symbol else 12.0
                if dist_to_next < min_threshold:
                    reason = (
                        f"Pivot Support Trap: Entry ₹{entry_price:,.2f} is only {dist_to_next:.1f} pts "
                        f"above Pivot Support ₹{below[0]:,.2f}. High bounce risk."
                    )
                    return False, reason, stop_loss, 0.0, 0.0, 0.0

                t1 = below[0]
                t2 = below[1] if len(below) > 1 else round(t1 - rng, 2)
                t3 = below[2] if len(below) > 2 else round(t2 - rng, 2)
                return True, None, stop_loss, t1, t2, t3
            else:
                # Fallback without pivots
                risk = max(10.0, stop_loss - entry_price)
                t1 = round(entry_price - risk * 1.5, 2)
                t2 = round(entry_price - risk * 2.5, 2)
                t3 = round(entry_price - risk * 3.5, 2)
                return True, None, stop_loss, t1, t2, t3

        else:  # LONG
            if pivots:
                # Find pivot levels strictly above entry price in ascending order
                all_levels = [pivots.pivot, pivots.r1, pivots.r2, pivots.r3, pivots.r4]
                above = [lvl for lvl in sorted(all_levels) if lvl > entry_price]

                if not above:
                    t1 = round(entry_price + (rng * 0.8), 2)
                    t2 = round(entry_price + (rng * 1.5), 2)
                    t3 = round(entry_price + (rng * 2.2), 2)
                    return True, None, stop_loss, t1, t2, t3

                # TRAP FILTER: If nearest pivot is less than threshold above entry,
                # the candle slammed directly into resistance. Reversal risk is high.
                dist_to_next = above[0] - entry_price
                min_threshold = 20.0 if "BANK" in symbol else 12.0
                if dist_to_next < min_threshold:
                    reason = (
                        f"Pivot Resistance Trap: Entry ₹{entry_price:,.2f} is only {dist_to_next:.1f} pts "
                        f"below Pivot Resistance ₹{above[0]:,.2f}. High rejection risk."
                    )
                    return False, reason, stop_loss, 0.0, 0.0, 0.0

                t1 = above[0]
                t2 = above[1] if len(above) > 1 else round(t1 + rng, 2)
                t3 = above[2] if len(above) > 2 else round(t2 + rng, 2)
                return True, None, stop_loss, t1, t2, t3
            else:
                risk = max(10.0, entry_price - stop_loss)
                t1 = round(entry_price + risk * 1.5, 2)
                t2 = round(entry_price + risk * 2.5, 2)
                t3 = round(entry_price + risk * 3.5, 2)
                return True, None, stop_loss, t1, t2, t3


pivot_engine = PivotPointsEngine()
