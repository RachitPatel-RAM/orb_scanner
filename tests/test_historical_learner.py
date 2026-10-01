"""
Unit tests for 5-Year Historical Empirical Learner and Compounding Capital.
"""

from datetime import date
import pytest

from app.storage.database import db
from app.strategies.historical_learner import historical_learner


def test_compounding_capital_balance():
    # Test initial retrieval
    initial = db.get_account_balance(4322.0)
    assert initial >= 4322.0

    # Test profit compounding
    updated = db.update_account_balance(250.0, 4322.0)
    assert updated == round(initial + 250.0, 2)

    # Test retrieval
    fetched = db.get_account_balance(4322.0)
    assert fetched == updated

    # Restore initial
    db.update_account_balance(-250.0, 4322.0)


def test_evaluate_multi_year_bars():
    # Synthetic multi-year bars with known breakout characteristics
    bars = []
    base_price = 1000.0
    for i in range(50):
        # alternate expansion and contraction
        high = base_price + 15.0
        low = base_price - 10.0
        open_p = base_price - 5.0
        close_p = base_price + 10.0
        vol = 100000.0 if i % 2 == 0 else 50000.0
        bars.append({
            "open": open_p,
            "high": high,
            "low": low,
            "close": close_p,
            "volume": vol,
            "timestamp": 1641148200.0 + i * 86400,
        })
        base_price += 2.0

    metrics = historical_learner.evaluate_multi_year_bars("TESTSYM", bars)
    assert "win_rate" in metrics
    assert "high_vol_win_rate" in metrics
    assert "low_vol_win_rate" in metrics
    assert metrics["sessions_analyzed"] == 50


def test_learned_model_recency_decay_and_prediction_influence():
    from datetime import datetime
    from app.storage.models import Candle, Direction
    from app.strategies.ml_learner import ml_learner

    with db.get_connection() as conn:
        conn.execute("DELETE FROM stock_learned_models WHERE symbol = 'TOP_ALPHA'")

    # 1. Save an initial model for a high-performing stock
    db.save_stock_learned_model(
        symbol="TOP_ALPHA",
        security_id="99901",
        win_rate=70.0,
        high_vol_win_rate=75.0,
        trap_rate=10.0,
        sessions_analyzed=100,
        optimal_vol_ratio=1.3,
    )

    # 2. Update with continuous learning (recency decay): 30% new (65%), 70% old (75%) -> ~72%
    db.save_stock_learned_model(
        symbol="TOP_ALPHA",
        security_id="99901",
        win_rate=65.0,
        high_vol_win_rate=65.0,
        trap_rate=12.0,
        sessions_analyzed=110,
        optimal_vol_ratio=1.3,
    )

    model = db.get_stock_learned_model("TOP_ALPHA")
    assert model is not None
    # 0.70 * 75.0 + 0.30 * 65.0 = 52.5 + 19.5 = 72.0
    assert abs(model["high_vol_win_rate"] - 72.0) < 0.2

    # 3. Create a breakout candle for TOP_ALPHA
    candle = Candle(
        security_id="99901",
        symbol="TOP_ALPHA",
        timestamp=datetime(2026, 10, 1, 10, 15),
        open=100.0,
        high=105.0,
        low=99.5,
        close=104.5,  # Solid body
        volume=150000,
        is_closed=True,
    )

    try:
        result = ml_learner.calculate_conviction_score(
            candle=candle,
            direction=Direction.LONG,
            orb_high=102.0,
            orb_low=98.0,
            avg_volume_20=100000.0,
        )

        # Must receive high conviction and learned win rate tag
        assert result.score >= 70
        assert result.learned_win_rate == model["high_vol_win_rate"]
        assert any("Learned Win Rate" in r for r in result.reasons)
    finally:
        with db.get_connection() as conn:
            conn.execute("DELETE FROM stock_learned_models WHERE symbol = 'TOP_ALPHA'")
