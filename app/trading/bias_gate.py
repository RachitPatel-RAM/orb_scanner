"""
Strict Gate and Conflict Policy (Section 9).

Implements the deterministic directional gate matrix:
- Evaluates underlying direction.
- Long/CE mapped to Bullish candidates.
- Short/PE mapped to Bearish candidates.
- Returns typed GateDecision: PASS_BIAS, BLOCK_OPPOSITE_DAILY, BLOCK_NEUTRAL,
  BLOCK_CONTEXT_CONFLICT, BLOCK_DATA_UNAVAILABLE.
- Gate modes: OFF, SHADOW, STRICT.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional

from app.analysis.daily_bias import BiasDirection, DailyBiasSnapshot
from app.analysis.liquidity_context import ContextDirection, LiquidityContextSnapshot
from app.config import logger, settings
from app.storage.database import db
from app.storage.models import Direction


class GateDecision(str, Enum):
    PASS_BIAS = "PASS_BIAS"
    BLOCK_OPPOSITE_DAILY = "BLOCK_OPPOSITE_DAILY"
    BLOCK_NEUTRAL = "BLOCK_NEUTRAL"
    BLOCK_CONTEXT_CONFLICT = "BLOCK_CONTEXT_CONFLICT"
    BLOCK_DATA_UNAVAILABLE = "BLOCK_DATA_UNAVAILABLE"


class GateMode(str, Enum):
    OFF = "OFF"
    SHADOW = "SHADOW"
    STRICT = "STRICT"


@dataclass
class BiasGateResult:
    """Auditable result of evaluating a candidate breakout signal against bias and context."""
    decision_id: str
    candidate_id: str
    symbol: str
    security_id: str
    trade_date: str
    candidate_direction: str  # 'LONG' or 'SHORT'
    gate_mode: str            # 'OFF', 'SHADOW', 'STRICT'
    decision: GateDecision
    would_allow: bool
    is_allowed: bool
    daily_bias: str
    context_direction: str
    daily_bias_snapshot_id: Optional[str] = None
    liquidity_context_event_id: Optional[str] = None
    reason_code: str = ""
    timestamp: str = ""
    rule_version: str = "v1.0"

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["decision"] = self.decision.value
        return d


class BiasGateEngine:
    """
    Pure decision evaluator implementing the exact Section 9 gate matrix.
    """

    def __init__(self, mode: Optional[str] = None, rule_version: Optional[str] = None):
        raw_mode = (mode or getattr(settings, "bias_gate_mode", "SHADOW")).strip().upper()
        self.mode = GateMode(raw_mode) if raw_mode in GateMode.__members__ else GateMode.SHADOW
        self.rule_version = rule_version or getattr(settings, "bias_rule_version", "v1.0")
        self.allow_neutral_fallback = getattr(settings, "allow_neutral_intraday_fallback", False)

    def evaluate_candidate(
        self,
        candidate_id: str,
        symbol: str,
        security_id: str,
        trade_date: str,
        candidate_direction: Direction,
        daily_snapshot: Optional[DailyBiasSnapshot],
        liquidity_snapshot: Optional[LiquidityContextSnapshot],
        override_mode: Optional[GateMode] = None,
        persist: bool = True,
    ) -> BiasGateResult:
        """
        Evaluates a candidate breakout according to the Section 9 matrix.
        """
        active_mode = override_mode or self.mode
        dir_val = "LONG" if candidate_direction == Direction.LONG else "SHORT"
        now_iso = datetime.now().isoformat()

        # Extract daily bias
        d_bias = daily_snapshot.daily_bias if daily_snapshot else BiasDirection.DATA_UNAVAILABLE
        d_snap_id = daily_snapshot.snapshot_id if daily_snapshot else None

        # Extract intraday context
        ctx_dir = liquidity_snapshot.direction if liquidity_snapshot else ContextDirection.NONE
        ctx_event_id = None
        if liquidity_snapshot and liquidity_snapshot.active_events:
            ctx_event_id = liquidity_snapshot.active_events[0].event_id

        # Matrix Evaluation:
        decision = GateDecision.BLOCK_DATA_UNAVAILABLE
        reason = ""

        if d_bias == BiasDirection.DATA_UNAVAILABLE:
            decision = GateDecision.BLOCK_DATA_UNAVAILABLE
            reason = "Daily data unavailable or incomplete"

        elif d_bias == BiasDirection.NEUTRAL:
            if self.allow_neutral_fallback and ctx_dir in (ContextDirection.BULLISH, ContextDirection.BEARISH):
                # Fallback disabled by default in initial version
                if ctx_dir == ContextDirection.BULLISH and candidate_direction == Direction.LONG:
                    decision = GateDecision.PASS_BIAS
                    reason = "Neutral daily allowed by active bullish intraday context fallback"
                elif ctx_dir == ContextDirection.BEARISH and candidate_direction == Direction.SHORT:
                    decision = GateDecision.PASS_BIAS
                    reason = "Neutral daily allowed by active bearish intraday context fallback"
                else:
                    decision = GateDecision.BLOCK_OPPOSITE_DAILY
                    reason = "Candidate opposes active intraday fallback context"
            else:
                decision = GateDecision.BLOCK_NEUTRAL
                reason = "Neutral daily bias blocks candidate in initial strict profile"

        elif d_bias == BiasDirection.BULLISH:
            if ctx_dir in (ContextDirection.BEARISH, ContextDirection.CONFLICT):
                decision = GateDecision.BLOCK_CONTEXT_CONFLICT
                reason = f"Bullish daily bias opposes {ctx_dir.value} intraday context"
            elif candidate_direction == Direction.LONG:
                decision = GateDecision.PASS_BIAS
                reason = "Bullish daily bias aligned with Long candidate"
            else:
                decision = GateDecision.BLOCK_OPPOSITE_DAILY
                reason = "Bullish daily bias blocks Short candidate"

        elif d_bias == BiasDirection.BEARISH:
            if ctx_dir in (ContextDirection.BULLISH, ContextDirection.CONFLICT):
                decision = GateDecision.BLOCK_CONTEXT_CONFLICT
                reason = f"Bearish daily bias opposes {ctx_dir.value} intraday context"
            elif candidate_direction == Direction.SHORT:
                decision = GateDecision.PASS_BIAS
                reason = "Bearish daily bias aligned with Short candidate"
            else:
                decision = GateDecision.BLOCK_OPPOSITE_DAILY
                reason = "Bearish daily bias blocks Long candidate"

        # Determination of would_allow and is_allowed
        would_allow = (decision == GateDecision.PASS_BIAS)

        if active_mode == GateMode.STRICT:
            is_allowed = would_allow
        elif active_mode == GateMode.SHADOW:
            # Shadow records would_allow without altering baseline execution
            is_allowed = True
        else:  # OFF
            is_allowed = True

        raw = f"{candidate_id}:{symbol}:{active_mode.value}:{decision.value}:{now_iso}"
        decision_id = f"GATE_{symbol}_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:10]}"

        res = BiasGateResult(
            decision_id=decision_id,
            candidate_id=candidate_id,
            symbol=symbol.upper(),
            security_id=str(security_id),
            trade_date=trade_date,
            candidate_direction=dir_val,
            gate_mode=active_mode.value,
            decision=decision,
            would_allow=would_allow,
            is_allowed=is_allowed,
            daily_bias=d_bias.value,
            context_direction=ctx_dir.value,
            daily_bias_snapshot_id=d_snap_id,
            liquidity_context_event_id=ctx_event_id,
            reason_code=reason,
            timestamp=now_iso,
            rule_version=self.rule_version,
        )

        if persist:
            db.save_bias_gate_decision(res.to_dict())

        return res


bias_gate_engine = BiasGateEngine()
