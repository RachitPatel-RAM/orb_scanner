"""
Unit tests for Smart Money Concepts (SMC) Engine:
- Fair Value Gap (FVG) detection
- Liquidity Sweep detection
- Displacement detection
- Hidden Liquidity (Origin Retest) strategy
"""

from datetime import datetime
import pytest

from app.storage.models import Candle, Direction
from app.strategies.smc import smc_engine, FairValueGap, LiquiditySweep


def test_fvg_detection_bullish_and_bearish():
    # Construct 3 candles for Bullish FVG
    c1 = Candle(security_id="13", symbol="NIFTY", timestamp=datetime(2026, 10, 5, 9, 30), open=22500, high=22520, low=22490, close=22510, volume=1000)
    c2 = Candle(security_id="13", symbol="NIFTY", timestamp=datetime(2026, 10, 5, 9, 45), open=22515, high=22600, low=22510, close=22590, volume=5000)
    c3 = Candle(security_id="13", symbol="NIFTY", timestamp=datetime(2026, 10, 5, 10, 0), open=22595, high=22620, low=22540, close=22610, volume=2000)

    fvgs = smc_engine.detect_fvg([c1, c2, c3])
    assert len(fvgs) == 1
    fvg = fvgs[0]
    assert fvg.direction == Direction.LONG
    assert fvg.bottom == 22520  # c1.high
    assert fvg.top == 22540     # c3.low
    assert fvg.midpoint == 22530.0
    assert fvg.size == 20.0


def test_liquidity_sweep_detection():
    # Sweep of high (Bearish sweep)
    c_sweep_high = Candle(
        security_id="13",
        symbol="NIFTY",
        timestamp=datetime(2026, 10, 5, 10, 15),
        open=22550,
        high=22610,  # Sweeps 22590
        low=22540,
        close=22560, # Closes below 22590 with high upper wick
        volume=3000,
    )
    sweep = smc_engine.detect_liquidity_sweep(c_sweep_high, swing_high=22590, swing_low=22400)
    assert sweep is not None
    assert sweep.direction == Direction.SHORT
    assert sweep.extreme_price == 22610
    assert sweep.wick_ratio >= 0.28


def test_hidden_liquidity_strategy():
    c1 = Candle(security_id="13", symbol="NIFTY", timestamp=datetime(2026, 10, 5, 9, 30), open=22500, high=22510, low=22490, close=22505, volume=1000)
    # Origin candle
    c2 = Candle(security_id="13", symbol="NIFTY", timestamp=datetime(2026, 10, 5, 9, 45), open=22505, high=22520, low=22500, close=22515, volume=1200)
    # Strong displacement breakout
    c3 = Candle(security_id="13", symbol="NIFTY", timestamp=datetime(2026, 10, 5, 10, 0), open=22520, high=22600, low=22518, close=22590, volume=4000)
    c4 = Candle(security_id="13", symbol="NIFTY", timestamp=datetime(2026, 10, 5, 10, 15), open=22590, high=22605, low=22570, close=22600, volume=2000)

    setup = smc_engine.evaluate_hidden_liquidity_strategy(
        symbol="NIFTY",
        candles=[c1, c2, c3, c4],
        swing_high=22550,
        swing_low=22480,
    )
    assert setup is not None
    assert setup.strategy_name == "HIDDEN_LIQUIDITY"
    assert setup.direction == Direction.LONG
    assert setup.entry_price == c2.high  # Retest level is high of origin candle


def test_calculate_htf_trend():
    # Construct sequence of descending 15m candles (Downtrend)
    candles = [
        Candle(security_id="495213", symbol="GOLD", timestamp=datetime(2026, 10, 6, 17, 30), open=150500, high=150550, low=150450, close=150480, volume=100),
        Candle(security_id="495213", symbol="GOLD", timestamp=datetime(2026, 10, 6, 17, 45), open=150480, high=150500, low=150380, close=150420, volume=120),
        Candle(security_id="495213", symbol="GOLD", timestamp=datetime(2026, 10, 6, 18, 0), open=150420, high=150450, low=150220, close=150350, volume=150),
        Candle(security_id="495213", symbol="GOLD", timestamp=datetime(2026, 10, 6, 18, 15), open=150350, high=150360, low=150120, close=150270, volume=200),
        Candle(security_id="495213", symbol="GOLD", timestamp=datetime(2026, 10, 6, 18, 30), open=150270, high=150280, low=150100, close=150150, volume=180),
    ]
    trend, ema = smc_engine.calculate_htf_trend(candles)
    assert trend == Direction.SHORT
    assert ema > 150200


def test_fvg_strategy_blocks_counter_htf_setup():
    # Bullish 5m FVG candles
    c1 = Candle(security_id="495213", symbol="GOLD", timestamp=datetime(2026, 10, 6, 18, 35), open=150200, high=150220, low=150190, close=150210, volume=50)
    c2 = Candle(security_id="495213", symbol="GOLD", timestamp=datetime(2026, 10, 6, 18, 40), open=150210, high=150320, low=150205, close=150310, volume=200)
    c3 = Candle(security_id="495213", symbol="GOLD", timestamp=datetime(2026, 10, 6, 18, 45), open=150310, high=150350, low=150260, close=150340, volume=80)
    c4 = Candle(security_id="495213", symbol="GOLD", timestamp=datetime(2026, 10, 6, 18, 50), open=150340, high=150360, low=150320, close=150350, volume=90)

    # When 15m HTF Trend is BEARISH (SHORT), taking a 5m Bullish setup must be STRICTLY BLOCKED!
    setup = smc_engine.evaluate_fvg_strategy(
        symbol="GOLD",
        candles=[c1, c2, c3, c4],
        swing_high=150400,
        swing_low=150150,
        strict_filters=True,
        htf_trend=Direction.SHORT,
    )
    assert setup is None, "Counter-HTF setup must be strictly blocked!"

    # When 15m HTF Trend is BULLISH (LONG), the setup is allowed
    setup_long = smc_engine.evaluate_fvg_strategy(
        symbol="GOLD",
        candles=[c1, c2, c3, c4],
        swing_high=150400,
        swing_low=150150,
        strict_filters=False,
        htf_trend=Direction.LONG,
    )
    assert setup_long is not None
    assert setup_long.direction == Direction.LONG


@pytest.mark.asyncio
async def test_confluence_structural_target_and_sl():
    from app.strategies.confluence_engine import confluence_engine, PivotLevels
    from datetime import date

    # Mock pivots: Pivot=100, S1=95, S2=90, R1=105, R2=110, R3=115
    pivots = PivotLevels(pivot=100.0, r1=105.0, r2=110.0, r3=115.0, s1=95.0, s2=90.0, s3=85.0)
    confluence_engine._pivot_cache["TEST_STOCK"] = (date.today(), pivots)

    # Long breakout with entry at 102 (below R1) and candle low at 100.5
    c_long = Candle(security_id="999", symbol="TEST_STOCK", timestamp=datetime.now(), open=101.0, high=102.5, low=100.5, close=102.0, volume=5000)
    res = await confluence_engine.evaluate_confluence(
        security_id="999",
        symbol="TEST_STOCK",
        direction=Direction.LONG,
        entry_price=102.0,
        stop_loss=98.0,
        target=106.0,
        candle=c_long,
    )
    assert res.is_valid is True
    # Stop loss placed structurally at candle low - 0.05 = 100.45
    assert res.structural_stop_loss == 100.45
    # Target chosen dynamically from structural pivots (R1 or R2), not a blind rigid 1:2
    assert res.structural_target in (105.0, 110.0)
    assert res.structural_rr is not None
    assert res.structural_rr >= 1.4
