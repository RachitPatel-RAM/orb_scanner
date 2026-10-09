"""
Tests for Section 9: Strict Gate and Conflict Policy.
"""

from datetime import date
import pytest

from app.analysis.daily_bias import (
    BiasDirection,
    BiasReasonCode,
    DailyBiasSnapshot,
)
from app.analysis.liquidity_context import (
    ContextDirection,
    LiquidityContextSnapshot,
)
from app.storage.models import Direction
from app.trading.bias_gate import (
    BiasGateEngine,
    GateDecision,
    GateMode,
)


def make_dummy_daily(bias: BiasDirection) -> DailyBiasSnapshot:
    return DailyBiasSnapshot(
        snapshot_id="SNAP_123",
        symbol="NIFTY",
        security_id="13",
        exchange="NSE",
        trading_date="2026-10-06",
        previous_session_date="2026-10-05",
        reference_session_date="2026-10-01",
        previous_close=25000.0,
        reference_high=24900.0,
        reference_low=24700.0,
        daily_bias=bias,
        reason_code=BiasReasonCode.CLOSE_ABOVE_REF_HIGH if bias == BiasDirection.BULLISH else BiasReasonCode.INSIDE_REF_RANGE,
    )


def make_dummy_context(ctx_dir: ContextDirection) -> LiquidityContextSnapshot:
    return LiquidityContextSnapshot(
        symbol="NIFTY",
        security_id="13",
        timestamp="2026-10-06T10:05:00",
        direction=ctx_dir,
    )


def test_bullish_daily_long_candidate_passes():
    """Bullish + (None or Bullish) + Long -> PASS_BIAS."""
    engine = BiasGateEngine(mode="STRICT")

    daily = make_dummy_daily(BiasDirection.BULLISH)
    ctx_none = make_dummy_context(ContextDirection.NONE)
    ctx_bull = make_dummy_context(ContextDirection.BULLISH)

    res1 = engine.evaluate_candidate(
        candidate_id="SIG_1",
        symbol="NIFTY",
        security_id="13",
        trade_date="2026-10-06",
        candidate_direction=Direction.LONG,
        daily_snapshot=daily,
        liquidity_snapshot=ctx_none,
        persist=False,
    )
    assert res1.decision == GateDecision.PASS_BIAS
    assert res1.would_allow is True
    assert res1.is_allowed is True

    res2 = engine.evaluate_candidate(
        candidate_id="SIG_2",
        symbol="NIFTY",
        security_id="13",
        trade_date="2026-10-06",
        candidate_direction=Direction.LONG,
        daily_snapshot=daily,
        liquidity_snapshot=ctx_bull,
        persist=False,
    )
    assert res2.decision == GateDecision.PASS_BIAS
    assert res2.is_allowed is True


def test_bullish_daily_short_candidate_blocked():
    """Bullish + (None or Bullish) + Short -> BLOCK_OPPOSITE_DAILY."""
    engine = BiasGateEngine(mode="STRICT")

    daily = make_dummy_daily(BiasDirection.BULLISH)
    ctx_none = make_dummy_context(ContextDirection.NONE)

    res = engine.evaluate_candidate(
        candidate_id="SIG_SHORT",
        symbol="NIFTY",
        security_id="13",
        trade_date="2026-10-06",
        candidate_direction=Direction.SHORT,
        daily_snapshot=daily,
        liquidity_snapshot=ctx_none,
        persist=False,
    )
    assert res.decision == GateDecision.BLOCK_OPPOSITE_DAILY
    assert res.would_allow is False
    assert res.is_allowed is False


def test_bullish_daily_opposed_by_bearish_or_conflict_context_blocks():
    """Bullish + (Bearish or Conflict) + Either -> BLOCK_CONTEXT_CONFLICT."""
    engine = BiasGateEngine(mode="STRICT")

    daily = make_dummy_daily(BiasDirection.BULLISH)
    ctx_bear = make_dummy_context(ContextDirection.BEARISH)
    ctx_conf = make_dummy_context(ContextDirection.CONFLICT)

    res_bear = engine.evaluate_candidate(
        candidate_id="SIG_CAND",
        symbol="NIFTY",
        security_id="13",
        trade_date="2026-10-06",
        candidate_direction=Direction.LONG,
        daily_snapshot=daily,
        liquidity_snapshot=ctx_bear,
        persist=False,
    )
    assert res_bear.decision == GateDecision.BLOCK_CONTEXT_CONFLICT
    assert res_bear.would_allow is False
    assert res_bear.is_allowed is False

    res_conf = engine.evaluate_candidate(
        candidate_id="SIG_CAND2",
        symbol="NIFTY",
        security_id="13",
        trade_date="2026-10-06",
        candidate_direction=Direction.LONG,
        daily_snapshot=daily,
        liquidity_snapshot=ctx_conf,
        persist=False,
    )
    assert res_conf.decision == GateDecision.BLOCK_CONTEXT_CONFLICT
    assert res_conf.is_allowed is False


