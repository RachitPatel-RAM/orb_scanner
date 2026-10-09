"""
Unit Tests for Paper Tracker and Trade Lifecycle.
"""

from datetime import datetime, date
import pytest

from app.config import BacktestCosts
from app.market.session import IST_TZ
from app.storage.database import db
from app.storage.models import Candle, Direction, ExitReason, Signal
from app.trading.paper_tracker import PaperTracker, calculate_trade_costs


@pytest.fixture(autouse=True)
def clean_test_trades():
    yield
    with db.get_connection() as conn:
        conn.execute("DELETE FROM paper_account_ledger WHERE trade_id IS NULL OR trade_id IN (SELECT id FROM paper_trades WHERE signal_id IS NULL)")
        conn.execute("DELETE FROM paper_trades WHERE signal_id IS NULL")


def test_target_and_stop_hit():
    target_hits = []
    stop_hits = []

    tracker = PaperTracker(
        on_target_hit=lambda t: target_hits.append(t),
        on_stop_hit=lambda t: stop_hits.append(t),
    )

    sig = Signal(
        trade_date=date(2026, 10, 1),
        security_id="100",
        symbol="RELIANCE",
        strategy="ORB-15",
        direction=Direction.LONG,
        timestamp=datetime(2026, 10, 1, 9, 35, 0, tzinfo=IST_TZ),
        entry_price=2500.0,
        orb_high=2490.0,
        orb_low=2450.0,
        stop_loss=2450.0,
        target=2600.0,
        risk_reward=2.0,
        idempotency_key="key1",
    )

    trade = tracker.open_trade_from_signal(sig)
    assert trade.status == "OPEN"

    # Candle reaches target: high=2605
    c_target = Candle(
        security_id="100",
        symbol="RELIANCE",
        timestamp=datetime(2026, 10, 1, 9, 45, 0, tzinfo=IST_TZ),
        open=2550.0,
        high=2605.0,
        low=2540.0,
        close=2595.0,
        volume=5000.0,
        is_closed=True,
    )

    closed = tracker.update_with_candle(c_target)
    assert len(closed) == 1
    assert closed[0].exit_reason == ExitReason.TARGET
    assert closed[0].exit_price == 2600.0
    assert closed[0].pnl == 100.0  # 2600 - 2500
    assert closed[0].r_multiple == 2.0  # 100 / 50 risk
    assert len(target_hits) == 1


def test_conservative_tie_break_policy():
    """
    CRITICAL REALISM TEST:
    If both Target and Stop Loss are breached within the same bar,
    the conservative rule MUST declare Stop Loss hit first.
    """
    tracker = PaperTracker()

    sig = Signal(
        trade_date=date(2026, 10, 1),
        security_id="200",
        symbol="TCS",
        strategy="ORB-15",
        direction=Direction.LONG,
        timestamp=datetime(2026, 10, 1, 9, 35, 0, tzinfo=IST_TZ),
        entry_price=3000.0,
        orb_high=2980.0,
        orb_low=2950.0,
        stop_loss=2950.0,
        target=3100.0,
        risk_reward=2.0,
        idempotency_key="key2",
    )

    trade = tracker.open_trade_from_signal(sig)

    # Volatile candle touches both high 3120 (target) and low 2940 (stop)
    c_wild = Candle(
        security_id="200",
        symbol="TCS",
        timestamp=datetime(2026, 10, 1, 9, 40, 0, tzinfo=IST_TZ),
        open=3000.0,
        high=3120.0,
        low=2940.0,
        close=3020.0,
        volume=10000.0,
        is_closed=True,
    )

    closed = tracker.update_with_candle(c_wild)
    assert len(closed) == 1
    assert closed[0].exit_reason == ExitReason.STOP_LOSS, "Must assume worst-case execution on conflict"
    assert closed[0].exit_price == 2950.0
    assert closed[0].pnl == -50.0
    assert closed[0].r_multiple == -1.0


def test_regulatory_cost_calculation():
    costs = BacktestCosts(
        brokerage_pct=0.03,
        max_brokerage_per_order=20.0,
        stt_pct=0.025,
        exchange_charges_pct=0.00297,
        gst_pct=18.0,
        sebi_turnover_pct=0.0001,
        stamp_duty_pct=0.003,
        slippage_pct=0.02,
    )

    breakdown = calculate_trade_costs(
        entry_price=2500.0,
        exit_price=2600.0,
        quantity=1,
        direction=Direction.LONG,
        costs=costs,
    )

    assert breakdown.brokerage > 0
    assert breakdown.stt > 0
    assert breakdown.exchange_charges > 0
    assert breakdown.gst > 0
    assert breakdown.total_charges > 0
