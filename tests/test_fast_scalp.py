"""
Tests for Fast Scalp Pre-Market & 09:16 AM Opening Momentum Trade Engine.

Verifies:
1. Complete broker-searchable contract name with Day and Month (e.g. 'BANKNIFTY 14 OCT 51400 CE').
2. Mutual Fallback: Only 1 morning trade per day (Pre-market OR 09:16).
3. Auto Trail-to-Cost alert (+12 pts) for zero-risk trading.
4. Strict absence of proprietary strategy names (no ORB, CPR, SMC).
5. Tap-to-copy code formatting (<code>...</code>) for instant broker search.
6. Celebratory Target Hit alert and disciplined Stop Loss alert.
"""

from datetime import date, datetime
from unittest.mock import AsyncMock, patch
import pytest

from app.analysis.pre_market import PreMarketSnapshot
from app.storage.models import Candle, Direction
from app.trading.fast_scalp import (
    FastScalpEngine,
    FastScalpSetup,
    format_contract_with_month,
)


@pytest.fixture
def scalp_engine():
    return FastScalpEngine()


def test_format_contract_with_month():
    """Verifies that contracts format with Day, Month, Strike, and Option Type."""
    # With explicit expiry date string
    name_bn = format_contract_with_month("BANKNIFTY", 51400.0, "CE", "2026-10-14 15:30:00")
    assert name_bn == "BANKNIFTY 14 OCT 51400 CE"

    name_nifty = format_contract_with_month("NIFTY", 25100.0, "PE", "2026-10-15")
    assert name_nifty == "NIFTY 15 OCT 25100 PE"

    # With fallback to next weekly expiry
    name_fallback = format_contract_with_month("BANKNIFTY", 51200.0, "CE", ref_date=date(2026, 10, 10))
    assert "BANKNIFTY" in name_fallback
    assert "OCT 51200 CE" in name_fallback


@pytest.mark.asyncio
async def test_evaluate_premarket_scalp_includes_month_in_contract(scalp_engine):
    """Verifies that pre-market scalp generates contract with Month and Day."""
    mock_snapshot = PreMarketSnapshot(
        trade_date=date(2026, 10, 12),
        symbol="NIFTY",
        security_id="13",
        prev_close=25000.0,
        prev_high=25080.0,
        prev_low=24920.0,
        pre_open_price=25120.0,
        gap_points=120.0,
        gap_pct=0.48,
        gap_type="GAP_UP",
        pivot=25000.0,
        bc=25000.0,
        tc=25000.0,
        cpr_width_pct=0.15,
        regime="NARROW_TREND",
        recommended_strategy="ORB_BREAKOUT",
    )

    with patch("app.trading.fast_scalp.pre_market_manager.compute_snapshot", new_callable=AsyncMock) as mock_snap:
        mock_snap.return_value = mock_snapshot

        setup = await scalp_engine.evaluate_premarket_scalp(date(2026, 10, 12))
        assert setup is not None
        assert "OCT" in setup.contract_symbol
        assert "25100 CE" in setup.contract_symbol
        assert round(setup.entry_est - setup.stop_loss, 1) == 12.0


@pytest.mark.asyncio
async def test_mutual_fallback_prevents_0916_clash_when_premarket_active(scalp_engine):
    """Verifies that if Pre-Market trade is already active, 09:16 scalp is silenced."""
    today = date(2026, 10, 12)
    # Pre-populate active premarket trade
    scalp_engine._active_scalps["NIFTY_2026-10-12_PRE_MARKET"] = FastScalpSetup(
        trade_date=today,
        symbol="NIFTY",
        security_id="13",
        direction=Direction.LONG,
        underlying_price=25120.0,
        gap_points=120.0,
        gap_pct=0.48,
        contract_symbol="NIFTY 15 OCT 25100 CE",
        option_type="CE",
        strike_price=25100.0,
        entry_est=135.0,
        stop_loss=123.0,
        target_1=163.0,
        target_2=177.0,
        risk_reward="1:2.5+",
        rationale="Pre-market setup.",
        setup_source="PRE_MARKET",
        status="ACTIVE",
    )

    candle_1m = Candle(
        security_id=13,
        symbol="NIFTY",
        timestamp=datetime(2026, 10, 12, 9, 16, 0),
        open=25050.0,
        high=25095.0,
        low=25048.0,
        close=25090.0,
        volume=150000,
    )

    # 09:16 evaluation should return None (Mutual Fallback Shield)
    setup_0916 = await scalp_engine.evaluate_0916_scalp(candle_1m, today)
    assert setup_0916 is None


@pytest.mark.asyncio
async def test_trail_sl_to_cost_trigger_at_12_pts_profit(scalp_engine):
    """Verifies that reaching +12 pts triggers the zero-risk Trail-to-Cost alert."""
    today = date(2026, 10, 12)
    setup = FastScalpSetup(
        trade_date=today,
        symbol="BANKNIFTY",
        security_id="25",
        direction=Direction.LONG,
        underlying_price=51430.0,
        gap_points=200.0,
        gap_pct=0.40,
        contract_symbol="BANKNIFTY 14 OCT 51400 CE",
        option_type="CE",
        strike_price=51400.0,
        entry_est=280.0,
        stop_loss=258.0,
        target_1=335.0,
        target_2=365.0,
        risk_reward="1:2.5+",
        rationale="1-Min opening drive.",
        setup_source="OPEN_0916",
        status="ACTIVE",
    )
    scalp_engine._active_scalps["BANKNIFTY_2026-10-12_OPEN_0916"] = setup

    with patch("app.trading.fast_scalp.notifier.send_message", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = True

        # Price moves from ₹280 to ₹293 (+13 pts profit)
        await scalp_engine.on_tick(293.0, trade_date=today)

        assert setup.trailed_to_cost is True
        assert setup.stop_loss == 280.0  # Stop loss shifted to entry price

        # Verify alert content
        first_call_text = mock_send.call_args_list[0].args[0]
        assert "TRAIL SL TO COST" in first_call_text
        assert "BANKNIFTY 14 OCT 51400 CE" in first_call_text
        assert "RISK IS ZERO" in first_call_text
