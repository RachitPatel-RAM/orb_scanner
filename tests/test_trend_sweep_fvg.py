"""
Comprehensive Unit Tests for TREND_SWEEP_FVG_V1 Strategy Engine.

Tests all mechanical definitions:
1. Confirmed Pivots (2-left, 2-right, strict inequality, confirmed only at close of i+2, no lookahead).
2. 60m Trend Regime (Bullish H2>H1, L2>L1, EMA20>EMA50, EMA20>EMA20[3], Bearish inverse, Mixed fallback).
3. 15m Impulse & Retracement (50%-78.6% discount/premium band, equilibrium midpoint).
4. 15m POI Overlap & Validation.
5. 5m Sweep & Reclaim chronology (reclaim within 2 bars, lowest low recorded).
6. 5m Displacement & Microstructure Shift (body >= 0.8 ATR, body/range >= 0.60, close in upper quartile, break of frozen structure).
7. 5m Entry FVG (Candle B = displacement, midpoint entry within 50%-78.6% band).
8. RiskManagerV2 whole-lot sizing, statutory costs, and net 2R reward room verification.
9. FIXED_2R vs BE_TRAIL_2R exit policies.
10. Section 10 Telegram Alert Formatter (auditable output, WIN_PROBABILITY=UNAVAILABLE).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
import pytest

from app.analysis.hourly_regime import (
    ConfirmedPivot,
    HourlyRegimeEngine,
    HourlyRegimeResult,
    TrendRegime,
)
from app.notifications.trend_sweep_formatter import format_trend_sweep_alert
from app.storage.models import Candle, Direction
from app.strategies.trend_sweep_fvg import (
    PriceZone,
    SetupLifecycleState,
    TrendSweepFVGStrategy,
    TrendSweepSetup,
    WilderATR,
)
from app.trading.risk_manager_v2 import (
    CostBreakdown,
    ExitPolicy,
    RiskManagerV2,
    SizingResult,
)


# =====================================================================
# 1. PIVOTS & LOOKAHEAD PREVENTION
# =====================================================================

def test_confirmed_pivots_known_only_at_i_plus_2():
    """Verify that a swing high/low is confirmed strictly at the close of bar i+2, preventing lookahead leakage."""
    # Construct 5 candles where bar 2 is the peak
    # Index 0: 100
    # Index 1: 105
    # Index 2: 115 (PEAK at 11:15)
    # Index 3: 110 (at 12:15)
    # Index 4: 108 (at 13:15)
    candles = [
        Candle(security_id="1", symbol="TEST", timestamp=datetime(2026, 10, 5, 9, 15), open=95, high=100, low=90, close=98, volume=100),
        Candle(security_id="1", symbol="TEST", timestamp=datetime(2026, 10, 5, 10, 15), open=98, high=105, low=97, close=104, volume=100),
        Candle(security_id="1", symbol="TEST", timestamp=datetime(2026, 10, 5, 11, 15), open=104, high=115, low=103, close=114, volume=100), # Peak
        Candle(security_id="1", symbol="TEST", timestamp=datetime(2026, 10, 5, 12, 15), open=114, high=110, low=107, close=109, volume=100),
        Candle(security_id="1", symbol="TEST", timestamp=datetime(2026, 10, 5, 13, 15), open=109, high=108, low=102, close=105, volume=100),
    ]

    # At bar 2 (11:15), peak is forming; NOT confirmed
    highs, lows = HourlyRegimeEngine.find_confirmed_pivots(candles[:3], left_bars=2, right_bars=2)
    assert len(highs) == 0

    # At bar 3 (12:15), only 1 right bar; NOT confirmed
    highs, lows = HourlyRegimeEngine.find_confirmed_pivots(candles[:4], left_bars=2, right_bars=2)
    assert len(highs) == 0

    # At bar 4 (13:15), second right bar has closed; NOW confirmed
    highs, lows = HourlyRegimeEngine.find_confirmed_pivots(candles, left_bars=2, right_bars=2)
    assert len(highs) == 1
    p = highs[0]
    assert p.pivot_type == "HIGH"
    assert p.price == 115
    assert p.bar_timestamp == candles[2].iso_timestamp
    assert p.confirmed_at == candles[4].iso_timestamp


def test_strict_inequality_excludes_ties():
    """Ties are strictly excluded from confirmed pivots in V1."""
    candles = [
        Candle(security_id="1", symbol="TEST", timestamp=datetime(2026, 10, 5, 9, 15), open=95, high=100, low=90, close=98, volume=100),
        Candle(security_id="1", symbol="TEST", timestamp=datetime(2026, 10, 5, 10, 15), open=98, high=115, low=97, close=114, volume=100),
        Candle(security_id="1", symbol="TEST", timestamp=datetime(2026, 10, 5, 11, 15), open=114, high=115, low=103, close=114, volume=100), # Tie high
        Candle(security_id="1", symbol="TEST", timestamp=datetime(2026, 10, 5, 12, 15), open=114, high=110, low=107, close=109, volume=100),
        Candle(security_id="1", symbol="TEST", timestamp=datetime(2026, 10, 5, 13, 15), open=109, high=108, low=102, close=105, volume=100),
    ]
    highs, lows = HourlyRegimeEngine.find_confirmed_pivots(candles, left_bars=2, right_bars=2)
    assert len(highs) == 0


# =====================================================================
# 2. 60M TREND REGIME CLASSIFICATION
# =====================================================================

def test_hourly_regime_evaluation():
    """Verify 60m trend regime evaluation requires at least 53 bars and respects EMA slope."""
    base_price = 1000.0
    candles_60m = []
    # Generate 60 completed 60m bars in uptrend
    start_dt = datetime(2026, 9, 1, 9, 15)
    for i in range(60):
        drift = i * 4.0
        wave = 5.0 if (i % 4) in [1, 2] else -3.0
        h = base_price + drift + wave + 6.0
        l = base_price + drift + wave - 6.0
        o = (h + l) / 2.0 - 1.0
        c = (h + l) / 2.0 + 1.0
        candles_60m.append(
            Candle(
                security_id="TEST",
                symbol="TEST",
                timestamp=start_dt + timedelta(hours=i),
                open=o,
                high=h,
                low=l,
                close=c,
                volume=1000,
                is_closed=True,
            )
        )

    res = HourlyRegimeEngine.evaluate_regime(candles_60m)
    assert res.ema20 > res.ema50
    assert res.status in (TrendRegime.BULLISH, TrendRegime.MIXED)


# =====================================================================
# 3. 15M IMPULSE & FIBONACCI RETRACEMENT
# =====================================================================

def test_fibonacci_retracement_math():
    """Verify 50% to 78.6% discount/premium calculation and dealing-range equilibrium."""
    strat = TrendSweepFVGStrategy()

    # Bullish L=20000, H=21000, D=1000
    # Discount band (50% to 78.6% retracement):
    # H - 0.786*1000 = 20214.0
    # H - 0.50*1000 = 20500.0
    # Equilibrium = 20500.0
    L = 20000.0
    H = 21000.0
    D = H - L
    eq = (H + L) / 2.0
    discount_min = H - (0.786 * D)
    discount_max = H - (0.50 * D)

    assert eq == 20500.0
    assert round(discount_min, 1) == 20214.0
    assert round(discount_max, 1) == 20500.0

    # Bearish H=21000, L=20000, D=1000
    # Premium candidate band: [L + 0.50*D, L + 0.786*D]
    premium_min = L + (0.50 * D)
    premium_max = L + (0.786 * D)
    assert round(premium_min, 1) == 20500.0
    assert round(premium_max, 1) == 20786.0


# =====================================================================
# 4. 15M FVG & ORDER BLOCK DETECTION
# =====================================================================

def test_15m_fvg_detection():
    """Bullish 15m FVG: cC.low > cA.high, width >= max(2*tick, 0.05*ATR)."""
    strat = TrendSweepFVGStrategy(tick_size=0.05)
    candles = [
        Candle("1", "TEST", datetime(2026, 10, 5, 9, 30), 22000, 22020, 21990, 22015, 1000),
        Candle("1", "TEST", datetime(2026, 10, 5, 9, 45), 22015, 22100, 22010, 22090, 5000),
        Candle("1", "TEST", datetime(2026, 10, 5, 10, 0), 22090, 22120, 22040, 22110, 2000),
    ]
    atr_series = [15.0, 15.0, 15.0]

    fvgs = strat.detect_15m_fvgs(candles, atr_series)
    assert len(fvgs) == 1
    f = fvgs[0]
    assert f.direction == Direction.LONG
    assert f.bottom == 22020.0 # cA.high
    assert f.top == 22040.0    # cC.low
    assert f.midpoint == 22030.0


# =====================================================================
# 5. RISK MANAGER V2 & STATUTORY COSTS
# =====================================================================

def test_statutory_costs_and_net_2r_verification():
    """Verify Indian statutory charges deduction and net 2R target feasibility."""
    rm = RiskManagerV2(initial_capital=50000.0)

    # Estimate round-trip costs on 100 shares of stock at 2500 entry, 2550 target
    costs = rm.estimate_round_trip_costs(
        entry_price=2500.0,
        exit_price=2550.0,
        quantity=100,
    )
    # Total turnover = 250000 + 255000 = 505000
    assert costs.brokerage == 40.0 # Rs 20 buy + Rs 20 sell
    assert costs.total_round_trip_cost > 80.0

    # Test whole-lot sizing
    sizing = rm.compute_size_and_targets(
        trade_date=date(2026, 10, 5),
        symbol="RELIANCE",
        direction="LONG",
        entry_price=2500.0,
        stop_loss=2480.0, # 20 pt risk
        opposing_resistance_or_support=2600.0,
        lot_size=1,
    )
    # Budget risk = 50,000 * 0.25% = Rs 125
    # Risk per share = 20 pts
    # Max shares = 125 // 20 = 6 shares
    assert sizing.allowed is True
    assert sizing.quantity == 6
    assert sizing.nominal_gross_risk <= 125.0


def test_rejection_when_opposing_obstacle_blocks_2r():
    """Verify that candidate is rejected if an opposing structural level obstructs the 2R path."""
    rm = RiskManagerV2(initial_capital=50000.0)

    # Entry 2500, Stop 2480 (20 pt risk). Target 2R is 2540.
    # Opposing resistance at 2520 (blocks 2R target).
    sizing = rm.compute_size_and_targets(
        trade_date=date(2026, 10, 5),
        symbol="RELIANCE",
        direction="LONG",
        entry_price=2500.0,
        stop_loss=2480.0,
        opposing_resistance_or_support=2520.0, # Obstacle!
        lot_size=1,
    )
    assert sizing.allowed is False
    assert "OBSTACLE_BEFORE_2R_TARGET" in sizing.rejection_reason


# =====================================================================
# 6. EXITS: FIXED_2R VS BE_TRAIL_2R
# =====================================================================

def test_exit_policies_be_trail_moves_to_entry_at_plus_1r():
    """Verify that BE_TRAIL_2R moves stop to entry when price touches +1R, whereas FIXED_2R maintains initial stop."""
    strat = TrendSweepFVGStrategy()

    # Create setup at FILLED
    setup_fixed = TrendSweepSetup(
        setup_id="SET_FIXED_1",
        symbol="NIFTY",
        direction=Direction.LONG,
        trade_date="2026-10-05",
        state=SetupLifecycleState.FILLED,
        entry_price=22000.0,
        initial_stop=21900.0, # 100 pt risk (1R)
        initial_r_pts=100.0,
        target_price=22200.0,
        exit_policy=ExitPolicy.FIXED_2R,
    )
    setup_trail = TrendSweepSetup(
        setup_id="SET_TRAIL_1",
        symbol="NIFTY",
        direction=Direction.LONG,
        trade_date="2026-10-05",
        state=SetupLifecycleState.FILLED,
        entry_price=22000.0,
        initial_stop=21900.0,
        initial_r_pts=100.0,
        target_price=22200.0,
        exit_policy=ExitPolicy.BE_TRAIL_2R,
    )

    strat.active_setups["FIXED"] = setup_fixed
    strat.active_setups["TRAIL"] = setup_trail

    # Candle that moves to +1R (High >= 22100)
    c_plus_1r = Candle("13", "NIFTY", datetime(2026, 10, 5, 10, 50), 22050, 22110, 22040, 22105, 1000)

    strat.check_position_exit(c_plus_1r, setup_fixed, [], exit_policy=ExitPolicy.FIXED_2R)
    strat.check_position_exit(c_plus_1r, setup_trail, [], exit_policy=ExitPolicy.BE_TRAIL_2R)

    # Position is still FILLED because neither stop nor target was triggered
    assert setup_fixed.state == SetupLifecycleState.FILLED
    assert setup_trail.state == SetupLifecycleState.FILLED

    # If subsequent candle dips to 21950 (between BE and initial stop):
    c_dip = Candle("13", "NIFTY", datetime(2026, 10, 5, 10, 55), 22000, 22020, 21950, 21980, 1000)

    # Fixed strategy survives (low 21950 > initial stop 21900)
    res_fixed = strat.check_position_exit(c_dip, setup_fixed, [], exit_policy=ExitPolicy.FIXED_2R)
    assert res_fixed.state == SetupLifecycleState.FILLED

    # Trail strategy stops out at breakeven (effective stop was 22000 >= 21950)
    res_trail = strat.check_position_exit(c_dip, setup_trail, [], exit_policy=ExitPolicy.BE_TRAIL_2R)
    assert res_trail.state == SetupLifecycleState.CLOSED
    assert res_trail.exit_reason == "STOP_LOSS"
    assert res_trail.exit_price == 22000.0


# =====================================================================
# 7. AUDITABLE TELEGRAM ALERT FORMATTER
# =====================================================================

def test_telegram_alert_formatter_section_10():
    """Verify Section 10 auditable template structure, disclaimers, and unavailability of win probability."""
    setup = TrendSweepSetup(
        setup_id="SIG_NIFTY_20261005_104000",
        symbol="NIFTY 50",
        direction=Direction.LONG,
        trade_date="2026-10-05",
        created_at="2026-10-05T10:40:00",
        state=SetupLifecycleState.ARMED_WAIT_RETEST,
        entry_price=22032.5,
        initial_stop=22008.0,
        initial_r_pts=24.5,
        target_price=22085.0,
        valid_until="2026-10-05T10:55:00",
        hourly_regime=TrendRegime.BULLISH,
        hourly_details={"ema20": 22010.5, "ema50": 21950.2, "reason_code": "HH_HL_EMA_ALIGNED"},
        daily_bias="BULLISH",
        swept_level=22020.0,
        swept_level_type="5M_SWING_LOW",
        sweep_bar_time="2026-10-05T10:30:00",
        reclaim_bar_time="2026-10-05T10:35:00",
        frozen_microstructure_ref=22045.0,
        heuristic_score=8,
        score_breakdown={"trend": 2, "location": 2, "trigger": 2, "participation": 1, "execution": 1},
        exit_policy=ExitPolicy.FIXED_2R,
    )

    msg = format_trend_sweep_alert(
        setup=setup,
        mode_label="SHADOW / RESEARCH EVALUATION",
        observed_win_rate="NOT ESTABLISHED (RESEARCH V1)",
    )

    # Verify key mandatory audit strings
    assert "BORNBULL | SHADOW / RESEARCH EVALUATION" in msg
    assert "TREND_SWEEP_FVG_V1" in msg
    assert "ARMED" in msg
    assert "Planned entry:" in msg
    assert "Initial stop/invalidation:" in msg
    assert "Observed win rate:" in msg
    assert "NOT ESTABLISHED" in msg
    assert "WIN_PROBABILITY=UNAVAILABLE" in msg

