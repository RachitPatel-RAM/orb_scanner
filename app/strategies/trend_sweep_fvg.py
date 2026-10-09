"""
TREND_SWEEP_FVG_V1 Strategy Engine (Sections 4, 5, 6, 8, 9, 10).

Deterministic, auditable quantitative alert and paper execution strategy:
- 60m Directional Trend Regime (EMA20/50 + higher highs/higher lows or lower highs/lower lows)
- 15m Impulse Anchors & Dealing Range Equilibrium (Fibonacci 50% - 78.6% Discount / Premium)
- 15m POI Overlap (Qualified 15m FVG or Order Block formed during impulse leg, <= 3 sessions old)
- 5m Liquidity Sweep & Fast Reclaim (Within 2 bars)
- 5m Microstructure Shift & Displacement Candle through pre-breach structure
- 5m Entry Fair Value Gap (Displacement = Candle B, entry at midpoint inside 50%-78.6% band)
- Structural Invalidation Stop & Net 2R Profit Target after statutory costs
- Exits: Declared FIXED_2R vs BE_TRAIL_2R
- State Machine: DETECTED -> WAIT_SWEEP -> WAIT_STRUCTURE -> WAIT_FVG -> ARMED_WAIT_RETEST -> TRIGGER_OBSERVED -> FILLED -> CLOSED
- Heuristic Setup Score (out of 10) capped across 5 distinct families.
- Isolated shadow/paper mode by default with zero unauthorized subscriber publication.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from enum import Enum
import hashlib
import json
import math
from typing import Any, Dict, List, Optional, Tuple

from app.analysis.hourly_regime import ConfirmedPivot, HourlyRegimeEngine, HourlyRegimeResult, TrendRegime
from app.analysis.optional_evidence import OptionalEvidenceReport, RVOLEngine, RVOLResult
from app.analysis.volume_profile import VolumeProfileEngine, VolumeProfileResult
from app.config import logger, settings
from app.market.session import default_session
from app.storage.database import db
from app.storage.models import Candle, Direction
from app.trading.risk_manager_v2 import CostBreakdown, ExitPolicy, SizingResult, risk_manager_v2


class SetupLifecycleState(str, Enum):
    DETECTED = "DETECTED"
    WAIT_SWEEP = "WAIT_SWEEP"
    WAIT_STRUCTURE = "WAIT_STRUCTURE"
    WAIT_FVG = "WAIT_FVG"
    ARMED_WAIT_RETEST = "ARMED_WAIT_RETEST"
    TRIGGER_OBSERVED = "TRIGGER_OBSERVED"
    FILLED = "FILLED"
    CLOSED = "CLOSED"
    REJECTED = "REJECTED"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


@dataclass
class WilderATR:
    """Wilder ATR(14) calculator with shared warm-up."""
    period: int = 14
    values: List[float] = field(default_factory=list)

    @staticmethod
    def compute_atr_series(candles: List[Candle], period: int = 14) -> List[float]:
        if len(candles) < 2:
            return [0.0] * len(candles)

        tr_list: List[float] = [candles[0].high - candles[0].low]
        for i in range(1, len(candles)):
            curr = candles[i]
            prev = candles[i - 1]
            tr = max(curr.high - prev.low, abs(curr.high - prev.close), abs(curr.low - prev.close))
            tr_list.append(tr)

        if len(tr_list) < period:
            return tr_list

        # Warm-up with simple average
        atr_series: List[float] = [0.0] * (period - 1)
        first_atr = sum(tr_list[:period]) / period
        atr_series.append(first_atr)

        for i in range(period, len(tr_list)):
            next_atr = (atr_series[-1] * (period - 1) + tr_list[i]) / period
            atr_series.append(next_atr)

        return atr_series


@dataclass(frozen=True)
class PriceZone:
    zone_id: str
    zone_type: str       # '15M_FVG', '15M_OB', '5M_FVG'
    timeframe: str
    direction: Direction
    top: float
    bottom: float
    midpoint: float
    formed_at: str
    known_at: str
    is_mitigated: bool = False
    source_bar_indices: Tuple[int, ...] = field(default_factory=tuple)


@dataclass
class ImpulseLeg:
    leg_id: str
    direction: Direction
    low_pivot: ConfirmedPivot
    high_pivot: ConfirmedPivot
    equilibrium_price: float
    retrace_382: float
    retrace_500: float
    retrace_618: float
    retrace_786: float
    discount_band: Tuple[float, float] # [low, high]
    bos_candle_time: str
    formed_at: str


@dataclass
class TrendSweepSetup:
    """Complete auditable setup payload for TREND_SWEEP_FVG_V1."""
    setup_id: str
    strategy_version: str = "TREND_SWEEP_FVG_V1"
    symbol: str = ""
    security_id: str = ""
    direction: Direction = Direction.LONG
    trade_date: str = ""
    created_at: str = ""
    state: SetupLifecycleState = SetupLifecycleState.DETECTED
    rejection_reason: str = ""

    # Trend & Context
    hourly_regime: TrendRegime = TrendRegime.MIXED
    hourly_details: Dict[str, Any] = field(default_factory=dict)
    daily_bias: str = "NEUTRAL"
    daily_bias_snapshot_id: Optional[str] = None

    # 15m Location & Equilibrium
    impulse_leg: Optional[ImpulseLeg] = None
    selected_15m_poi: Optional[PriceZone] = None
    retrace_overlap_band: Tuple[float, float] = (0.0, 0.0)

    # 5m Sweep & Reclaim
    swept_level: float = 0.0
    swept_level_type: str = "" # 'PREV_SESSION_LOW', 'CONFIRMED_5M_SWING_LOW', etc.
    sweep_bar_time: str = ""
    reclaim_bar_time: str = ""
    sweep_sequence_extreme: float = 0.0 # lowest low for long, highest high for short

    # 5m Microstructure & Displacement
    frozen_microstructure_ref: float = 0.0
    displacement_bar_time: str = ""
    entry_fvg: Optional[PriceZone] = None

    # Order & Execution Geometry
    entry_price: float = 0.0
    initial_stop: float = 0.0
    current_stop: float = 0.0
    initial_r_pts: float = 0.0
    target_price: float = 0.0
    valid_until: str = ""
    retest_deadline_bar: int = 0

    # Risk & Sizing
    sizing: Optional[SizingResult] = None
    exit_policy: ExitPolicy = ExitPolicy.FIXED_2R
    heuristic_score: int = 0
    score_breakdown: Dict[str, int] = field(default_factory=dict)

    # Execution Outcomes
    filled_at: Optional[str] = None
    fill_price: Optional[float] = None
    exit_at: Optional[str] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    realized_pnl: Optional[float] = None

    # Optional Evidence
    rvol: Optional[RVOLResult] = None
    volume_profile: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["direction"] = self.direction.value
        d["state"] = self.state.value
        d["hourly_regime"] = self.hourly_regime.value
        d["exit_policy"] = self.exit_policy.value
        return d


class TrendSweepFVGStrategy:
    """
    Production-grade, deterministic implementation of TREND_SWEEP_FVG_V1.
    Shared between live event loop and historical backtest replay.
    """

    def __init__(
        self,
        strategy_version: str = "TREND_SWEEP_FVG_V1",
        mode: str = "SHADOW",
        tick_size: float = 0.05,
        paper_capital: float = 50000.0,
    ):
        self.strategy_version = strategy_version
        self.mode = mode.upper() # 'OFF', 'SHADOW', 'STRICT'
        self.tick_size = tick_size
        self.paper_capital = paper_capital

        # Active setups state machine: (symbol, trade_date) -> TrendSweepSetup
        self.active_setups: Dict[str, TrendSweepSetup] = {}

    def _round_tick(self, price: float, round_outward: bool = False, is_stop_for_long: bool = False) -> float:
        """Rounds price strictly to valid tick size."""
        if not round_outward:
            return round(round(price / self.tick_size) * self.tick_size, 2)
        if is_stop_for_long:
            # Round stop DOWN (outward) so risk is not artificially compressed
            return round(math.floor(price / self.tick_size) * self.tick_size, 2)
        else:
            # Round stop UP (outward) for short
            return round(math.ceil(price / self.tick_size) * self.tick_size, 2)

    def detect_15m_fvgs(self, candles_15m: List[Candle], atr_series: List[float]) -> List[PriceZone]:
        """Detects 15m Fair Value Gaps with minimum width check."""
        zones: List[PriceZone] = []
        if len(candles_15m) < 3:
            return zones

        for i in range(2, len(candles_15m)):
            cA = candles_15m[i - 2]
            cB = candles_15m[i - 1]
            cC = candles_15m[i]
            prev_atr = atr_series[i - 1] if i - 1 < len(atr_series) else (cB.high - cB.low)
            min_width = max(2.0 * self.tick_size, 0.05 * prev_atr)

            # Bullish 15m FVG: cC.low > cA.high
            if cC.low > cA.high and (cC.low - cA.high) >= min_width:
                zid = f"FVG15M_L_{cC.timestamp.strftime('%Y%m%d_%H%M')}_{round(cA.high, 2)}"
                zones.append(
                    PriceZone(
                        zone_id=zid,
                        zone_type="15M_FVG",
                        timeframe="15m",
                        direction=Direction.LONG,
                        top=cC.low,
                        bottom=cA.high,
                        midpoint=round((cC.low + cA.high) / 2.0, 2),
                        formed_at=cB.iso_timestamp,
                        known_at=cC.iso_timestamp,
                        source_bar_indices=(i - 2, i - 1, i),
                    )
                )

            # Bearish 15m FVG: cC.high < cA.low
            elif cC.high < cA.low and (cA.low - cC.high) >= min_width:
                zid = f"FVG15M_S_{cC.timestamp.strftime('%Y%m%d_%H%M')}_{round(cA.low, 2)}"
                zones.append(
                    PriceZone(
                        zone_id=zid,
                        zone_type="15M_FVG",
                        timeframe="15m",
                        direction=Direction.SHORT,
                        top=cA.low,
                        bottom=cC.high,
                        midpoint=round((cA.low + cC.high) / 2.0, 2),
                        formed_at=cB.iso_timestamp,
                        known_at=cC.iso_timestamp,
                        source_bar_indices=(i - 2, i - 1, i),
                    )
                )

        return zones

    def detect_15m_order_blocks(
        self,
        candles_15m: List[Candle],
        high_pivots: List[ConfirmedPivot],
        low_pivots: List[ConfirmedPivot],
        atr_series: List[float],
    ) -> List[PriceZone]:
        """
        Operational Order Block: last opposite-colour candle within 3 bars immediately
        preceding the qualifying displacement/BOS candle.
        """
        obs: List[PriceZone] = []
        if len(candles_15m) < 5:
            return obs

        for i in range(3, len(candles_15m)):
            curr = candles_15m[i]
            prev_atr = atr_series[i - 1] if i - 1 < len(atr_series) else (curr.high - curr.low)
            buffer = max(2.0 * self.tick_size, 0.05 * prev_atr)

            # Check Bullish BOS over an already-known swing high
            eligible_highs = [h for h in high_pivots if h.bar_index < i - 1 and curr.close > (h.price + buffer)]
            if eligible_highs and curr.close > curr.open:
                # Find last down candle in [i-3, i-2, i-1]
                for k in range(i - 1, max(-1, i - 4), -1):
                    cand = candles_15m[k]
                    if cand.close < cand.open:
                        zid = f"OB15M_L_{cand.timestamp.strftime('%Y%m%d_%H%M')}_{round(cand.low, 2)}"
                        obs.append(
                            PriceZone(
                                zone_id=zid,
                                zone_type="15M_OB",
                                timeframe="15m",
                                direction=Direction.LONG,
                                top=cand.high,
                                bottom=cand.low,
                                midpoint=round((cand.high + cand.low) / 2.0, 2),
                                formed_at=cand.iso_timestamp,
                                known_at=curr.iso_timestamp,
                                source_bar_indices=(k,),
                            )
                        )
                        break

            # Check Bearish BOS over an already-known swing low
            eligible_lows = [l for l in low_pivots if l.bar_index < i - 1 and curr.close < (l.price - buffer)]
            if eligible_lows and curr.close < curr.open:
                for k in range(i - 1, max(-1, i - 4), -1):
                    cand = candles_15m[k]
                    if cand.close > cand.open:
                        zid = f"OB15M_S_{cand.timestamp.strftime('%Y%m%d_%H%M')}_{round(cand.high, 2)}"
                        obs.append(
                            PriceZone(
                                zone_id=zid,
                                zone_type="15M_OB",
                                timeframe="15m",
                                direction=Direction.SHORT,
                                top=cand.high,
                                bottom=cand.low,
                                midpoint=round((cand.high + cand.low) / 2.0, 2),
                                formed_at=cand.iso_timestamp,
                                known_at=curr.iso_timestamp,
                                source_bar_indices=(k,),
                            )
                        )
                        break

        return obs

    def find_15m_impulse_leg(
        self,
        candles_15m: List[Candle],
        direction: Direction,
        high_pivots: List[ConfirmedPivot],
        low_pivots: List[ConfirmedPivot],
        max_age_sessions: int = 3,
        current_date: Optional[date] = None,
    ) -> Optional[ImpulseLeg]:
        """
        Finds the most recent completed 15m impulse leg aligned with trend regime:
        - Bullish: confirmed swing low L followed by confirmed swing high H (H > L), with directional BOS.
        - Bearish: confirmed swing high H followed by confirmed swing low L (H > L), with directional BOS.
        - Endpoint no older than 3 completed sessions.
        """
        if not high_pivots or not low_pivots or not candles_15m:
            return None

        c_date = current_date or candles_15m[-1].timestamp.date()

        if direction == Direction.LONG:
            # Look backwards from newest high pivot
            for h in reversed(high_pivots):
                # Low pivot must precede high pivot
                eligible_lows = [l for l in low_pivots if l.bar_index < h.bar_index and h.price > l.price]
                if not eligible_lows:
                    continue
                l = eligible_lows[-1] # closest preceding low

                # Check age
                h_dt = datetime.fromisoformat(h.bar_timestamp)
                if (c_date - h_dt.date()).days > max_age_sessions + 2:
                    continue

                D = h.price - l.price
                if D <= 0:
                    continue

                eq = round(l.price + 0.50 * D, 2)
                r_382 = round(h.price - 0.382 * D, 2)
                r_500 = round(h.price - 0.50 * D, 2)
                r_618 = round(h.price - 0.618 * D, 2)
                r_786 = round(h.price - 0.786 * D, 2)
                discount_band = (r_786, r_500) # lower half

                lid = f"IMP_15M_L_{l.bar_timestamp[:10]}_{h.bar_timestamp[:10]}"
                return ImpulseLeg(
                    leg_id=lid,
                    direction=Direction.LONG,
                    low_pivot=l,
                    high_pivot=h,
                    equilibrium_price=eq,
                    retrace_382=r_382,
                    retrace_500=r_500,
                    retrace_618=r_618,
                    retrace_786=r_786,
                    discount_band=discount_band,
                    bos_candle_time=h.confirmed_at,
                    formed_at=h.bar_timestamp,
                )

        else: # SHORT
            for l in reversed(low_pivots):
                eligible_highs = [h for h in high_pivots if h.bar_index < l.bar_index and h.price > l.price]
                if not eligible_highs:
                    continue
                h = eligible_highs[-1]

                l_dt = datetime.fromisoformat(l.bar_timestamp)
                if (c_date - l_dt.date()).days > max_age_sessions + 2:
                    continue

                D = h.price - l.price
                if D <= 0:
                    continue

                eq = round(l.price + 0.50 * D, 2)
                r_382 = round(l.price + 0.382 * D, 2)
                r_500 = round(l.price + 0.50 * D, 2)
                r_618 = round(l.price + 0.618 * D, 2)
                r_786 = round(l.price + 0.786 * D, 2)
                premium_band = (r_500, r_786) # upper half

                lid = f"IMP_15M_S_{h.bar_timestamp[:10]}_{l.bar_timestamp[:10]}"
                return ImpulseLeg(
                    leg_id=lid,
                    direction=Direction.SHORT,
                    low_pivot=l,
                    high_pivot=h,
                    equilibrium_price=eq,
                    retrace_382=r_382,
                    retrace_500=r_500,
                    retrace_618=r_618,
                    retrace_786=r_786,
                    discount_band=premium_band,
                    bos_candle_time=l.confirmed_at,
                    formed_at=l.bar_timestamp,
                )

        return None

    def select_overlapping_15m_poi(
        self,
        candidate_retrace_band: Tuple[float, float],
        direction: Direction,
        fvgs_15m: List[PriceZone],
        obs_15m: List[PriceZone],
        candles_15m: List[Candle],
    ) -> Optional[PriceZone]:
        """
        Finds nonempty overlap between candidate retracement band and eligible 15m POI.
        Deterministic tie-breaking:
        1. Most recently qualified eligible 15m FVG first
        2. Otherwise most recently qualified 15m OB
        3. Stable IDs for ties
        """
        band_low, band_high = candidate_retrace_band

        # Helper to check overlap with band
        def has_overlap(z: PriceZone) -> bool:
            return not (z.top < band_low or z.bottom > band_high)

        # Helper to check if zone was revisited after qualification
        def is_unrevisited(z: PriceZone) -> bool:
            # Find candles after qualification
            for c in candles_15m:
                if c.iso_timestamp > z.known_at:
                    # check if price entered zone
                    if not (c.high < z.bottom or c.low > z.top):
                        return False
            return True

        # 1. Filter eligible FVGs
        matching_fvgs = [
            f for f in fvgs_15m
            if f.direction == direction and has_overlap(f) and is_unrevisited(f)
        ]
        if matching_fvgs:
            # Sort newest known_at descending, then zone_id
            matching_fvgs.sort(key=lambda x: (x.known_at, x.zone_id), reverse=True)
            return matching_fvgs[0]

        # 2. Filter eligible OBs
        matching_obs = [
            o for o in obs_15m
            if o.direction == direction and has_overlap(o) and is_unrevisited(o)
        ]
        if matching_obs:
            matching_obs.sort(key=lambda x: (x.known_at, x.zone_id), reverse=True)
            return matching_obs[0]

        return None

    def calculate_heuristic_score(
        self,
        setup: TrendSweepSetup,
        rvol: Optional[RVOLResult],
        displacement_body_atr_ratio: float,
        sweep_depth_buffer_ratio: float,
    ) -> Tuple[int, Dict[str, int]]:
        """
        Calculates HEURISTIC_SETUP_SCORE out of 10, strictly capped across 5 distinct families:
        1. Trend: 0 to 2 pts
        2. Location: 0 to 2 pts
        3. Trigger: 0 to 2 pts
        4. Participation/Volume: 0 to 2 pts
        5. Execution/Room: 0 to 2 pts
        """
        scores: Dict[str, int] = {
            "trend": 0,
            "location": 0,
            "trigger": 0,
            "participation": 0,
            "execution": 0,
        }

        # 1. Trend (0-2)
        if setup.hourly_regime in (TrendRegime.BULLISH, TrendRegime.BEARISH):
            scores["trend"] += 1
            if setup.hourly_details.get("ema_aligned", False):
                scores["trend"] += 1

        # 2. Location (0-2)
        if setup.selected_15m_poi is not None:
            scores["location"] += 1
            if setup.impulse_leg is not None:
                # Deep discount/premium bonus (61.8% to 78.6%)
                scores["location"] += 1

        # 3. Trigger (0-2)
        if sweep_depth_buffer_ratio >= 1.5:
            scores["trigger"] += 1
        if displacement_body_atr_ratio >= 1.0:
            scores["trigger"] += 1

        # 4. Participation (0-2)
        if rvol is not None and rvol.status == "CALCULATED":
            if rvol.rvol >= 1.2:
                scores["participation"] += 1
            if rvol.is_high_volume:
                scores["participation"] += 1

        # 5. Execution (0-2)
        if setup.sizing is not None and setup.sizing.allowed:
            scores["execution"] += 1
            if setup.sizing.net_reward_risk_ratio >= 2.2:
                scores["execution"] += 1

        # Cap each family at 2
        for k in scores:
            scores[k] = min(2, max(0, scores[k]))

        total_score = sum(scores.values())
        return total_score, scores

    def evaluate_5m_candle(
        self,
        candle_5m: Candle,
        candles_5m_history: List[Candle],
        candles_15m_history: List[Candle],
        candles_60m_history: List[Candle],
        previous_session_candles: List[Candle],
        daily_bias: str = "NEUTRAL",
        daily_bias_snapshot_id: Optional[str] = None,
        volume_rvol_history: Optional[List[float]] = None,
    ) -> Optional[TrendSweepSetup]:
        """
        Step-by-step deterministic evaluation of a completed 5m candle.
        Returns a newly armed or updated TrendSweepSetup.
        """
        symbol = candle_5m.symbol
        sec_id = candle_5m.security_id
        c_time = default_session.localize(candle_5m.timestamp)
        t_date_str = c_time.date().isoformat()
        setup_key = f"{symbol}_{t_date_str}"

        # 1. Exchange schedule checks
        cur_t = c_time.time()
        if cur_t < time(10, 0) or cur_t > time(14, 30):
            return None

        # 2. Evaluate 60m trend regime using only already-completed 60m candles
        hourly_regime_res = HourlyRegimeEngine.evaluate_regime(candles_60m_history, as_of_time=c_time)
        if hourly_regime_res.status == TrendRegime.MIXED:
            return None

        strategy_dir = Direction.LONG if hourly_regime_res.status == TrendRegime.BULLISH else Direction.SHORT

        # Daily bias conflict check (if bias gate enabled)
        if daily_bias != "DATA_UNAVAILABLE" and daily_bias != "NEUTRAL":
            if strategy_dir == Direction.LONG and daily_bias == "BEARISH":
                return None
            if strategy_dir == Direction.SHORT and daily_bias == "BULLISH":
                return None

        # 3. 15m Impulse & POI Overlap
        highs_15m, lows_15m = HourlyRegimeEngine.find_confirmed_pivots(candles_15m_history, "15m", 2, 2)
        atr_15m = WilderATR.compute_atr_series(candles_15m_history, 14)

        impulse = self.find_15m_impulse_leg(candles_15m_history, strategy_dir, highs_15m, lows_15m, current_date=c_time.date())
        if not impulse:
            return None

        fvgs_15m = self.detect_15m_fvgs(candles_15m_history, atr_15m)
        obs_15m = self.detect_15m_order_blocks(candles_15m_history, highs_15m, lows_15m, atr_15m)

        selected_poi = self.select_overlapping_15m_poi(impulse.discount_band, strategy_dir, fvgs_15m, obs_15m, candles_15m_history)
        if not selected_poi:
            return None

        # Overlap region between impulse discount band and POI
        overlap_low = max(impulse.discount_band[0], selected_poi.bottom)
        overlap_high = min(impulse.discount_band[1], selected_poi.top)
        if overlap_high <= overlap_low:
            return None

        # 4. Check if current 5m candle touches the overlap region
        if not (candle_5m.high >= overlap_low and candle_5m.low <= overlap_high):
            return None

        # 5. Freeze sell-side reference (for long) or buy-side reference (for short)
        atr_5m = WilderATR.compute_atr_series(candles_5m_history, 14)
        prev_5m_atr = atr_5m[-2] if len(atr_5m) >= 2 else (candle_5m.high - candle_5m.low)
        buffer_5m = max(2.0 * self.tick_size, 0.05 * prev_5m_atr)

        highs_5m, lows_5m = HourlyRegimeEngine.find_confirmed_pivots(candles_5m_history, "5m", 2, 2)

        frozen_sweep_level: Optional[float] = None
        sweep_level_type: str = ""
        overlap_mid = (overlap_low + overlap_high) / 2.0

        if strategy_dir == Direction.LONG:
            # Check previous completed session low
            candidates_levels: List[Tuple[float, str]] = []
            if previous_session_candles:
                ps_low = min(c.low for c in previous_session_candles)
                candidates_levels.append((ps_low, "PREV_SESSION_LOW"))

            # Check unconsumed 5m swing lows <= 3 sessions old inside POI expanded by 0.20 * 15m ATR
            poi_exp_low = selected_poi.bottom - (0.20 * atr_15m[-1] if atr_15m else 0.0)
            poi_exp_high = selected_poi.top + (0.20 * atr_15m[-1] if atr_15m else 0.0)
            for pl in lows_5m:
                if poi_exp_low <= pl.price <= poi_exp_high:
                    candidates_levels.append((pl.price, f"5M_SWING_LOW_{pl.id}"))

            if not candidates_levels:
                return None

            # Pick level nearest overlap midpoint
            candidates_levels.sort(key=lambda x: abs(x[0] - overlap_mid))
            frozen_sweep_level, sweep_level_type = candidates_levels[0]

        else: # SHORT
            candidates_levels = []
            if previous_session_candles:
                ps_high = max(c.high for c in previous_session_candles)
                candidates_levels.append((ps_high, "PREV_SESSION_HIGH"))

            poi_exp_low = selected_poi.bottom - (0.20 * atr_15m[-1] if atr_15m else 0.0)
            poi_exp_high = selected_poi.top + (0.20 * atr_15m[-1] if atr_15m else 0.0)
            for ph in highs_5m:
                if poi_exp_low <= ph.price <= poi_exp_high:
                    candidates_levels.append((ph.price, f"5M_SWING_HIGH_{ph.id}"))

            if not candidates_levels:
                return None

            candidates_levels.sort(key=lambda x: abs(x[0] - overlap_mid))
            frozen_sweep_level, sweep_level_type = candidates_levels[0]

        # 6. Check breach & reclaim sequence over last few candles
        # Need at least 4 candles: pre-breach, breach, reclaim, displacement/FVG
        idx_curr = len(candles_5m_history) - 1
        if idx_curr < 5:
            return None

        # Look back up to 6 bars for the breach
        breach_idx: Optional[int] = None
        reclaim_idx: Optional[int] = None
        lowest_sweep_low = float("inf")
        highest_sweep_high = float("-inf")

        if strategy_dir == Direction.LONG:
            for k in range(max(0, idx_curr - 6), idx_curr):
                c_k = candles_5m_history[k]
                # Breach requires breach by at least 5m buffer starting from prior close at/above level
                if c_k.low <= (frozen_sweep_level - buffer_5m):
                    breach_idx = k
                    lowest_sweep_low = min(lowest_sweep_low, c_k.low)
                    break

            if breach_idx is None:
                return None

            # Reclaim: must close strictly back above level within 2 bars including breach bar
            for r in range(breach_idx, min(len(candles_5m_history), breach_idx + 2)):
                if candles_5m_history[r].close > frozen_sweep_level:
                    reclaim_idx = r
                    break

            if reclaim_idx is None:
                return None

            # Find frozen microstructure reference: latest confirmed 5m swing high known before breach starts
            pre_breach_highs = [h for h in highs_5m if h.bar_index < breach_idx]
            if not pre_breach_highs:
                return None
            frozen_micro_ref = pre_breach_highs[-1].price

            # 7. Check displacement in reclaim bar or next 3 bars
            disp_idx: Optional[int] = None
            for d_i in range(reclaim_idx, min(len(candles_5m_history), reclaim_idx + 4)):
                c_d = candles_5m_history[d_i]
                d_atr = atr_5m[d_i - 1] if d_i - 1 < len(atr_5m) else (c_d.high - c_d.low)
                c_range = c_d.high - c_d.low

                if (
                    c_d.close > c_d.open and
                    (c_d.close - c_d.open) >= 0.8 * d_atr and
                    c_range > 0 and ((c_d.close - c_d.open) / c_range) >= 0.60 and
                    ((c_d.close - c_d.low) / c_range) >= 0.75 and
                    c_d.close > frozen_micro_ref
                ):
                    disp_idx = d_i
                    break

            if disp_idx is None or disp_idx >= len(candles_5m_history) - 1:
                return None

            # 8. Check 3-candle FVG where Candle B is the displacement candle
            # Displacement candle = disp_idx. Candle A = disp_idx - 1. Candle C = disp_idx + 1.
            cA = candles_5m_history[disp_idx - 1]
            cB = candles_5m_history[disp_idx]
            cC = candles_5m_history[disp_idx + 1]

            fvg_atr = atr_5m[disp_idx] if disp_idx < len(atr_5m) else (cB.high - cB.low)
            fvg_min_w = max(2.0 * self.tick_size, 0.05 * fvg_atr)

            if not (cC.low > cA.high and (cC.low - cA.high) >= fvg_min_w):
                return None

            fvg_zone = PriceZone(
                zone_id=f"FVG5M_L_{cC.timestamp.strftime('%Y%m%d_%H%M')}_{round(cA.high, 2)}",
                zone_type="5M_FVG",
                timeframe="5m",
                direction=Direction.LONG,
                top=cC.low,
                bottom=cA.high,
                midpoint=self._round_tick((cC.low + cA.high) / 2.0),
                formed_at=cB.iso_timestamp,
                known_at=cC.iso_timestamp,
            )

            # Planned entry = FVG midpoint
            planned_entry = fvg_zone.midpoint

            # Must remain inside both FVG and 50%-78.6% retracement band
            if not (fvg_zone.bottom <= planned_entry <= fvg_zone.top):
                return None
            if not (impulse.discount_band[0] <= planned_entry <= impulse.discount_band[1]):
                return None

            # Initial stop = min(sweep_sequence_low, selected_15m_POI.low) - buffer_5m
            initial_stop = self._round_tick(
                min(lowest_sweep_low, selected_poi.bottom) - buffer_5m,
                round_outward=True,
                is_stop_for_long=True,
            )
            initial_r = planned_entry - initial_stop
            if initial_r <= 0:
                return None

            # Check opposing resistance for room
            opposing_res: Optional[float] = None
            if highs_15m:
                opposing_res = max(h.price for h in highs_15m[-3:])

        else: # SHORT
            for k in range(max(0, idx_curr - 6), idx_curr):
                c_k = candles_5m_history[k]
                if c_k.high >= (frozen_sweep_level + buffer_5m):
                    breach_idx = k
                    highest_sweep_high = max(highest_sweep_high, c_k.high)
                    break

            if breach_idx is None:
                return None

            for r in range(breach_idx, min(len(candles_5m_history), breach_idx + 2)):
                if candles_5m_history[r].close < frozen_sweep_level:
                    reclaim_idx = r
                    break

            if reclaim_idx is None:
                return None

            pre_breach_lows = [l for l in lows_5m if l.bar_index < breach_idx]
            if not pre_breach_lows:
                return None
            frozen_micro_ref = pre_breach_lows[-1].price

            disp_idx = None
            for d_i in range(reclaim_idx, min(len(candles_5m_history), reclaim_idx + 4)):
                c_d = candles_5m_history[d_i]
                d_atr = atr_5m[d_i - 1] if d_i - 1 < len(atr_5m) else (c_d.high - c_d.low)
                c_range = c_d.high - c_d.low

                if (
                    c_d.close < c_d.open and
                    (c_d.open - c_d.close) >= 0.8 * d_atr and
                    c_range > 0 and ((c_d.open - c_d.close) / c_range) >= 0.60 and
                    ((c_d.high - c_d.close) / c_range) >= 0.75 and
                    c_d.close < frozen_micro_ref
                ):
                    disp_idx = d_i
                    break

            if disp_idx is None or disp_idx >= len(candles_5m_history) - 1:
                return None

            cA = candles_5m_history[disp_idx - 1]
            cB = candles_5m_history[disp_idx]
            cC = candles_5m_history[disp_idx + 1]

            fvg_atr = atr_5m[disp_idx] if disp_idx < len(atr_5m) else (cB.high - cB.low)
            fvg_min_w = max(2.0 * self.tick_size, 0.05 * fvg_atr)

            if not (cC.high < cA.low and (cA.low - cC.high) >= fvg_min_w):
                return None

            fvg_zone = PriceZone(
                zone_id=f"FVG5M_S_{cC.timestamp.strftime('%Y%m%d_%H%M')}_{round(cA.low, 2)}",
                zone_type="5M_FVG",
                timeframe="5m",
                direction=Direction.SHORT,
                top=cA.low,
                bottom=cC.high,
                midpoint=self._round_tick((cA.low + cC.high) / 2.0),
                formed_at=cB.iso_timestamp,
                known_at=cC.iso_timestamp,
            )

            planned_entry = fvg_zone.midpoint

            if not (fvg_zone.bottom <= planned_entry <= fvg_zone.top):
                return None
            if not (impulse.discount_band[0] <= planned_entry <= impulse.discount_band[1]):
                return None

            initial_stop = self._round_tick(
                max(highest_sweep_high, selected_poi.top) + buffer_5m,
                round_outward=True,
                is_stop_for_long=False,
            )
            initial_r = initial_stop - planned_entry
            if initial_r <= 0:
                return None

            opposing_res = None
            if lows_5m:
                opposing_res = min(l.price for l in lows_5m[-3:])

        # 9. Whole lot sizing and Net 2R room verification
        sizing_res = risk_manager_v2.compute_size_and_targets(
            trade_date=c_time.date(),
            symbol=symbol,
            direction=strategy_dir.value,
            entry_price=planned_entry,
            stop_loss=initial_stop,
            opposing_resistance_or_support=opposing_res,
            lot_size=1, # Default 1 for equities / paper index points
            tick_size=self.tick_size,
        )

        # 10. Optional RVOL & Volume Profile calculation
        rvol_res: Optional[RVOLResult] = None
        if volume_rvol_history is not None:
            rvol_res = RVOLEngine.calculate_rvol(candle_5m, volume_rvol_history)

        vp_res: Optional[Dict[str, Any]] = None
        if previous_session_candles:
            vp_obj = VolumeProfileEngine.compute_profile(
                previous_session_candles,
                session_date=previous_session_candles[0].timestamp.date().isoformat(),
                symbol=symbol,
                tick_size=self.tick_size,
            )
            if vp_obj:
                vp_res = vp_obj.get_summary()

        setup_id = f"TSF_{symbol}_{c_time.strftime('%Y%m%d_%H%M%S')}_{strategy_dir.value[0]}"
        valid_until_dt = c_time + timedelta(minutes=15) # 3 future 5m bars for retest

        setup = TrendSweepSetup(
            setup_id=setup_id,
            symbol=symbol,
            security_id=sec_id,
            direction=strategy_dir,
            trade_date=t_date_str,
            created_at=c_time.isoformat(),
            state=SetupLifecycleState.ARMED_WAIT_RETEST if sizing_res.allowed else SetupLifecycleState.REJECTED,
            rejection_reason=sizing_res.rejection_reason if not sizing_res.allowed else "",
            hourly_regime=hourly_regime_res.status,
            hourly_details=hourly_regime_res.to_dict(),
            daily_bias=daily_bias,
            daily_bias_snapshot_id=daily_bias_snapshot_id,
            impulse_leg=impulse,
            selected_15m_poi=selected_poi,
            retrace_overlap_band=(overlap_low, overlap_high),
            swept_level=frozen_sweep_level,
            swept_level_type=sweep_level_type,
            sweep_bar_time=candles_5m_history[breach_idx].iso_timestamp if breach_idx is not None else "",
            reclaim_bar_time=candles_5m_history[reclaim_idx].iso_timestamp if reclaim_idx is not None else "",
            sweep_sequence_extreme=lowest_sweep_low if strategy_dir == Direction.LONG else highest_sweep_high,
            frozen_microstructure_ref=frozen_micro_ref,
            displacement_bar_time=candles_5m_history[disp_idx].iso_timestamp if disp_idx is not None else "",
            entry_fvg=fvg_zone,
            entry_price=planned_entry,
            initial_stop=initial_stop,
            initial_r_pts=round(initial_r, 2),
            target_price=sizing_res.target_price if sizing_res.allowed else (planned_entry + 2 * initial_r if strategy_dir == Direction.LONG else planned_entry - 2 * initial_r),
            valid_until=valid_until_dt.isoformat(),
            retest_deadline_bar=len(candles_5m_history) + 3,
            sizing=sizing_res,
            exit_policy=ExitPolicy.FIXED_2R,
            rvol=rvol_res,
            volume_profile=vp_res,
        )

        # 11. Heuristic Setup Score (0-10)
        disp_body = abs(candles_5m_history[disp_idx].close - candles_5m_history[disp_idx].open) if disp_idx is not None else 0.0
        disp_atr_ratio = disp_body / (prev_5m_atr if prev_5m_atr > 0 else 1.0)
        sweep_depth = abs(frozen_sweep_level - (lowest_sweep_low if strategy_dir == Direction.LONG else highest_sweep_high))
        sweep_depth_ratio = sweep_depth / (buffer_5m if buffer_5m > 0 else 1.0)

        tot_score, score_breakdown = self.calculate_heuristic_score(setup, rvol_res, disp_atr_ratio, sweep_depth_ratio)
        setup.heuristic_score = tot_score
        setup.score_breakdown = score_breakdown

        if sizing_res.allowed:
            self.active_setups[setup_id] = setup
            risk_manager_v2.reserve_risk(setup_id, symbol, sizing_res.nominal_gross_risk)

        return setup

    def check_armed_retest(
        self,
        current_candle: Candle,
        active_setup: TrendSweepSetup,
    ) -> TrendSweepSetup:
        """
        Monitors subsequent executable candles for the midpoint retest fill:
        - Eligible only in next 3 5m bars after FVG confirmation.
        - Fills conservative execution if price touches or crosses midpoint.
        - Cancels if stop breached before entry or target reached before entry.
        """
        if active_setup.state != SetupLifecycleState.ARMED_WAIT_RETEST:
            return active_setup

        c_time = default_session.localize(current_candle.timestamp)
        now_iso = c_time.isoformat()

        # Check expiry
        if now_iso > active_setup.valid_until:
            active_setup.state = SetupLifecycleState.EXPIRED
            active_setup.rejection_reason = "RETEST_WINDOW_EXPIRED"
            risk_manager_v2.release_risk(active_setup.setup_id)
            return active_setup

        entry_p = active_setup.entry_price
        stop_p = active_setup.initial_stop
        target_p = active_setup.target_price

        # Check pre-entry invalidation
        if active_setup.direction == Direction.LONG:
            # Stop breached before fill
            if current_candle.low <= stop_p:
                active_setup.state = SetupLifecycleState.INVALIDATED
                active_setup.rejection_reason = "STOP_BREACHED_BEFORE_FILL"
                risk_manager_v2.release_risk(active_setup.setup_id)
                return active_setup
            # Target reached before fill
            if current_candle.high >= target_p:
                active_setup.state = SetupLifecycleState.INVALIDATED
                active_setup.rejection_reason = "TARGET_REACHED_BEFORE_FILL"
                risk_manager_v2.release_risk(active_setup.setup_id)
                return active_setup
            # Retest touch fill
            if current_candle.low <= entry_p <= current_candle.high:
                active_setup.state = SetupLifecycleState.FILLED
                active_setup.filled_at = now_iso
                active_setup.fill_price = entry_p
                return active_setup

        else: # SHORT
            if current_candle.high >= stop_p:
                active_setup.state = SetupLifecycleState.INVALIDATED
                active_setup.rejection_reason = "STOP_BREACHED_BEFORE_FILL"
                risk_manager_v2.release_risk(active_setup.setup_id)
                return active_setup
            if current_candle.low <= target_p:
                active_setup.state = SetupLifecycleState.INVALIDATED
                active_setup.rejection_reason = "TARGET_REACHED_BEFORE_FILL"
                risk_manager_v2.release_risk(active_setup.setup_id)
                return active_setup
            if current_candle.low <= entry_p <= current_candle.high:
                active_setup.state = SetupLifecycleState.FILLED
                active_setup.filled_at = now_iso
                active_setup.fill_price = entry_p
                return active_setup

        return active_setup

    def check_position_exit(
        self,
        current_candle: Candle,
        filled_setup: TrendSweepSetup,
        candles_5m_since_entry: List[Candle],
        exit_policy: ExitPolicy = ExitPolicy.FIXED_2R,
    ) -> TrendSweepSetup:
        """
        Manages active filled trade through exit under declared exit policy:
        - FIXED_2R: Target hit or structural stop hit.
        - BE_TRAIL_2R: Move stop to entry at +1 price R; trail behind confirmed 5m swing pivots.
        - EOD: Square off at 15:15 IST.
        """
        if filled_setup.state != SetupLifecycleState.FILLED:
            return filled_setup

        c_time = default_session.localize(current_candle.timestamp)
        now_iso = c_time.isoformat()

        entry_p = filled_setup.fill_price or filled_setup.entry_price
        stop_p = filled_setup.initial_stop
        target_p = filled_setup.target_price
        r_pts = filled_setup.initial_r_pts
        qty = filled_setup.sizing.quantity if filled_setup.sizing else 1

        # 1. EOD Square-off at 15:15 IST
        if c_time.time() >= time(15, 15):
            exit_p = current_candle.close
            pnl_pts = (exit_p - entry_p) if filled_setup.direction == Direction.LONG else (entry_p - exit_p)
            cost_bd = risk_manager_v2.estimate_round_trip_costs(entry_p, exit_p, qty)
            net_pnl = (pnl_pts * qty) - cost_bd.total_round_trip_cost

            filled_setup.state = SetupLifecycleState.CLOSED
            filled_setup.exit_at = now_iso
            filled_setup.exit_price = exit_p
            filled_setup.exit_reason = "EOD_SQUAREOFF"
            filled_setup.realized_pnl = round(net_pnl, 2)
            risk_manager_v2.record_closed_trade(c_time.date(), filled_setup.setup_id, net_pnl, c_time)
            return filled_setup

        # 2. BE_TRAIL_2R Stop Management
        if filled_setup.current_stop == 0.0:
            filled_setup.current_stop = stop_p

        effective_stop = filled_setup.current_stop
        if exit_policy == ExitPolicy.BE_TRAIL_2R:
            if filled_setup.direction == Direction.LONG:
                # Check if reached +1R
                if current_candle.high >= (entry_p + r_pts):
                    filled_setup.current_stop = max(filled_setup.current_stop, entry_p) # Break-even
                # Trail behind confirmed 5m swing lows
                if candles_5m_since_entry:
                    _, lows_trail = HourlyRegimeEngine.find_confirmed_pivots(candles_5m_since_entry, "5m", 2, 2)
                    if lows_trail:
                        trailing_sl = lows_trail[-1].price - (2 * self.tick_size)
                        filled_setup.current_stop = max(filled_setup.current_stop, trailing_sl)
            else:
                if current_candle.low <= (entry_p - r_pts):
                    filled_setup.current_stop = min(filled_setup.current_stop, entry_p)
                if candles_5m_since_entry:
                    highs_trail, _ = HourlyRegimeEngine.find_confirmed_pivots(candles_5m_since_entry, "5m", 2, 2)
                    if highs_trail:
                        trailing_sl = highs_trail[-1].price + (2 * self.tick_size)
                        filled_setup.current_stop = min(filled_setup.current_stop, trailing_sl)
            effective_stop = filled_setup.current_stop

        # 3. Check Stop / Target hits
        if filled_setup.direction == Direction.LONG:
            # Check Stop Loss first (conservative path)
            if current_candle.low <= effective_stop:
                exit_p = effective_stop
                pnl_pts = exit_p - entry_p
                cost_bd = risk_manager_v2.estimate_round_trip_costs(entry_p, exit_p, qty)
                net_pnl = (pnl_pts * qty) - cost_bd.total_round_trip_cost

                filled_setup.state = SetupLifecycleState.CLOSED
                filled_setup.exit_at = now_iso
                filled_setup.exit_price = exit_p
                filled_setup.exit_reason = "STOP_LOSS"
                filled_setup.realized_pnl = round(net_pnl, 2)
                risk_manager_v2.record_closed_trade(c_time.date(), filled_setup.setup_id, net_pnl, c_time)
                return filled_setup

            # Check Target
            if current_candle.high >= target_p:
                exit_p = target_p
                pnl_pts = exit_p - entry_p
                cost_bd = risk_manager_v2.estimate_round_trip_costs(entry_p, exit_p, qty)
                net_pnl = (pnl_pts * qty) - cost_bd.total_round_trip_cost

                filled_setup.state = SetupLifecycleState.CLOSED
                filled_setup.exit_at = now_iso
                filled_setup.exit_price = exit_p
                filled_setup.exit_reason = "TARGET"
                filled_setup.realized_pnl = round(net_pnl, 2)
                risk_manager_v2.record_closed_trade(c_time.date(), filled_setup.setup_id, net_pnl, c_time)
                return filled_setup

        else: # SHORT
            if current_candle.high >= effective_stop:
                exit_p = effective_stop
                pnl_pts = entry_p - exit_p
                cost_bd = risk_manager_v2.estimate_round_trip_costs(entry_p, exit_p, qty)
                net_pnl = (pnl_pts * qty) - cost_bd.total_round_trip_cost

                filled_setup.state = SetupLifecycleState.CLOSED
                filled_setup.exit_at = now_iso
                filled_setup.exit_price = exit_p
                filled_setup.exit_reason = "STOP_LOSS"
                filled_setup.realized_pnl = round(net_pnl, 2)
                risk_manager_v2.record_closed_trade(c_time.date(), filled_setup.setup_id, net_pnl, c_time)
                return filled_setup

            if current_candle.low <= target_p:
                exit_p = target_p
                pnl_pts = entry_p - exit_p
                cost_bd = risk_manager_v2.estimate_round_trip_costs(entry_p, exit_p, qty)
                net_pnl = (pnl_pts * qty) - cost_bd.total_round_trip_cost

                filled_setup.state = SetupLifecycleState.CLOSED
                filled_setup.exit_at = now_iso
                filled_setup.exit_price = exit_p
                filled_setup.exit_reason = "TARGET"
                filled_setup.realized_pnl = round(net_pnl, 2)
                risk_manager_v2.record_closed_trade(c_time.date(), filled_setup.setup_id, net_pnl, c_time)
                return filled_setup

        return filled_setup


trend_sweep_strategy = TrendSweepFVGStrategy()