def test_bearish_daily_matrix():
    """
    Bearish + (None or Bearish) + Short -> PASS_BIAS
    Bearish + (None or Bearish) + Long -> BLOCK_OPPOSITE_DAILY
    Bearish + (Bullish or Conflict) -> BLOCK_CONTEXT_CONFLICT
    """
    engine = BiasGateEngine(mode="STRICT")
    daily = make_dummy_daily(BiasDirection.BEARISH)

    # 1. Bearish + None + Short -> Pass
    res1 = engine.evaluate_candidate(
        candidate_id="S1",
        symbol="NIFTY",
        security_id="13",
        trade_date="2026-10-06",
        candidate_direction=Direction.SHORT,
        daily_snapshot=daily,
        liquidity_snapshot=make_dummy_context(ContextDirection.NONE),
        persist=False,
    )
    assert res1.decision == GateDecision.PASS_BIAS
    assert res1.is_allowed is True

    # 2. Bearish + None + Long -> Block opposite
    res2 = engine.evaluate_candidate(
        candidate_id="S2",
        symbol="NIFTY",
        security_id="13",
        trade_date="2026-10-06",
        candidate_direction=Direction.LONG,
        daily_snapshot=daily,
        liquidity_snapshot=make_dummy_context(ContextDirection.NONE),
        persist=False,
    )
    assert res2.decision == GateDecision.BLOCK_OPPOSITE_DAILY
    assert res2.is_allowed is False

    # 3. Bearish + Bullish context -> Block context conflict
    res3 = engine.evaluate_candidate(
        candidate_id="S3",
        symbol="NIFTY",
        security_id="13",
        trade_date="2026-10-06",
        candidate_direction=Direction.SHORT,
        daily_snapshot=daily,
        liquidity_snapshot=make_dummy_context(ContextDirection.BULLISH),
        persist=False,
    )
    assert res3.decision == GateDecision.BLOCK_CONTEXT_CONFLICT
    assert res3.is_allowed is False


def test_neutral_and_data_unavailable_block_in_strict_profile():
    """Neutral and Data Unavailable both block in initial strict profile."""
    engine = BiasGateEngine(mode="STRICT")

    # Neutral
    res_neut = engine.evaluate_candidate(
        candidate_id="S_N",
        symbol="NIFTY",
        security_id="13",
        trade_date="2026-10-06",
        candidate_direction=Direction.LONG,
        daily_snapshot=make_dummy_daily(BiasDirection.NEUTRAL),
        liquidity_snapshot=make_dummy_context(ContextDirection.NONE),
        persist=False,
    )
    assert res_neut.decision == GateDecision.BLOCK_NEUTRAL
    assert res_neut.is_allowed is False

    # Data Unavailable
    res_unav = engine.evaluate_candidate(
        candidate_id="S_U",
        symbol="NIFTY",
        security_id="13",
        trade_date="2026-10-06",
        candidate_direction=Direction.LONG,
        daily_snapshot=make_dummy_daily(BiasDirection.DATA_UNAVAILABLE),
        liquidity_snapshot=make_dummy_context(ContextDirection.NONE),
        persist=False,
    )
    assert res_unav.decision == GateDecision.BLOCK_DATA_UNAVAILABLE
    assert res_unav.is_allowed is False


def test_shadow_mode_records_decision_without_altering_baseline():
    """
    In SHADOW mode:
    would_allow reflects STRICT gate decision, but is_allowed remains True
    so baseline execution continues undisturbed.
    """
    engine = BiasGateEngine(mode="SHADOW")

    # Opposite direction trade (Bullish daily + Short candidate)
    res = engine.evaluate_candidate(
        candidate_id="S_SHADOW",
        symbol="NIFTY",
        security_id="13",
        trade_date="2026-10-06",
        candidate_direction=Direction.SHORT,
        daily_snapshot=make_dummy_daily(BiasDirection.BULLISH),
        liquidity_snapshot=make_dummy_context(ContextDirection.NONE),
        persist=False,
    )
    assert res.decision == GateDecision.BLOCK_OPPOSITE_DAILY
    assert res.would_allow is False  # Shadowed veto
    assert res.is_allowed is True   # Baseline execution NOT blocked in SHADOW mode!
    assert res.gate_mode == "SHADOW"
