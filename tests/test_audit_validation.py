"""
Unit Test Suite: Audit Validation & Capital Safety Verification.
Formally verifies the institutional criteria required by the audit:
1. Use one ORB implementation: 09:30-10:00 range, completed 15-minute confirmation, earliest signal 10:15.
   Separate five-minute index route removed.
2. Freeze the range: late, duplicate, or out-of-order candles cannot modify finalized levels.
3. Repair capital validation: calculate option purchase cost from validated price x quantity,
   include entry charges, and reject missing/non-finite inputs.
4. Repair order statuses: handle rejection/cancellation explicitly; missing order IDs must never count
   as successful submission.
5. Implement actual option paper trading: simulated fills, reserved cash, exits, costs, and an auditable ledger.
6. Zero broker calls when live execution is disabled.
7. Zero lookahead bias: future candles cannot change earlier signals.
"""

import math
from datetime import date, datetime, time
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from app.config import EntryConfig, RiskConfig, SessionConfig, StrategyConfig, settings
from app.market.session import IST_TZ, MarketSession
from app.storage.database import db
from app.storage.models import Candle, Direction, ExitReason, ORBLevels, PaperTrade, Signal
from app.strategies.orb import ORBStrategy
from app.trading.order_executor import DhanOrderExecutor
from app.trading.paper_tracker import PaperTracker
from app.dhan.option_finder import OptionFinder, OptionContractInfo, is_valid_price


def make_test_candle(
    sec_id: str,
    symbol: str,
    hour: int,
    minute: int,
    open_: float,
    high: float,
    low: float,
    close: float,
    is_closed: bool = True,
    t_date: Optional[date] = None,
) -> Candle:
    d = t_date or date(2026, 10, 9)
    dt = datetime(d.year, d.month, d.day, hour, minute, tzinfo=IST_TZ)
    return Candle(
        security_id=sec_id,
        symbol=symbol,
        timestamp=dt,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=5000.0,
        is_closed=is_closed,
    )


