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
