"""
Intraday Liquidity Context Engine (Section 7).

Implements observable price rules on complete 60-minute bars:
- 60-min intervals anchored to session open (09:15 - 15:15; 6 full bars per regular day).
  Trailing 15-minute fragment (15:15 - 15:30) is excluded from context pivot calculations.
- Confirmed swing high/low with 2 left / 2 right bars (available only when bar i+2 closes).
- High sweep: breach above unconsumed reference high, reclaim strictly below in <= 3 bars -> BEARISH context.
- Low sweep: breach below unconsumed reference low, reclaim strictly above in <= 3 bars -> BULLISH context.
- Sweep extreme tracking, TTL expiry (2 bars after confirmation, capped at session end),
  and invalidation if completed bar closes beyond the sweep extreme.
- Conflicting simultaneously active evidence -> CONFLICT.
- No active confirmed sweep -> NONE.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from app.config import logger, settings
from app.market.session import default_session
from app.storage.database import db
from app.storage.models import Candle


class ContextDirection(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    CONFLICT = "CONFLICT"
    NONE = "NONE"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"


class PivotType(str, Enum):
    SWING_HIGH = "SWING_HIGH"
    SWING_LOW = "SWING_LOW"


class SweepStatus(str, Enum):
    ACTIVE = "ACTIVE"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"
    CONSUMED = "CONSUMED"


@dataclass
class Bar60m:
    """Represents a finalized, completed 60-minute candle."""
    bar_id: str
    symbol: str
    security_id: str
    start_time: datetime  # e.g. 09:15:00
    end_time: datetime    # e.g. 10:15:00
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def iso_end_time(self) -> str:
        return self.end_time.isoformat()


@dataclass
class ConfirmedPivot:
    """Confirmed 2-left / 2-right swing pivot."""
    pivot_id: str
    pivot_type: PivotType
    price: float
    pivot_bar_time: datetime     # Timestamp of bar i
    confirmation_time: datetime  # Timestamp when bar i+2 closed
    is_consumed: bool = False


@dataclass
class LiquidityContextEvent:
    """Record of a confirmed liquidity sweep event."""
    event_id: str
    symbol: str
    security_id: str
    timeframe_minutes: int
    pivot_type: PivotType
    level_price: float
    breach_bar_time: str
    confirmation_time: str
    reclaim_bars: int
    sweep_direction: ContextDirection  # BULLISH or BEARISH
    sweep_extreme: float
    status: SweepStatus
    expiry_time: str
    invalidation_time: Optional[str] = None
    rule_version: str = "v1.0"

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["pivot_type"] = self.pivot_type.value
        d["sweep_direction"] = self.sweep_direction.value
        d["status"] = self.status.value
        return d


@dataclass
class LiquidityContextSnapshot:
    """Current combined liquidity context state for a symbol."""
    symbol: str
    security_id: str
    timestamp: str
    direction: ContextDirection
    active_events: List[LiquidityContextEvent] = field(default_factory=list)
    latest_confirmed_high: Optional[float] = None
    latest_confirmed_low: Optional[float] = None
    rule_version: str = "v1.0"
    reason: str = "No active sweeps"


class LiquidityContextEngine:
    """
    Maintains the 60-minute candle series, swing pivots, and sweep state machine per symbol.
    """

    def __init__(self, rule_version: Optional[str] = None):
        self.rule_version = rule_version or getattr(settings, "bias_rule_version", "v1.0")
        # In-memory history: symbol -> List[Bar60m]
        self.bars_history: Dict[str, List[Bar60m]] = {}
        # Confirmed pivots: symbol -> List[ConfirmedPivot]
        self.pivots: Dict[str, List[ConfirmedPivot]] = {}
        # Active sweeps: symbol -> List[LiquidityContextEvent]
        self.active_sweeps: Dict[str, List[LiquidityContextEvent]] = {}
        # Pending candidate high/low sweep breaches: symbol -> Dict[str, Any]
        self._pending_high_sweep: Dict[str, Optional[Dict[str, Any]]] = {}
        self._pending_low_sweep: Dict[str, Optional[Dict[str, Any]]] = {}

    def reset_symbol(self, symbol: str) -> None:
        """Clears memory for a symbol."""
        sym = symbol.upper()
        self.bars_history.pop(sym, None)
        self.pivots.pop(sym, None)
        self.active_sweeps.pop(sym, None)
        self._pending_high_sweep.pop(sym, None)
        self._pending_low_sweep.pop(sym, None)

    @staticmethod
    def is_valid_60m_slot(dt: datetime) -> Optional[Tuple[time, time]]:
        """
        Validates if dt falls into one of the 6 complete 60-minute slots:
        09:15-10:15, 10:15-11:15, 11:15-12:15, 12:15-13:15, 13:15-14:15, 14:15-15:15.
        The 15:15-15:30 fragment is excluded.
        """
        t = dt.time()
        slots = [
            (time(9, 15), time(10, 15)),
            (time(10, 15), time(11, 15)),
            (time(11, 15), time(12, 15)),
            (time(12, 15), time(13, 15)),
            (time(13, 15), time(14, 15)),
            (time(14, 15), time(15, 15)),
        ]
        for s_start, s_end in slots:
            if s_start <= t < s_end:
                return (s_start, s_end)
        return None

    def add_60m_bar(self, bar: Bar60m) -> LiquidityContextSnapshot:
        """
        Appends a finalized 60-minute candle and updates pivots, sweeps,
        invalidations, and active context.
        """
        sym = bar.symbol.upper()
        if sym not in self.bars_history:
            self.bars_history[sym] = []
            self.pivots[sym] = []
            self.active_sweeps[sym] = []

        history = self.bars_history[sym]
        history.append(bar)

        # Cap history at 5 completed sessions (max 30 bars)
        if len(history) > 30:
            history = history[-30:]
            self.bars_history[sym] = history

        # 1. Check invalidation and TTL expiry on existing active sweeps
        self._update_sweep_lifecycle(sym, bar)

        # 2. Check swing pivots (requires at least 5 bars: i-2, i-1, i, i+1, i+2)
        self._detect_pivots(sym)

        # 3. Check for sweeps against active, unconsumed pivots
        self._evaluate_sweeps(sym, bar)

        # 4. Resolve current combined context
        return self.get_current_context(sym, bar.end_time)

    def _detect_pivots(self, symbol: str) -> None:
        """
        Detects 2-left / 2-right swing highs and lows.
        Pivot bar is index i = len - 3. Left bars: i-2, i-1. Right bars: i+1, i+2 (current bar).
        """
        history = self.bars_history[symbol]
        n = len(history)
        if n < 5:
            return

        i = n - 3
        p_bar = history[i]
        bar_left2 = history[i - 2]
        bar_left1 = history[i - 1]
        bar_right1 = history[i + 1]
        bar_right2 = history[i + 2]  # Just finalized

        # Swing High: strictly greater than 2 before and 2 after
        is_swing_high = (
            p_bar.high > bar_left2.high
            and p_bar.high > bar_left1.high
            and p_bar.high > bar_right1.high
            and p_bar.high > bar_right2.high
        )
        if is_swing_high:
            p_id = f"PIVOT_H_{symbol}_{p_bar.end_time.strftime('%Y%m%d%H%M')}"
            # Check if already added
            if not any(p.pivot_id == p_id for p in self.pivots[symbol]):
                self.pivots[symbol].append(
                    ConfirmedPivot(
                        pivot_id=p_id,
                        pivot_type=PivotType.SWING_HIGH,
                        price=p_bar.high,
                        pivot_bar_time=p_bar.end_time,
                        confirmation_time=bar_right2.end_time,
                        is_consumed=False,
                    )
                )

        # Swing Low: strictly less than 2 before and 2 after
        is_swing_low = (
            p_bar.low < bar_left2.low
            and p_bar.low < bar_left1.low
            and p_bar.low < bar_right1.low
            and p_bar.low < bar_right2.low
        )
        if is_swing_low:
            p_id = f"PIVOT_L_{symbol}_{p_bar.end_time.strftime('%Y%m%d%H%M')}"
            if not any(p.pivot_id == p_id for p in self.pivots[symbol]):
                self.pivots[symbol].append(
                    ConfirmedPivot(
                        pivot_id=p_id,
                        pivot_type=PivotType.SWING_LOW,
                        price=p_bar.low,
                        pivot_bar_time=p_bar.end_time,
                        confirmation_time=bar_right2.end_time,
                        is_consumed=False,
                    )
                )

    def _get_latest_eligible_pivot(self, symbol: str, p_type: PivotType, before_time: datetime) -> Optional[ConfirmedPivot]:
        """
        Returns the latest unconsumed pivot confirmed strictly before the breach bar starts.
        """
        eligible = [
            p for p in self.pivots.get(symbol, [])
            if p.pivot_type == p_type
            and not p.is_consumed
            and p.confirmation_time <= before_time
        ]
        return eligible[-1] if eligible else None

    def _evaluate_sweeps(self, symbol: str, current_bar: Bar60m) -> None:
        """
        Evaluates ongoing breach candidates or new breaches.
        Breach bar = bar 1. Reclaim must happen within at most 3 bars.
        """
        history = self.bars_history[symbol]
        prev_bar = history[-2] if len(history) >= 2 else None

        # -------------------------------------------------------------
        # High Sweep Evaluation (Breach above High, reclaim below -> BEARISH)
        # -------------------------------------------------------------
        pending_h = self._pending_high_sweep.get(symbol)
        if pending_h:
            # Candidate is active; increment bar count
            pending_h["bars_count"] += 1
            ref_pivot = pending_h["pivot"]
            pending_h["extreme"] = max(pending_h["extreme"], current_bar.high)

            # Check reclaim: close strictly below reference high
            if current_bar.close < ref_pivot.price:
                # Successfully reclaimed within <= 3 bars!
                event = self._create_sweep_event(
                    symbol=symbol,
                    security_id=current_bar.security_id,
                    pivot=ref_pivot,
                    sweep_direction=ContextDirection.BEARISH,
                    breach_bar_time=pending_h["breach_time"],
                    confirmation_bar=current_bar,
                    reclaim_bars=pending_h["bars_count"],
                    sweep_extreme=pending_h["extreme"],
                )
                self.active_sweeps[symbol].append(event)
                ref_pivot.is_consumed = True
                self._pending_high_sweep[symbol] = None
            elif pending_h["bars_count"] >= 3:
                # Failed to reclaim in 3 bars; candidate expires and cannot be relabeled a sweep
                self._pending_high_sweep[symbol] = None
        else:
            # Look for fresh breach
            if prev_bar:
                ref_pivot = self._get_latest_eligible_pivot(symbol, PivotType.SWING_HIGH, current_bar.start_time)
                if ref_pivot:
                    # Condition: crossed above ref high from prior close at/below it
                    if prev_bar.close <= ref_pivot.price and current_bar.high > ref_pivot.price:
                        # Breach occurred!
                        if current_bar.close < ref_pivot.price:
                            # Immediate reclaim on same bar (bar 1 of 3)
                            event = self._create_sweep_event(
                                symbol=symbol,
                                security_id=current_bar.security_id,
                                pivot=ref_pivot,
                                sweep_direction=ContextDirection.BEARISH,
                                breach_bar_time=current_bar.end_time.isoformat(),
                                confirmation_bar=current_bar,
                                reclaim_bars=1,
                                sweep_extreme=current_bar.high,
                            )
                            self.active_sweeps[symbol].append(event)
                            ref_pivot.is_consumed = True
                        else:
                            # Armed candidate: pending reclaim within 3 bars
                            self._pending_high_sweep[symbol] = {
                                "pivot": ref_pivot,
                                "breach_time": current_bar.end_time.isoformat(),
                                "bars_count": 1,
                                "extreme": current_bar.high,
                            }

        # -------------------------------------------------------------
        # Low Sweep Evaluation (Breach below Low, reclaim above -> BULLISH)
        # -------------------------------------------------------------
        pending_l = self._pending_low_sweep.get(symbol)
        if pending_l:
            pending_l["bars_count"] += 1
            ref_pivot = pending_l["pivot"]
            pending_l["extreme"] = min(pending_l["extreme"], current_bar.low)

            # Check reclaim: close strictly above reference low
            if current_bar.close > ref_pivot.price:
                event = self._create_sweep_event(
                    symbol=symbol,
                    security_id=current_bar.security_id,
                    pivot=ref_pivot,
                    sweep_direction=ContextDirection.BULLISH,
                    breach_bar_time=pending_l["breach_time"],
                    confirmation_bar=current_bar,
                    reclaim_bars=pending_l["bars_count"],
                    sweep_extreme=pending_l["extreme"],
                )
                self.active_sweeps[symbol].append(event)
                ref_pivot.is_consumed = True
                self._pending_low_sweep[symbol] = None
            elif pending_l["bars_count"] >= 3:
                self._pending_low_sweep[symbol] = None
        else:
            if prev_bar:
                ref_pivot = self._get_latest_eligible_pivot(symbol, PivotType.SWING_LOW, current_bar.start_time)
                if ref_pivot:
                    if prev_bar.close >= ref_pivot.price and current_bar.low < ref_pivot.price:
                        if current_bar.close > ref_pivot.price:
                            event = self._create_sweep_event(
                                symbol=symbol,
                                security_id=current_bar.security_id,
                                pivot=ref_pivot,
                                sweep_direction=ContextDirection.BULLISH,
                                breach_bar_time=current_bar.end_time.isoformat(),
                                confirmation_bar=current_bar,
                                reclaim_bars=1,
                                sweep_extreme=current_bar.low,
                            )
                            self.active_sweeps[symbol].append(event)
                            ref_pivot.is_consumed = True
                        else:
                            self._pending_low_sweep[symbol] = {
                                "pivot": ref_pivot,
                                "breach_time": current_bar.end_time.isoformat(),
                                "bars_count": 1,
                                "extreme": current_bar.low,
                            }

    def _create_sweep_event(
        self,
        symbol: str,
        security_id: str,
        pivot: ConfirmedPivot,
        sweep_direction: ContextDirection,
        breach_bar_time: str,
        confirmation_bar: Bar60m,
        reclaim_bars: int,
        sweep_extreme: float,
    ) -> LiquidityContextEvent:
        """
        Creates and persists a confirmed LiquidityContextEvent.
        Expiry is 2 full 60-min intervals after confirmation, capped at session close (15:30).
        """
        conf_dt = confirmation_bar.end_time
        # TTL: 2 full 60-minute intervals
        ttl_dt = conf_dt + timedelta(hours=2)
        # Cap at regular market close (15:30) of same day
        session_close_dt = default_session.localize(
            datetime.combine(conf_dt.date(), default_session.market_close_time)
        )
        expiry_dt = min(ttl_dt, session_close_dt)

        raw = f"{symbol}:{pivot.pivot_id}:{conf_dt.isoformat()}:{self.rule_version}"
        event_id = f"SWEEP_{symbol}_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:10]}"

        event = LiquidityContextEvent(
            event_id=event_id,
            symbol=symbol.upper(),
            security_id=security_id,
            timeframe_minutes=60,
            pivot_type=pivot.pivot_type,
            level_price=pivot.price,
            breach_bar_time=breach_bar_time,
            confirmation_time=conf_dt.isoformat(),
            reclaim_bars=reclaim_bars,
            sweep_direction=sweep_direction,
            sweep_extreme=sweep_extreme,
            status=SweepStatus.ACTIVE,
            expiry_time=expiry_dt.isoformat(),
            rule_version=self.rule_version,
        )
        db.save_liquidity_context_event(event.to_dict())
        return event

    def _update_sweep_lifecycle(self, symbol: str, current_bar: Bar60m) -> None:
        """
        Updates TTL expiry and structural invalidation:
        - Bearish context invalidated if completed bar closes strictly ABOVE sweep extreme.
        - Bullish context invalidated if completed bar closes strictly BELOW sweep extreme.
        - Expired if current_bar.end_time >= expiry_time.
        """
        sweeps = self.active_sweeps.get(symbol, [])
        for sw in sweeps:
            if sw.status != SweepStatus.ACTIVE:
                continue

            # Invalidation check
            if sw.sweep_direction == ContextDirection.BEARISH and current_bar.close > sw.sweep_extreme:
                sw.status = SweepStatus.INVALIDATED
                sw.invalidation_time = current_bar.end_time.isoformat()
                db.update_liquidity_context_status(sw.event_id, "INVALIDATED", sw.invalidation_time)
                continue

            if sw.sweep_direction == ContextDirection.BULLISH and current_bar.close < sw.sweep_extreme:
                sw.status = SweepStatus.INVALIDATED
                sw.invalidation_time = current_bar.end_time.isoformat()
                db.update_liquidity_context_status(sw.event_id, "INVALIDATED", sw.invalidation_time)
                continue

            # Expiry check
            try:
                exp_dt = datetime.fromisoformat(sw.expiry_time)
                if current_bar.end_time >= exp_dt:
                    sw.status = SweepStatus.EXPIRED
                    db.update_liquidity_context_status(sw.event_id, "EXPIRED")
            except Exception:
                pass

    def get_current_context(self, symbol: str, check_time: Optional[datetime] = None) -> LiquidityContextSnapshot:
        """
        Returns current combined context direction:
        - BULLISH if only active bullish sweeps exist
        - BEARISH if only active bearish sweeps exist
        - CONFLICT if both active bullish and bearish sweeps exist
        - NONE if no active sweeps exist
        """
        sym = symbol.upper()
        now_dt = check_time or default_session.now()
        now_iso = now_dt.isoformat()

        # Filter active sweeps
        active = [
            sw for sw in self.active_sweeps.get(sym, [])
            if sw.status == SweepStatus.ACTIVE and sw.expiry_time > now_iso
        ]

        has_bullish = any(sw.sweep_direction == ContextDirection.BULLISH for sw in active)
        has_bearish = any(sw.sweep_direction == ContextDirection.BEARISH for sw in active)

        if has_bullish and has_bearish:
            direction = ContextDirection.CONFLICT
            reason = "Simultaneous active confirmed high and low sweeps (CONFLICT)"
        elif has_bullish:
            direction = ContextDirection.BULLISH
            reason = f"Active Bullish Low Sweep ({active[0].level_price})"
        elif has_bearish:
            direction = ContextDirection.BEARISH
            reason = f"Active Bearish High Sweep ({active[0].level_price})"
        else:
            direction = ContextDirection.NONE
            reason = "No confirmed liquidity sweep active"

        # Find latest confirmed high & low
        p_high = self._get_latest_eligible_pivot(sym, PivotType.SWING_HIGH, now_dt)
        p_low = self._get_latest_eligible_pivot(sym, PivotType.SWING_LOW, now_dt)

        sec_id = active[0].security_id if active else ""

        return LiquidityContextSnapshot(
            symbol=sym,
            security_id=sec_id,
            timestamp=now_iso,
            direction=direction,
            active_events=active,
            latest_confirmed_high=p_high.price if p_high else None,
            latest_confirmed_low=p_low.price if p_low else None,
            rule_version=self.rule_version,
            reason=reason,
        )


liquidity_context_engine = LiquidityContextEngine()