@pytest.fixture
def canonical_orb_strategy():
    """Strategy configured with canonical 09:30-10:00 range and 15m confirmation."""
    cfg = StrategyConfig(
        name="ORB-15",
        session=SessionConfig(
            market_open="09:15",
            orb_start="09:30",
            orb_end="10:00",
            entry_end="15:25",
            market_close="15:30",
        ),
        signal_timeframe=15,
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
    return ORBStrategy(config=cfg)


# ---------------------------------------------------------------------------
# Test 1: Paper mode makes zero broker order-submission calls
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_paper_mode_makes_zero_broker_order_submission_calls():
    executor = DhanOrderExecutor()
    mock_client = MagicMock()
    mock_client.place_order = MagicMock()
    mock_client.place_super_order = MagicMock()
    executor._dhan = mock_client

    order_data = {
        "security_id": "11536",
        "symbol": "TCS",
        "direction": "BUY",
        "entry_price": 3500.0,
        "stop_loss": 3480.0,
        "target": 3540.0,
        "lot_size": 1,
        "margin_req": 700.0,
    }

    # Verify when live trading is disabled
    with patch.object(settings, "live_order_enabled", False):
        status_category, success, msg = await executor.execute_dhan_order(order_data, lot_multiplier=1)

        assert status_category == "SIMULATED"
        assert success is True
        assert "Simulated Paper Order Logged" in msg
        assert "LIVE_ORDER_ENABLED=false" in msg
        # Zero calls made to Dhan order submission APIs
        mock_client.place_order.assert_not_called()
        mock_client.place_super_order.assert_not_called()


# ---------------------------------------------------------------------------
# Test 2: Missing or invalid quotes create zero trades and abort downstream
# (Seeded contract catalogue verifies mock quote function invocation)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_missing_or_invalid_quotes_create_zero_trades():
    # 2a. Direct quote validation
    assert not is_valid_price(None)
    assert not is_valid_price(0.0)
    assert not is_valid_price(0.40)  # below 0.50 threshold
    assert not is_valid_price(float("nan"))
    assert not is_valid_price(float("inf"))
    assert not is_valid_price(-10.5)
    assert is_valid_price(95.0)

    # 2b. Option finder rejects invalid quotes with seeded contract fixture
    finder = OptionFinder()
    finder._loaded = True
    finder._opt_index["NIFTY_CE"] = [
        {
            "security_id": "54321",
            "underlying": "NIFTY",
            "trading_symbol": "NIFTY 09 OCT 25000 CE",
            "custom_symbol": "NIFTY 25000 CE",
            "strike_price": 25000.0,
            "option_type": "CE",
            "expiry_date": "2026-10-15 15:30:00",
            "lot_size": 75,
            "exchange": "NSE",
            "exchange_segment": "NSE_FNO",
        }
    ]

    with patch.object(finder, "fetch_option_ltp", new_callable=AsyncMock) as mock_ltp:
        # None quote: verify fetch_option_ltp was called and contract rejected
        mock_ltp.return_value = None
        contract = await finder.find_atm_contract("NIFTY", 25000.0, Direction.LONG, target_date=date(2026, 10, 9))
        assert contract is None
        mock_ltp.assert_called_once()

        # NaN quote: rejected
        mock_ltp.reset_mock()
        mock_ltp.return_value = float("nan")
        contract = await finder.find_atm_contract("NIFTY", 25000.0, Direction.LONG, target_date=date(2026, 10, 9))
        assert contract is None
        mock_ltp.assert_called_once()

        # Inf quote: rejected
        mock_ltp.reset_mock()
        mock_ltp.return_value = float("inf")
        contract = await finder.find_atm_contract("NIFTY", 25000.0, Direction.LONG, target_date=date(2026, 10, 9))
        assert contract is None
        mock_ltp.assert_called_once()

        # Valid numeric quote: accepted
        mock_ltp.reset_mock()
        mock_ltp.return_value = 145.0
        contract = await finder.find_atm_contract("NIFTY", 25000.0, Direction.LONG, target_date=date(2026, 10, 9))
        assert contract is not None
        assert contract.ltp == 145.0
        assert contract.custom_symbol == "NIFTY 25000 CE"

    # 2c. Downstream order executor registration aborts index when opt_contract is None
    executor = DhanOrderExecutor()
    d = date(2026, 10, 9)
    idx_signal = Signal(
        trade_date=d,
        security_id="13",
        symbol="NIFTY",
        timestamp=datetime(2026, 10, 9, 10, 15, tzinfo=IST_TZ),
        strategy="ORB-15",
        direction=Direction.LONG,
        entry_price=25000.0,
        orb_high=24950.0,
        orb_low=24850.0,
        stop_loss=24850.0,
        target=25200.0,
        risk_reward=2.0,
        idempotency_key="2026-10-09_NIFTY_TEST",
    )
    markup, qty, margin = executor.register_signal_for_approval(idx_signal, opt_contract=None)
    assert markup is None
    assert qty == 0
    assert margin == 0.0


# ---------------------------------------------------------------------------
# Test 3: Insufficient funds produce skipped entry and unchanged balance
# (Calculates purchase cost + entry charges, rejects NaN/missing margin_req)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_insufficient_funds_produce_skipped_entry_and_unchanged_balance():
    executor = DhanOrderExecutor()
    mock_client = MagicMock()
    executor._dhan = mock_client

    # Set account balance to ₹2,500
    db.set_account_balance(2500.0)
    balance_before = db.get_account_balance(2500.0)
    assert balance_before == 2500.0

    # Option costing ₹6,175 (ltp=82.33, lot_size=75 -> ₹6,174.75 + ₹60 charges = ₹6,234.75)
    opt_info = OptionContractInfo(
        security_id="998877",
        underlying="NIFTY",
        custom_symbol="NIFTY 25000 CE",
        trading_symbol="NIFTY 25000 CE",
        strike_price=25000.0,
        option_type="CE",
        expiry_date="2026-10-15",
        lot_size=75,
        exchange_segment="NSE_FNO",
        ltp=82.33,
        stop_loss_premium=50.0,
        target_premium=140.0,
        margin_required=6175.0,
    )

    # NaN margin_req must not bypass guard
    order_data_nan = {
        "security_id": "13",
        "symbol": "NIFTY",
        "direction": "BUY",
        "entry_price": 25000.0,
        "stop_loss": 24900.0,
        "target": 25200.0,
        "lot_size": 75,
        "margin_req": float("nan"),
        "opt_contract": opt_info,
    }

    status_category, success, msg = await executor.execute_dhan_order(order_data_nan, lot_multiplier=1)

    # Must return FAILED, False (NOT STAGED, True!)
    assert status_category == "FAILED"
    assert success is False
    assert "Insufficient funds" in msg

    # Account balance in DB must remain strictly unchanged at ₹2,500
    balance_after = db.get_account_balance(2500.0)
    assert balance_after == 2500.0

    # Zero broker calls
    mock_client.place_order.assert_not_called()
    mock_client.place_super_order.assert_not_called()


# ---------------------------------------------------------------------------
# Test 4: Finalized ORB range is strictly immutable to late, duplicate or out-of-order candles
# ---------------------------------------------------------------------------
def test_finalized_range_is_immutable_to_late_candles(canonical_orb_strategy):
    strat = canonical_orb_strategy
    d = date(2026, 10, 9)
    strat.reset_day(d)

    # Normal morning candles: 09:30 and 09:45
    c1 = make_test_candle("13", "NIFTY", 9, 30, 25000, 25050, 24950, 25020, is_closed=True, t_date=d)
    c2 = make_test_candle("13", "NIFTY", 9, 45, 25020, 25080, 24980, 25060, is_closed=True, t_date=d)

    strat.register_orb_candle(c1)
    strat.register_orb_candle(c2)

    # Finalize range at 10:00 IST: High = 25,080, Low = 24,950
    orb = strat.finalize_orb_levels(d, "13", "NIFTY")
    assert orb is not None
    assert orb.high == 25080.0
    assert orb.low == 24950.0
    assert orb.is_complete is True

    # 4a. Late 09:45 candle with high 26,000 and low 24,000 must NOT mutate
    late_c = make_test_candle("13", "NIFTY", 9, 45, 25000, 26000, 24000, 25500, is_closed=True, t_date=d)
    result_orb = strat.register_orb_candle(late_c)
    assert result_orb is not None
    assert result_orb.high == 25080.0
    assert result_orb.low == 24950.0
    assert result_orb.is_complete is True

    # 4b. Out-of-order 09:20 or duplicate 09:30 candle must NOT mutate
    dup_c = make_test_candle("13", "NIFTY", 9, 30, 25000, 27000, 23000, 26500, is_closed=True, t_date=d)
    result_dup = strat.register_orb_candle(dup_c)
    assert result_dup.high == 25080.0
    assert result_dup.low == 24950.0


# ---------------------------------------------------------------------------
# Test 5: Index breakout scanner strictly rejects signals before 10:15 IST
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_indices_breakout_scanner_rejects_signals_before_1015():
    executor = DhanOrderExecutor()
    callback_mock = MagicMock()

    # Mock time at 10:06:00 IST (pre-10:15 cutoff)
    time_1006 = datetime(2026, 10, 9, 10, 6, 0, tzinfo=IST_TZ)

    with patch("app.market.session.default_session.now", return_value=time_1006):
        await executor.check_indices_breakouts(callback_mock)
        # Zero signals generated at 10:06 IST!
        callback_mock.assert_not_called()

    # Also test at 10:05 and 10:14:59 IST
    for test_time in [
        datetime(2026, 10, 9, 10, 5, 0, tzinfo=IST_TZ),
        datetime(2026, 10, 9, 10, 14, 59, tzinfo=IST_TZ),
    ]:
        with patch("app.market.session.default_session.now", return_value=test_time):
            await executor.check_indices_breakouts(callback_mock)
            callback_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Test 6: One single canonical ORBStrategy engine routes index breakout
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_indices_breakout_routes_through_canonical_orb_strategy(canonical_orb_strategy):
    d = date(2026, 10, 9)
    executor = DhanOrderExecutor(orb_strategy=canonical_orb_strategy)
    executor.orb_strategy.reset_day(d)

    callback_mock = MagicMock()
    time_1016 = datetime(2026, 10, 9, 10, 16, 0, tzinfo=IST_TZ)

    # 15m candles from Dhan: 09:15, 09:30, 09:45, 10:00 (closes at 10:15)
    # 09:30 candle (start 09:30, epoch 1791518400)
    dt_0930 = datetime(2026, 10, 9, 9, 30, tzinfo=IST_TZ)
    dt_0945 = datetime(2026, 10, 9, 9, 45, tzinfo=IST_TZ)
    dt_1000 = datetime(2026, 10, 9, 10, 0, tzinfo=IST_TZ)

    chart_data = {
        "timestamp": [dt_0930.timestamp(), dt_0945.timestamp(), dt_1000.timestamp()],
        "open": [25000.0, 25020.0, 25060.0],
        "high": [25050.0, 25080.0, 25130.0],
        "low": [24950.0, 24980.0, 25050.0],
        "close": [25020.0, 25060.0, 25120.0],  # 25120 > 25080 ORB High!
        "volume": [5000, 6000, 12000],
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = chart_data

    with patch("app.market.session.default_session.now", return_value=time_1016):
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
            await executor.check_indices_breakouts(callback_mock)

    # Callback must be called with a signal produced by ORBStrategy!
    assert callback_mock.called
    sig = callback_mock.call_args[0][0]
    assert sig.direction == Direction.LONG
    assert sig.orb_high == 25080.0
    assert sig.orb_low == 24950.0
    assert sig.entry_price == 25120.0


# ---------------------------------------------------------------------------
# Test 7: Broker response distinct statuses (REJECTED, CANCELLED, TRADED, PENDING)
# (Rejects orderStatus=REJECTED and missing/#N/A order ID)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_broker_response_distinct_statuses_handling():
    executor = DhanOrderExecutor()

    # 7a. orderStatus="REJECTED" must return FAILED, False
    res_rejected = {
        "status": "success",
        "data": {
            "orderId": "11223344",
            "orderStatus": "REJECTED",
        },
        "remarks": "Insufficient margin in client account",
    }
    status, success, msg = executor._parse_dhan_response(res_rejected, 75, 25000.0)
    assert status == "FAILED"
    assert success is False
    assert "REJECTED" in msg

    # 7b. Missing order ID or sentinel "#N/A" must return FAILED, False
    res_missing_id = {
        "status": "success",
        "data": {
            "orderId": "#N/A",
            "orderStatus": "PENDING",
        },
    }
    status, success, msg = executor._parse_dhan_response(res_missing_id, 75, 25000.0)
    assert status == "FAILED"
    assert success is False
    assert "without valid Order ID" in msg

    # 7c. orderStatus="CANCELLED" must return FAILED, False
    res_cancelled = {
        "status": "success",
        "data": {
            "orderId": "11223344",
            "orderStatus": "CANCELLED",
        },
        "remarks": "Cancelled by RMS",
    }
    status, success, msg = executor._parse_dhan_response(res_cancelled, 75, 25000.0)
    assert status == "FAILED"
    assert success is False
    assert "CANCELLED" in msg

    # 7d. orderStatus="TRADED" must return TRADED, True
    res_traded = {
        "status": "success",
        "data": {
            "orderId": "11223344",
            "orderStatus": "TRADED",
            "tradedQuantity": 75,
            "tradedPrice": 25000.0,
        },
    }
    status, success, msg = executor._parse_dhan_response(res_traded, 75, 25000.0)
    assert status == "TRADED"
    assert success is True
    assert "TRADED" in msg

    # 7e. orderStatus="PENDING" must return SUBMITTED, True
    res_pending = {
        "status": "success",
        "data": {
            "orderId": "11223344",
            "orderStatus": "PENDING",
        },
    }
    status, success, msg = executor._parse_dhan_response(res_pending, 75, 25000.0)
    assert status == "SUBMITTED"
    assert success is True
    assert "PENDING" in msg


# ---------------------------------------------------------------------------
# Test 8: Actual Option Paper Trading: Fills, Reserved Cash, Exits, Costs & Ledger
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_paper_option_execution_establishes_simulated_fill_and_ledger():
    executor = DhanOrderExecutor()
    tracker = PaperTracker()

    start_balance = 50000.0
    db.set_account_balance(start_balance)

    # 1 Lot NIFTY CE: 75 Qty @ ₹100.0 LTP -> Cost = ₹7,500.0, Entry Charges = ₹60.0
    # Total Required = ₹7,560.0
    opt_info = OptionContractInfo(
        security_id="998877",
        underlying="NIFTY",
        custom_symbol="NIFTY 25000 CE",
        trading_symbol="NIFTY 25000 CE",
        strike_price=25000.0,
        option_type="CE",
        expiry_date="2026-10-15",
        lot_size=75,
        exchange_segment="NSE_FNO",
        ltp=100.0,
        stop_loss_premium=70.0,
        target_premium=160.0,
        margin_required=7500.0,
    )

    order_data = {
        "security_id": "13",
        "symbol": "NIFTY",
        "direction": "BUY",
        "entry_price": 25000.0,
        "stop_loss": 24900.0,
        "target": 25200.0,
        "lot_size": 75,
        "opt_contract": opt_info,
    }

    # Step 1: Simulate option order fill
    with patch.object(settings, "live_order_enabled", False):
        status_category, success, msg = await executor.execute_dhan_order(order_data, lot_multiplier=1)

        assert status_category == "SIMULATED"
        assert success is True
        assert "Simulated Paper Option Order Filled" in msg
        assert "Trade ID: #" in msg

    # Verify cash reservation in DB: ₹50,000 - ₹7,560 = ₹42,440
    reserved_balance = db.get_account_balance()
    assert reserved_balance == 42440.0

    # Verify auditable ledger entries for entry
    ledger = db.get_paper_ledger(limit=10)
    tx_types = [entry["transaction_type"] for entry in ledger]
    assert "CASH_RESERVATION" in tx_types
    assert "ENTRY_CHARGES" in tx_types

    # Step 2: Simulate trade exit at Target premium (₹160.0)
    # Open trade object
    open_trades = db.get_open_paper_trades()
    assert len(open_trades) >= 1
    t_row = open_trades[0]
    trade_obj = PaperTrade(
        id=t_row["id"],
        signal_id=t_row["signal_id"],
        trade_date=date.today(),
        security_id=t_row["security_id"],
        symbol=t_row["symbol"],
        direction=Direction.LONG,
        entry_price=t_row["entry_price"],
        entry_time=datetime.now(tz=IST_TZ),
        stop_loss=t_row["stop_loss"],
        target=t_row["target"],
        quantity=t_row["quantity"],
        asset_type="OPTION",
        entry_charges=t_row["entry_charges"],
    )
    tracker.open_trades[trade_obj.id] = trade_obj

    # Close trade at target premium ₹160.0
    # Gross proceeds: 75 * 160 = ₹12,000.0
    # Gross PnL: ₹12,000 - ₹7,500 = +₹4,500.0
    # Exit Charges: ₹60.0. Total Charges = ₹60 + ₹60 = ₹120.0
    # Net Realized PnL: +₹4,380.0
    # Cash returned: ₹12,000 - ₹60 = ₹11,940.0
    # Ending account balance: ₹42,440 + ₹11,940 = ₹54,380.0 (Starting ₹50,000 + Net PnL ₹4,380)
    tracker._close_trade(trade_obj, exit_price=160.0, exit_time=datetime.now(tz=IST_TZ), exit_reason=ExitReason.TARGET)

    ending_balance = db.get_account_balance()
    assert ending_balance == 54380.0

    # Verify ledger entries for exit
    ledger_after = db.get_paper_ledger(limit=10)
    after_types = [entry["transaction_type"] for entry in ledger_after]
    assert "CASH_RELEASE" in after_types
    assert "EXIT_CHARGES" in after_types
    assert "REALIZED_PNL" in after_types


# ---------------------------------------------------------------------------
# Test 9: Changing future candles cannot change earlier signal (Zero Look-Ahead)
# ---------------------------------------------------------------------------
def test_future_candles_cannot_change_earlier_signal(canonical_orb_strategy):
    d = date(2026, 10, 9)

    def run_simulation(future_candles):
        strat = ORBStrategy(config=canonical_orb_strategy.config)
        strat.reset_day(d)

        # Baseline morning candles
        c1 = make_test_candle("13", "NIFTY", 9, 30, 25000, 25050, 24950, 25020, is_closed=True, t_date=d)
        c2 = make_test_candle("13", "NIFTY", 9, 45, 25020, 25080, 24980, 25060, is_closed=True, t_date=d)
        c_1015 = make_test_candle("13", "NIFTY", 10, 0, 25060, 25120, 25050, 25110, is_closed=True, t_date=d)

        strat.on_candle_closed(c1)
        strat.on_candle_closed(c2)
        sig = strat.on_candle_closed(c_1015)

        # Process future candles chronologically
        for fc in future_candles:
            strat.on_candle_closed(fc)

        return sig

    # Scenario A: Massive afternoon rally to 26000
    future_a = [
        make_test_candle("13", "NIFTY", 11, 0, 25110, 25500, 25100, 25480, is_closed=True, t_date=d),
        make_test_candle("13", "NIFTY", 14, 0, 25480, 26000, 25450, 25950, is_closed=True, t_date=d),
    ]

    # Scenario B: Severe market crash down to 24000
    future_b = [
        make_test_candle("13", "NIFTY", 11, 0, 25110, 25120, 24500, 24550, is_closed=True, t_date=d),
        make_test_candle("13", "NIFTY", 14, 0, 24550, 24600, 24000, 24050, is_closed=True, t_date=d),
    ]

    sig_a = run_simulation(future_a)
    sig_b = run_simulation(future_b)

    # The 10:15 signal must be 100% invariant to future prices (Zero Look-Ahead Bias)
    assert sig_a is not None
    assert sig_b is not None
    assert sig_a.direction == sig_b.direction == Direction.LONG
    assert sig_a.entry_price == sig_b.entry_price == 25110
    assert sig_a.stop_loss == sig_b.stop_loss
    assert sig_a.target == sig_b.target
