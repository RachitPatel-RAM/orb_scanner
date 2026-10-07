"""
Unit Tests for ORB Strategy Engine.
"""

from datetime import datetime, date, time
import pytest
import zoneinfo

from app.config import EntryConfig, RiskConfig, SessionConfig, StrategyConfig
from app.market.session import IST_TZ
from app.storage.models import Candle, Direction, ORBLevels, Signal
from app.strategies.orb import ORBStrategy


@pytest.fixture
def base_config():
    return StrategyConfig(
        name="ORB-15",
        session=SessionConfig(
            market_open="09:15",
            orb_end="09:30",
            entry_end="15:25",
            market_close="15:30",
        ),
        signal_timeframe=5,
        entry=EntryConfig(
            confirmation="candle_close",
            breakout_buffer_pct=0.0,
            max_signals_per_symbol_per_day=1,
            direction="both",
        ),
        risk=RiskConfig(
            stop_method="opposite_or",
            fixed_stop_pct=0.5,
            risk_reward=2.0,
            min_tick_size=0.05,
        ),
    )


def make_candle(
    sec_id: str,
    symbol: str,
    dt_str: str,
    open_: float,
    high: float,
    low: float,
    close: float,
    vol: float = 1000.0,
    is_closed: bool = True,
) -> Candle:
    dt = datetime.fromisoformat(dt_str).replace(tzinfo=IST_TZ)
    return Candle(
        security_id=sec_id,
        symbol=symbol,
        timestamp=dt,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=vol,
        is_closed=is_closed,
    )


def test_orb_high_low_calculation(base_config):
    strategy = ORBStrategy(config=base_config)
    d = date(2026, 10, 1)
    strategy.reset_day(d)

    # 09:15 candle (ignored because orb_start is 09:30)
    c0 = make_candle("100", "RELIANCE", "2026-10-01T09:15:00", 2400, 2600, 2300, 2450)
    # 2 x 15m benchmark candles: 09:30-09:45 and 09:45-10:00
    c1 = make_candle("100", "RELIANCE", "2026-10-01T09:30:00", 2500, 2530, 2500, 2520)
    c2 = make_candle("100", "RELIANCE", "2026-10-01T09:45:00", 2520, 2525, 2490, 2515)

    strategy.register_orb_candle(c0)
    strategy.register_orb_candle(c1)
    strategy.register_orb_candle(c2)

    orb = strategy.finalize_orb_levels(d, "100", "RELIANCE")
    assert orb is not None
    assert orb.high == 2530.0  # Max high across 09:30 and 09:45
    assert orb.low == 2490.0   # Min low across 09:30 and 09:45
    assert orb.mid == 2510.0   # (2530 + 2490) / 2


def test_no_breakout_before_1000(base_config):
    strategy = ORBStrategy(config=base_config)
    d = date(2026, 10, 1)
    strategy.reset_day(d)

    c1 = make_candle("100", "RELIANCE", "2026-10-01T09:15:00", 2500, 2520, 2490, 2510)
    c2 = make_candle("100", "RELIANCE", "2026-10-01T09:30:00", 2510, 2550, 2505, 2545)
    c3 = make_candle("100", "RELIANCE", "2026-10-01T09:45:00", 2545, 2560, 2540, 2555)

    # Candles starting before 10:00 must NEVER return a breakout signal
    assert strategy.on_candle_closed(c1) is None
    assert strategy.on_candle_closed(c2) is None
    assert strategy.on_candle_closed(c3) is None


def test_long_breakout_confirmation_and_levels(base_config):
    strategy = ORBStrategy(config=base_config)
    d = date(2026, 10, 1)
    strategy.reset_day(d)

    # Establish ORB: High=2500, Low=2450
    orb = ORBLevels(trade_date=d, security_id="100", symbol="RELIANCE", high=2500.0, low=2450.0, mid=2475.0, is_complete=True)
    strategy.set_orb_levels(orb)

    # Previous candle inside range: close=2490 (<= 2500)
    c_prev = make_candle("100", "RELIANCE", "2026-10-01T10:00:00", 2480, 2495, 2475, 2490)
    sig1 = strategy.on_candle_closed(c_prev)
    assert sig1 is None

    # Current candle breaks out: close=2510 (> 2500)
    c_break = make_candle("100", "RELIANCE", "2026-10-01T10:15:00", 2490, 2515, 2485, 2510)
    sig2 = strategy.on_candle_closed(c_break)

    assert sig2 is not None
    assert sig2.direction == Direction.LONG
    assert sig2.entry_price == 2510.0
    assert sig2.stop_loss == 2450.0  # opposite_or SL = ORB_LOW
    # Risk = 2510 - 2450 = 60. Target = 2510 + 60 * 2 = 2630
    assert sig2.target == 2630.0
    assert sig2.risk_reward == 2.0


