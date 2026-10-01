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
