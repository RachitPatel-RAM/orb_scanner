"""
Daily Bias Engine (Section 6).

Deterministic computation of daily directional bias using two completed trading sessions:
- previous = completed session D-1
- reference = completed session D-2
- C = previous.close, H = reference.high, L = reference.low
- C > H -> BULLISH
- C < L -> BEARISH
- else -> NEUTRAL
- missing / invalid -> DATA_UNAVAILABLE

Today's developing session D is NEVER used to compute today's daily bias.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from app.config import logger, settings
from app.market.session import default_session
from app.storage.database import db


class BiasDirection(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"


class BiasReasonCode(str, Enum):
    CLOSE_ABOVE_REF_HIGH = "CLOSE_ABOVE_REF_HIGH"
    CLOSE_BELOW_REF_LOW = "CLOSE_BELOW_REF_LOW"
    INSIDE_REF_RANGE = "INSIDE_REF_RANGE"
    EQUAL_REF_HIGH = "EQUAL_REF_HIGH"
    EQUAL_REF_LOW = "EQUAL_REF_LOW"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    INVALID_CANDLE_DATA = "INVALID_CANDLE_DATA"
    NON_CHRONOLOGICAL_DATES = "NON_CHRONOLOGICAL_DATES"


@dataclass(frozen=True)
class DailyCandle:
    """Represents a validated, completed daily session bar."""
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    timestamp: Optional[str] = None

    def validate(self) -> Tuple[bool, str]:
        """Validates finite OHLC values and basic price consistency."""
        for name, val in [("open", self.open), ("high", self.high), ("low", self.low), ("close", self.close)]:
            if not isinstance(val, (int, float)) or not math.isfinite(val) or val <= 0:
                return False, f"Non-finite or non-positive price for {name}: {val}"
        if self.low > self.high:
            return False, f"Low ({self.low}) cannot exceed High ({self.high})"
        if not (self.low <= self.open <= self.high):
            return False, f"Open ({self.open}) out of range [{self.low}, {self.high}]"
        if not (self.low <= self.close <= self.high):
            return False, f"Close ({self.close}) out of range [{self.low}, {self.high}]"
        if not isinstance(self.volume, (int, float)) or not math.isfinite(self.volume) or self.volume < 0:
            return False, f"Invalid volume: {self.volume}"
        return True, "OK"


@dataclass
class DailyBiasSnapshot:
    """Immutable snapshot of the daily bias calculation for session D."""
    snapshot_id: str
    symbol: str
    security_id: str
    exchange: str
    trading_date: str          # Session D (YYYY-MM-DD)
    previous_session_date: str # Session D-1 (YYYY-MM-DD)
    reference_session_date: str# Session D-2 (YYYY-MM-DD)
    previous_close: float
    reference_high: float
    reference_low: float
    daily_bias: BiasDirection
    reason_code: BiasReasonCode
    source_data_timestamp: Optional[str] = None
    calculated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    rule_version: str = "v1.0"
    data_health: str = "HEALTHY"
    raw_details: Dict[str, Any] = field(default_factory=dict)
    is_corrected: bool = False

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["daily_bias"] = self.daily_bias.value
        d["reason_code"] = self.reason_code.value
        return d


def generate_snapshot_id(
    symbol: str, exchange: str, trading_date: str, rule_version: str = "v1.0"
) -> str:
    """Generates deterministic, collision-resistant snapshot identifier."""
    raw = f"{symbol.upper()}:{exchange.upper()}:{trading_date}:{rule_version}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"BIAS_{symbol.upper()}_{trading_date.replace('-', '')}_{digest}"


class DailyBiasEngine:
    """
    Pure deterministic daily bias engine.
    Calculates bias from two completed trading sessions (D-1 and D-2).
    """

    def __init__(self, rule_version: Optional[str] = None):
        self.rule_version = rule_version or getattr(settings, "bias_rule_version", "v1.0")

    def evaluate(
        self,
        symbol: str,
        security_id: str,
        trading_date: date,
        previous_candle: Optional[DailyCandle],
        reference_candle: Optional[DailyCandle],
        exchange: str = "NSE",
        is_corrected: bool = False,
    ) -> DailyBiasSnapshot:
        """
        Pure deterministic computation:
        - previous = D-1 completed session
        - reference = D-2 completed session
        """
        trading_date_str = trading_date.isoformat()
        prev_date_str = previous_candle.trade_date.isoformat() if previous_candle else ""
        ref_date_str = reference_candle.trade_date.isoformat() if reference_candle else ""

        snapshot_id = generate_snapshot_id(symbol, exchange, trading_date_str, self.rule_version)

        # 1. Missing Data Check
        if previous_candle is None or reference_candle is None:
            return DailyBiasSnapshot(
                snapshot_id=snapshot_id,
                symbol=symbol.upper(),
                security_id=str(security_id),
                exchange=exchange,
                trading_date=trading_date_str,
                previous_session_date=prev_date_str,
                reference_session_date=ref_date_str,
                previous_close=0.0,
                reference_high=0.0,
                reference_low=0.0,
                daily_bias=BiasDirection.DATA_UNAVAILABLE,
                reason_code=BiasReasonCode.INSUFFICIENT_HISTORY,
                rule_version=self.rule_version,
                data_health="MISSING_DATA",
                raw_details={"error": "One or both completed sessions missing"},
                is_corrected=is_corrected,
            )

        # 2. Chronology Validation: ref < prev < trading_date
        if not (reference_candle.trade_date < previous_candle.trade_date < trading_date):
            return DailyBiasSnapshot(
                snapshot_id=snapshot_id,
                symbol=symbol.upper(),
                security_id=str(security_id),
                exchange=exchange,
                trading_date=trading_date_str,
                previous_session_date=prev_date_str,
                reference_session_date=ref_date_str,
                previous_close=previous_candle.close,
                reference_high=reference_candle.high,
                reference_low=reference_candle.low,
                daily_bias=BiasDirection.DATA_UNAVAILABLE,
                reason_code=BiasReasonCode.NON_CHRONOLOGICAL_DATES,
                rule_version=self.rule_version,
                data_health="INVALID_CHRONOLOGY",
                raw_details={
                    "error": f"Dates non-chronological: ref={ref_date_str}, prev={prev_date_str}, target={trading_date_str}"
                },
                is_corrected=is_corrected,
            )

        # 3. Candle Integrity Validation
        prev_valid, prev_err = previous_candle.validate()
        ref_valid, ref_err = reference_candle.validate()
        if not prev_valid or not ref_valid:
            err_msg = prev_err if not prev_valid else ref_err
            return DailyBiasSnapshot(
                snapshot_id=snapshot_id,
                symbol=symbol.upper(),
                security_id=str(security_id),
                exchange=exchange,
                trading_date=trading_date_str,
                previous_session_date=prev_date_str,
                reference_session_date=ref_date_str,
                previous_close=previous_candle.close if prev_valid else 0.0,
                reference_high=reference_candle.high if ref_valid else 0.0,
                reference_low=reference_candle.low if ref_valid else 0.0,
                daily_bias=BiasDirection.DATA_UNAVAILABLE,
                reason_code=BiasReasonCode.INVALID_CANDLE_DATA,
                rule_version=self.rule_version,
                data_health="INVALID_CANDLE_INTEGRITY",
                raw_details={"error": err_msg},
                is_corrected=is_corrected,
            )

        # 4. Deterministic Comparison
        # C = previous.close, H = reference.high, L = reference.low
        C = previous_candle.close
        H = reference_candle.high
        L = reference_candle.low

        # Note: Strictly C > H is BULLISH, C < L is BEARISH.
        # Equal close (C == H or C == L) or inside range (L <= C <= H) is NEUTRAL.
        if C > H:
            direction = BiasDirection.BULLISH
            reason = BiasReasonCode.CLOSE_ABOVE_REF_HIGH
        elif C < L:
            direction = BiasDirection.BEARISH
            reason = BiasReasonCode.CLOSE_BELOW_REF_LOW
        elif math.isclose(C, H, rel_tol=1e-9, abs_tol=1e-5):
            direction = BiasDirection.NEUTRAL
            reason = BiasReasonCode.EQUAL_REF_HIGH
        elif math.isclose(C, L, rel_tol=1e-9, abs_tol=1e-5):
            direction = BiasDirection.NEUTRAL
            reason = BiasReasonCode.EQUAL_REF_LOW
        else:
            direction = BiasDirection.NEUTRAL
            reason = BiasReasonCode.INSIDE_REF_RANGE

        return DailyBiasSnapshot(
            snapshot_id=snapshot_id,
            symbol=symbol.upper(),
            security_id=str(security_id),
            exchange=exchange,
            trading_date=trading_date_str,
            previous_session_date=prev_date_str,
            reference_session_date=ref_date_str,
            previous_close=C,
            reference_high=H,
            reference_low=L,
            daily_bias=direction,
            reason_code=reason,
            source_data_timestamp=previous_candle.timestamp,
            rule_version=self.rule_version,
            data_health="HEALTHY",
            raw_details={
                "prev_open": previous_candle.open,
                "prev_high": previous_candle.high,
                "prev_low": previous_candle.low,
                "prev_close": previous_candle.close,
                "prev_volume": previous_candle.volume,
                "ref_open": reference_candle.open,
                "ref_high": reference_candle.high,
                "ref_low": reference_candle.low,
                "ref_close": reference_candle.close,
                "ref_volume": reference_candle.volume,
            },
            is_corrected=is_corrected,
        )

    def compute_and_persist(
        self,
        symbol: str,
        security_id: str,
        trading_date: date,
        previous_candle: Optional[DailyCandle],
        reference_candle: Optional[DailyCandle],
        exchange: str = "NSE",
        is_corrected: bool = False,
    ) -> DailyBiasSnapshot:
        """Evaluates bias and stores the immutable snapshot in SQLite."""
        snapshot = self.evaluate(
            symbol=symbol,
            security_id=security_id,
            trading_date=trading_date,
            previous_candle=previous_candle,
            reference_candle=reference_candle,
            exchange=exchange,
            is_corrected=is_corrected,
        )
        db.save_daily_bias_snapshot(snapshot.to_dict())
        return snapshot

    def get_or_compute_snapshot(
        self,
        symbol: str,
        security_id: str,
        trading_date: date,
        previous_candle: Optional[DailyCandle] = None,
        reference_candle: Optional[DailyCandle] = None,
        exchange: str = "NSE",
    ) -> DailyBiasSnapshot:
        """
        Retrieves existing persisted snapshot for the session if already calculated,
        or calculates and persists it if candle data is provided.
        """
        trading_date_str = trading_date.isoformat()
        existing = db.get_daily_bias_snapshot(symbol, trading_date_str, self.rule_version)
        if existing:
            return DailyBiasSnapshot(
                snapshot_id=existing["snapshot_id"],
                symbol=existing["symbol"],
                security_id=str(existing["security_id"]),
                exchange=existing.get("exchange", "NSE"),
                trading_date=existing["trading_date"],
                previous_session_date=existing["previous_session_date"],
                reference_session_date=existing["reference_session_date"],
                previous_close=float(existing["previous_close"]),
                reference_high=float(existing["reference_high"]),
                reference_low=float(existing["reference_low"]),
                daily_bias=BiasDirection(existing["daily_bias"]),
                reason_code=BiasReasonCode(existing["reason_code"]),
                source_data_timestamp=existing.get("source_data_timestamp"),
                calculated_at=existing.get("calculated_at", ""),
                rule_version=existing.get("rule_version", self.rule_version),
                data_health=existing.get("data_health", "HEALTHY"),
                raw_details=existing.get("raw_details", {}),
                is_corrected=bool(existing.get("is_corrected", 0)),
            )

        if previous_candle is not None and reference_candle is not None:
            return self.compute_and_persist(
                symbol=symbol,
                security_id=security_id,
                trading_date=trading_date,
                previous_candle=previous_candle,
                reference_candle=reference_candle,
                exchange=exchange,
            )

        # Fallback if no candle data provided and no snapshot exists
        return self.evaluate(
            symbol=symbol,
            security_id=security_id,
            trading_date=trading_date,
            previous_candle=None,
            reference_candle=None,
            exchange=exchange,
        )


daily_bias_engine = DailyBiasEngine()