def test_short_breakout_confirmation_and_levels(base_config):
    strategy = ORBStrategy(config=base_config)
    d = date(2026, 10, 1)
    strategy.reset_day(d)

    orb = ORBLevels(trade_date=d, security_id="200", symbol="TCS", high=4000.0, low=3950.0, mid=3975.0, is_complete=True)
    strategy.set_orb_levels(orb)

    # Previous candle inside range: close=3960 (>= 3950)
    c_prev = make_candle("200", "TCS", "2026-10-01T10:00:00", 3970, 3975, 3955, 3960)
    assert strategy.on_candle_closed(c_prev) is None

    # Current candle breaks down: close=3940 (< 3950)
    c_break = make_candle("200", "TCS", "2026-10-01T10:15:00", 3960, 3965, 3935, 3940)
    sig = strategy.on_candle_closed(c_break)

    assert sig is not None
    assert sig.direction == Direction.SHORT
    assert sig.entry_price == 3940.0
    assert sig.stop_loss == 4000.0  # opposite_or SL = ORB_HIGH
    # Risk = 4000 - 3940 = 60. Target = 3940 - 60 * 2 = 3820
    assert sig.target == 3820.0


def test_per_symbol_trade_limit(base_config):
    base_config.entry.max_signals_per_symbol_per_day = 1
    strategy = ORBStrategy(config=base_config)
    d = date(2026, 10, 1)
    strategy.reset_day(d)

    orb = ORBLevels(trade_date=d, security_id="100", symbol="RELIANCE", high=2500.0, low=2450.0, mid=2475.0, is_complete=True)
    strategy.set_orb_levels(orb)

    c0 = make_candle("100", "RELIANCE", "2026-10-01T10:00:00", 2480, 2490, 2470, 2485)
    strategy.on_candle_closed(c0)

    # First breakout signal
    c1 = make_candle("100", "RELIANCE", "2026-10-01T10:15:00", 2485, 2510, 2480, 2505)
    sig1 = strategy.on_candle_closed(c1)
    assert sig1 is not None

    # Pullback and second breakout on same symbol - MUST BE REJECTED
    c2 = make_candle("100", "RELIANCE", "2026-10-01T10:30:00", 2505, 2508, 2495, 2498)
    strategy.on_candle_closed(c2)

    c3 = make_candle("100", "RELIANCE", "2026-10-01T10:45:00", 2498, 2520, 2495, 2515)
    sig2 = strategy.on_candle_closed(c3)
    assert sig2 is None, "Should not generate second signal for same symbol on the same day"


def test_breakout_buffer(base_config):
    base_config.entry.breakout_buffer_pct = 0.5  # 0.5% buffer
    strategy = ORBStrategy(config=base_config)
    d = date(2026, 10, 1)
    strategy.reset_day(d)

    # ORB High = 1000. Buffered level = 1000 * 1.005 = 1005.0
    orb = ORBLevels(trade_date=d, security_id="300", symbol="INFY", high=1000.0, low=950.0, mid=975.0, is_complete=True)
    strategy.set_orb_levels(orb)

    c0 = make_candle("300", "INFY", "2026-10-01T10:00:00", 980, 990, 975, 985)
    strategy.on_candle_closed(c0)

    # Close at 1002 is > 1000 raw high, but < 1005 buffered level -> NO SIGNAL
    c1 = make_candle("300", "INFY", "2026-10-01T10:15:00", 985, 1004, 980, 1002)
    assert strategy.on_candle_closed(c1) is None

    # Close at 1008 is > 1005 -> VALID SIGNAL
    c2 = make_candle("300", "INFY", "2026-10-01T10:30:00", 1002, 1010, 1000, 1008)
    sig = strategy.on_candle_closed(c2)
    assert sig is not None
    assert sig.direction == Direction.LONG


def test_orb_midpoint_stop(base_config):
    base_config.risk.stop_method = "orb_midpoint"
    strategy = ORBStrategy(config=base_config)
    d = date(2026, 10, 1)

    orb = ORBLevels(trade_date=d, security_id="100", symbol="RELIANCE", high=2500.0, low=2400.0, mid=2450.0, is_complete=True)
    sl = strategy.calculate_stop_loss(Direction.LONG, entry_price=2510.0, orb=orb)
    assert sl == 2450.0


def test_whipsaw_double_sided_breach_filter(base_config):
    """Verifies that if price breaches/sweeps ORB Low earlier, a subsequent LONG breakout is rejected as chop/whipsaw."""
    strategy = ORBStrategy(config=base_config)
    d = date(2026, 10, 1)
    strategy.reset_day(d)

    orb = ORBLevels(trade_date=d, security_id="500", symbol="HINDPETRO", high=350.0, low=340.0, mid=345.0, is_complete=True)
    strategy.set_orb_levels(orb)

    # 1. Neutral candle inside range
    c0 = make_candle("500", "HINDPETRO", "2026-10-01T10:00:00", 344, 346, 342, 345)
    strategy.on_candle_closed(c0)

    # 2. Candle breaches below ORB Low (340.0) -> marks breached_low = True
    c_low_sweep = make_candle("500", "HINDPETRO", "2026-10-01T10:30:00", 345, 346, 338, 341)
    strategy.on_candle_closed(c_low_sweep)
    assert orb.breached_low is True

    # 3. Later, stock rallies and closes above ORB High (350.0) -> MUST BE REJECTED AS WHIPSAW
    c_long_breakout = make_candle("500", "HINDPETRO", "2026-10-01T12:00:00", 348, 353, 347, 352)
    sig = strategy.on_candle_closed(c_long_breakout)
    assert sig is None, "Long breakout must be blocked because ORB Low was already breached earlier today (whipsaw/chop)."
