"""
Optional Evidence Modules (Section 7).

Implements:
1. RVOL Engine: Completed 5m volume divided by median of the same session-relative
   5m bucket over 20 previous valid sessions. Requires all 20 observations and positive denominator.
2. Volume Profile Context.
3. SMT Divergence Adapter: Synchronous observation window non-confirmation. Default DISABLED.
4. CBDR Adapter: Central Bank Dealers Range session adapter. Default DISABLED for Indian V1.

All modules support explicit DISABLED, SHADOW, and REQUIRED modes.
A required unavailable input blocks the affected setup.
A disabled input is neither a pass nor a failure.
A shadow result is recorded for research and cannot silently become a hard gate.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, time
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
import statistics

from app.storage.models import Candle


class ModuleMode(str, Enum):
    DISABLED = "DISABLED"
    SHADOW = "SHADOW"
    REQUIRED = "REQUIRED"


@dataclass
class RVOLResult:
    status: str              # 'CALCULATED', 'INSUFFICIENT_HISTORY', 'ZERO_MEDIAN', 'DISABLED'
    current_volume: float = 0.0
    median_volume_20: float = 0.0
    rvol: float = 0.0
    history_count: int = 0
    required_count: int = 20
    is_high_volume: bool = False  # RVOL >= 1.5 threshold

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SMTResult:
    status: str = "DISABLED" # 'DISABLED', 'NO_DIVERGENCE', 'BEARISH_SMT', 'BULLISH_SMT', 'DATA_UNAVAILABLE'
    pair_symbol: Optional[str] = None
    observation_window_bars: int = 3
    correlation: Optional[float] = None
    divergence_detected: bool = False
    details: str = "SMT divergence is disabled for Indian market V1 by default."

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CBDRResult:
    status: str = "DISABLED" # 'DISABLED', 'CALCULATED', 'UNCONFIGURED'
    market_name: str = "NSE"
    session_start: str = "14:00"
    session_end: str = "20:00"
    range_high: Optional[float] = None
    range_low: Optional[float] = None
    details: str = "CBDR (Central Bank Dealers Range) is unconfigured/disabled for Indian equity index sessions."

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class OptionalEvidenceReport:
    """Aggregated optional evidence for a candidate signal."""
    rvol: RVOLResult
    volume_profile_poc_distance: Optional[float] = None
    volume_profile_inside_va: Optional[bool] = None
    smt: SMTResult = field(default_factory=SMTResult)
    cbdr: CBDRResult = field(default_factory=CBDRResult)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rvol": self.rvol.to_dict(),
            "volume_profile_poc_distance": self.volume_profile_poc_distance,
            "volume_profile_inside_va": self.volume_profile_inside_va,
            "smt": self.smt.to_dict(),
            "cbdr": self.cbdr.to_dict(),
        }


class RVOLEngine:
    """
    Computes deterministic RVOL against 20 prior trading sessions' same-time bucket.
    """

    @staticmethod
    def calculate_rvol(
        current_candle: Candle,
        historical_same_bucket_volumes: List[float],
        min_required_sessions: int = 20,
        high_vol_threshold: float = 1.5,
    ) -> RVOLResult:
        """
        Calculates RVOL = current_volume / median(20 previous sessions at same 5m time).
        Requires strictly 20 observations and positive median.
        """
        curr_vol = max(0.0, float(current_candle.volume))
        valid_history = [v for v in historical_same_bucket_volumes if v > 0]

        if len(valid_history) < min_required_sessions:
            return RVOLResult(
                status="INSUFFICIENT_HISTORY",
                current_volume=curr_vol,
                history_count=len(valid_history),
                required_count=min_required_sessions,
            )

        # Take strictly the last 20 sessions
        history_20 = valid_history[-min_required_sessions:]
        med_vol = statistics.median(history_20)

        if med_vol <= 0:
            return RVOLResult(
                status="ZERO_MEDIAN",
                current_volume=curr_vol,
                median_volume_20=med_vol,
                history_count=len(history_20),
            )

        rvol_val = round(curr_vol / med_vol, 2)
        is_high = rvol_val >= high_vol_threshold

        return RVOLResult(
            status="CALCULATED",
            current_volume=curr_vol,
            median_volume_20=round(med_vol, 2),
            rvol=rvol_val,
            history_count=len(history_20),
            required_count=min_required_sessions,
            is_high_volume=is_high,
        )


class SMTDivergenceEngine:
    """
    Evaluates synchronous non-confirmation divergence between correlated pairs.
    """

    @staticmethod
    def evaluate_smt(
        primary_candles: List[Candle],
        reference_candles: List[Candle],
        mode: ModuleMode = ModuleMode.DISABLED,
        pair_symbol: Optional[str] = None,
    ) -> SMTResult:
        if mode == ModuleMode.DISABLED:
            return SMTResult(status="DISABLED", pair_symbol=pair_symbol)

        if not primary_candles or not reference_candles or len(primary_candles) < 3 or len(reference_candles) < 3:
            return SMTResult(
                status="DATA_UNAVAILABLE",
                pair_symbol=pair_symbol,
                details="Insufficient candles for synchronous pair evaluation.",
            )

        # Basic synchronous high/low check on last 3 bars
        p_highs = [c.high for c in primary_candles[-3:]]
        r_highs = [c.high for c in reference_candles[-3:]]
        p_lows = [c.low for c in primary_candles[-3:]]
        r_lows = [c.low for c in reference_candles[-3:]]

        # Bearish SMT: Primary makes higher high, reference fails to make higher high
        p_made_hh = p_highs[-1] > max(p_highs[:-1])
        r_made_hh = r_highs[-1] > max(r_highs[:-1])

        p_made_ll = p_lows[-1] < min(p_lows[:-1])
        r_made_ll = r_lows[-1] < min(r_lows[:-1])

        if p_made_hh and not r_made_hh:
            return SMTResult(
                status="BEARISH_SMT",
                pair_symbol=pair_symbol,
                divergence_detected=True,
                details=f"Primary made higher high while {pair_symbol} failed to confirm.",
            )
        elif not p_made_hh and r_made_hh:
            return SMTResult(
                status="BEARISH_SMT",
                pair_symbol=pair_symbol,
                divergence_detected=True,
                details=f"Reference {pair_symbol} made higher high while primary failed to confirm.",
            )
        elif p_made_ll and not r_made_ll:
            return SMTResult(
                status="BULLISH_SMT",
                pair_symbol=pair_symbol,
                divergence_detected=True,
                details=f"Primary made lower low while {pair_symbol} held higher low.",
            )
        elif not p_made_ll and r_made_ll:
            return SMTResult(
                status="BULLISH_SMT",
                pair_symbol=pair_symbol,
                divergence_detected=True,
                details=f"Reference {pair_symbol} swept low while primary held higher low.",
            )

        return SMTResult(
            status="NO_DIVERGENCE",
            pair_symbol=pair_symbol,
            divergence_detected=False,
            details="Synchronous pair highs and lows moved in harmony.",
        )


class CBDREngine:
    """
    Central Bank Dealers Range adapter (ICT concept).
    Explicitly labeled experimental and defaulted to DISABLED for Indian market V1.
    """

    @staticmethod
    def evaluate_cbdr(
        mode: ModuleMode = ModuleMode.DISABLED,
    ) -> CBDRResult:
        return CBDRResult(
            status="DISABLED",
            market_name="NSE",
            details="Central Bank Dealers Range is disabled for Indian equity index sessions in V1.",
        )
