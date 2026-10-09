"""
Paper Signal Tracker and Virtual Trade Manager.

Maintains virtual positions for signal evaluation with conservative intra-candle
conflict resolution (SL assumed first on ambiguity) and realistic Indian market costs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, date
from typing import Callable, Dict, List, Optional

from app.config import BacktestCosts, logger, settings
from app.market.session import default_session
from app.storage.database import db
from app.storage.models import Candle, Direction, ExitReason, PaperTrade, Signal


@dataclass
class TradeCostBreakdown:
    brokerage: float
    stt: float
    exchange_charges: float
    gst: float
    sebi_charges: float
    stamp_duty: float
    slippage: float
    total_charges: float


def calculate_trade_costs(
    entry_price: float,
    exit_price: float,
    quantity: int,
    direction: Direction,
    costs: BacktestCosts,
) -> TradeCostBreakdown:
    """
    Calculates detailed statutory and regulatory trading costs for NSE Intraday Equity:
    - Brokerage: 0.03% or Rs 20 flat per order (whichever is lower)
    - STT: 0.025% on sell turnover
    - Exchange Transaction Charges: 0.00297% on both buy & sell
    - GST: 18% on (Brokerage + Exchange Charges)
    - SEBI Turnover Charges: 0.0001%
    - Stamp Duty: 0.003% on buy side only
    - Slippage: Applied per leg
    """
    buy_price = entry_price if direction == Direction.LONG else exit_price
    sell_price = exit_price if direction == Direction.LONG else entry_price

    buy_turnover = buy_price * quantity
    sell_turnover = sell_price * quantity
    total_turnover = buy_turnover + sell_turnover

    # Brokerage (min of pct or max per order, applied to both legs)
    buy_brok = min(buy_turnover * (costs.brokerage_pct / 100.0), costs.max_brokerage_per_order)
    sell_brok = min(sell_turnover * (costs.brokerage_pct / 100.0), costs.max_brokerage_per_order)
    total_brokerage = buy_brok + sell_brok

    # STT (Intraday equity STT applies on sell leg only)
    stt = sell_turnover * (costs.stt_pct / 100.0)

    # Exchange charges
    exch_charges = total_turnover * (costs.exchange_charges_pct / 100.0)

    # GST on (brokerage + exchange charges)
    gst = (total_brokerage + exch_charges) * (costs.gst_pct / 100.0)

    # SEBI turnover charges
    sebi = total_turnover * (costs.sebi_turnover_pct / 100.0)

    # Stamp duty (on buy leg only)
    stamp_duty = buy_turnover * (costs.stamp_duty_pct / 100.0)

    # Slippage per leg
    slippage = total_turnover * (costs.slippage_pct / 100.0)

    total_charges = total_brokerage + stt + exch_charges + gst + sebi + stamp_duty + slippage

    return TradeCostBreakdown(
        brokerage=round(total_brokerage, 2),
        stt=round(stt, 2),
        exchange_charges=round(exch_charges, 2),
        gst=round(gst, 2),
        sebi_charges=round(sebi, 2),
        stamp_duty=round(stamp_duty, 2),
        slippage=round(slippage, 2),
        total_charges=round(total_charges, 2),
    )


class PaperTracker:
    """Tracks active virtual trades, evaluates SL/Target hits, and handles EOD square-offs."""

    def __init__(
        self,
        costs: Optional[BacktestCosts] = None,
        on_target_hit: Optional[Callable[[PaperTrade], None]] = None,
        on_stop_hit: Optional[Callable[[PaperTrade], None]] = None,
        on_eod_squareoff: Optional[Callable[[PaperTrade], None]] = None,
        on_milestone_hit: Optional[Callable[[PaperTrade, int, float], None]] = None,
    ):
        self.costs = costs or settings.strategy.costs
        self.on_target_hit = on_target_hit
        self.on_stop_hit = on_stop_hit
        self.on_eod_squareoff = on_eod_squareoff
        self.on_milestone_hit = on_milestone_hit

        # Active open trades by ID
        self.open_trades: Dict[int, PaperTrade] = {}

    def open_trade_from_signal(self, signal: Signal, signal_id: Optional[int] = None) -> PaperTrade:
        """Creates and stores a virtual position from an ORB Signal."""
        trade_id = db.save_paper_trade(
            signal_id=signal_id,
            trade_date=signal.trade_date.isoformat(),
            security_id=signal.security_id,
            symbol=signal.symbol,
            direction=signal.direction.value,
            entry_price=signal.entry_price,
            entry_time=signal.timestamp.isoformat(),
            stop_loss=signal.stop_loss,
            target=signal.target,
        )

        trade = PaperTrade(
            id=trade_id,
            signal_id=signal_id,
            trade_date=signal.trade_date,
            security_id=signal.security_id,
            symbol=signal.symbol,
            direction=signal.direction,
            entry_price=signal.entry_price,
            entry_time=signal.timestamp,
            stop_loss=signal.stop_loss,
            target=signal.target,
            status="OPEN",
            target_1=signal.target_1 or signal.target,
            target_2=signal.target_2,
            target_3=signal.target_3,
            initial_stop_loss=signal.stop_loss,
        )

        self.open_trades[trade_id] = trade
        logger.info(
            f"Opened Virtual Trade #{trade_id} for {trade.symbol} {trade.direction.value} "
            f"@ ₹{trade.entry_price:.2f} | SL: ₹{trade.stop_loss:.2f} | Target: ₹{trade.target:.2f}"
        )
        return trade

    def load_open_trades_from_db(self, trade_date: Optional[date] = None) -> None:
        """Restores open trades from database across crashes/restarts."""
        date_str = trade_date.isoformat() if trade_date else None
        db_trades = db.get_open_paper_trades(date_str)
        for t in db_trades:
            trade_id = t["id"]
            if trade_id not in self.open_trades:
                self.open_trades[trade_id] = PaperTrade(
                    id=trade_id,
                    signal_id=t["signal_id"],
                    trade_date=date.fromisoformat(t["trade_date"]),
                    security_id=t["security_id"],
                    symbol=t["symbol"],
                    direction=Direction(t["direction"]),
                    entry_price=t["entry_price"],
                    entry_time=datetime.fromisoformat(t["entry_time"]),
                    stop_loss=t["stop_loss"],
                    target=t["target"],
                    status="OPEN",
                )
        logger.info(f"Loaded {len(self.open_trades)} active virtual trades from database.")

    def update_with_candle(self, candle: Candle) -> List[PaperTrade]:
        """
        Updates open trades matching candle's security_id.
        Evaluates Target, Stop Loss, and EOD square-off.
        Returns list of closed trades in this step.
        """
        sec_id = candle.security_id
        candle_time = default_session.localize(candle.timestamp)
        closed_trades: List[PaperTrade] = []

        matching_trades = [t for t in self.open_trades.values() if t.security_id == sec_id and t.symbol == candle.symbol]

        for trade in matching_trades:
            # Check EOD square-off first if candle timestamp >= 15:25
            if default_session.is_eod_squareoff(candle_time):
                self._close_trade(trade, candle.close, candle_time, ExitReason.EOD)
                closed_trades.append(trade)
                if self.on_eod_squareoff:
                    self.on_eod_squareoff(trade)
                continue

            # Evaluate Price Actions
            if trade.direction == Direction.LONG:
                final_target = trade.target_3 or trade.target_2 or trade.target
                current_sl = trade.stop_loss
                target_hit = candle.high >= final_target
                stop_hit = candle.low <= current_sl

                if target_hit and stop_hit:
                    # CONSERVATIVE TIE-BREAKING POLICY:
                    # If both SL and Target are within the same candle range and intra-candle ticks
                    # are ambiguous, assume Stop Loss was hit first. Never fabricate optimistic results.
                    self._close_trade(trade, current_sl, candle_time, ExitReason.STOP_LOSS)
                    closed_trades.append(trade)
                    if self.on_stop_hit:
                        self.on_stop_hit(trade)
                    continue
                elif stop_hit:
                    self._close_trade(trade, current_sl, candle_time, ExitReason.STOP_LOSS)
                    closed_trades.append(trade)
                    if self.on_stop_hit:
                        self.on_stop_hit(trade)
                    continue

                # 1. Milestone Target 1 (Adjust Stop Loss to Break-Even Entry)
                if trade.target_1 and not trade.target_1_hit:
                    if candle.high >= trade.target_1:
                        trade.target_1_hit = True
                        trade.stop_loss = max(trade.stop_loss, trade.entry_price)
                        logger.info(
                            f"Milestone Hit: {trade.symbol} Target 1 achieved at ₹{trade.target_1:,.2f}. "
                            f"SL moved to Entry ₹{trade.entry_price:,.2f}."
                        )
                        if self.on_milestone_hit:
                            self.on_milestone_hit(trade, 1, trade.target_1)

                # 2. Milestone Target 2 (Trail Stop Loss to Target 1)
                if trade.target_2 and trade.target_1_hit and not trade.target_2_hit:
                    if candle.high >= trade.target_2:
                        trade.target_2_hit = True
                        if trade.target_1:
                            trade.stop_loss = max(trade.stop_loss, trade.target_1)
                        logger.info(
                            f"Milestone Hit: {trade.symbol} Target 2 achieved at ₹{trade.target_2:,.2f}. "
                            f"SL trailed to Target 1 ₹{trade.stop_loss:,.2f}."
                        )
                        if self.on_milestone_hit:
                            self.on_milestone_hit(trade, 2, trade.target_2)

                if target_hit:
                    self._close_trade(trade, final_target, candle_time, ExitReason.TARGET)
                    closed_trades.append(trade)
                    if self.on_target_hit:
                        self.on_target_hit(trade)

            elif trade.direction == Direction.SHORT:
                final_target = trade.target_3 or trade.target_2 or trade.target
                current_sl = trade.stop_loss
                target_hit = candle.low <= final_target
                stop_hit = candle.high >= current_sl

                if target_hit and stop_hit:
                    # CONSERVATIVE TIE-BREAKING POLICY:
                    self._close_trade(trade, current_sl, candle_time, ExitReason.STOP_LOSS)
                    closed_trades.append(trade)
                    if self.on_stop_hit:
                        self.on_stop_hit(trade)
                    continue
                elif stop_hit:
                    self._close_trade(trade, current_sl, candle_time, ExitReason.STOP_LOSS)
                    closed_trades.append(trade)
                    if self.on_stop_hit:
                        self.on_stop_hit(trade)
                    continue

                # 1. Milestone Target 1 (Adjust Stop Loss to Break-Even Entry)
                if trade.target_1 and not trade.target_1_hit:
                    if candle.low <= trade.target_1:
                        trade.target_1_hit = True
                        trade.stop_loss = min(trade.stop_loss, trade.entry_price)
                        logger.info(
                            f"Milestone Hit: {trade.symbol} Target 1 achieved at ₹{trade.target_1:,.2f}. "
                            f"SL moved to Entry ₹{trade.entry_price:,.2f}."
                        )
                        if self.on_milestone_hit:
                            self.on_milestone_hit(trade, 1, trade.target_1)

                # 2. Milestone Target 2 (Trail Stop Loss to Target 1)
                if trade.target_2 and trade.target_1_hit and not trade.target_2_hit:
                    if candle.low <= trade.target_2:
                        trade.target_2_hit = True
                        if trade.target_1:
                            trade.stop_loss = min(trade.stop_loss, trade.target_1)
                        logger.info(
                            f"Milestone Hit: {trade.symbol} Target 2 achieved at ₹{trade.target_2:,.2f}. "
                            f"SL trailed to Target 1 ₹{trade.stop_loss:,.2f}."
                        )
                        if self.on_milestone_hit:
                            self.on_milestone_hit(trade, 2, trade.target_2)

                if target_hit:
                    self._close_trade(trade, final_target, candle_time, ExitReason.TARGET)
                    closed_trades.append(trade)
                    if self.on_target_hit:
                        self.on_target_hit(trade)

        return closed_trades

    def update_with_tick(self, sec_id: str, ltp: float, tick_time: datetime, symbol: Optional[str] = None) -> List[PaperTrade]:
        """Direct tick-level evaluation for live feed."""
        t_time = default_session.localize(tick_time)
        closed_trades: List[PaperTrade] = []

        matching_trades = [t for t in self.open_trades.values() if t.security_id == sec_id and (symbol is None or t.symbol == symbol)]

        for trade in matching_trades:
            if default_session.is_eod_squareoff(t_time):
                self._close_trade(trade, ltp, t_time, ExitReason.EOD)
                closed_trades.append(trade)
                if self.on_eod_squareoff:
                    self.on_eod_squareoff(trade)
                continue

            if trade.direction == Direction.LONG:
                final_target = trade.target_3 or trade.target_2 or trade.target
                current_sl = trade.stop_loss

                if ltp <= current_sl:
                    self._close_trade(trade, current_sl, t_time, ExitReason.STOP_LOSS)
                    closed_trades.append(trade)
                    if self.on_stop_hit:
                        self.on_stop_hit(trade)
                    continue

                # 1. Milestone Target 1 (Adjust Stop Loss to Break-Even Entry)
                if trade.target_1 and not trade.target_1_hit:
                    if ltp >= trade.target_1:
                        trade.target_1_hit = True
                        trade.stop_loss = max(trade.stop_loss, trade.entry_price)
                        logger.info(
                            f"Milestone Hit: {trade.symbol} Target 1 reached at ₹{trade.target_1:,.2f}. "
                            f"SL moved to Entry ₹{trade.entry_price:,.2f}."
                        )
                        if self.on_milestone_hit:
                            self.on_milestone_hit(trade, 1, trade.target_1)

                # 2. Milestone Target 2 (Trail Stop Loss to Target 1)
                if trade.target_2 and trade.target_1_hit and not trade.target_2_hit:
                    if ltp >= trade.target_2:
                        trade.target_2_hit = True
                        if trade.target_1:
                            trade.stop_loss = max(trade.stop_loss, trade.target_1)
                        logger.info(
                            f"Milestone Hit: {trade.symbol} Target 2 reached at ₹{trade.target_2:,.2f}. "
                            f"SL trailed to Target 1 ₹{trade.stop_loss:,.2f}."
                        )
                        if self.on_milestone_hit:
                            self.on_milestone_hit(trade, 2, trade.target_2)

                if ltp >= final_target:
                    self._close_trade(trade, final_target, t_time, ExitReason.TARGET)
                    closed_trades.append(trade)
                    if self.on_target_hit:
                        self.on_target_hit(trade)

            elif trade.direction == Direction.SHORT:
                final_target = trade.target_3 or trade.target_2 or trade.target
                current_sl = trade.stop_loss

                if ltp >= current_sl:
                    self._close_trade(trade, current_sl, t_time, ExitReason.STOP_LOSS)
                    closed_trades.append(trade)
                    if self.on_stop_hit:
                        self.on_stop_hit(trade)
                    continue

                # 1. Milestone Target 1 (Adjust Stop Loss to Break-Even Entry)
                if trade.target_1 and not trade.target_1_hit:
                    if ltp <= trade.target_1:
                        trade.target_1_hit = True
                        trade.stop_loss = min(trade.stop_loss, trade.entry_price)
                        logger.info(
                            f"Milestone Hit: {trade.symbol} Target 1 reached at ₹{trade.target_1:,.2f}. "
                            f"SL moved to Entry ₹{trade.entry_price:,.2f}."
                        )
                        if self.on_milestone_hit:
                            self.on_milestone_hit(trade, 1, trade.target_1)

                # 2. Milestone Target 2 (Trail Stop Loss to Target 1)
                if trade.target_2 and trade.target_1_hit and not trade.target_2_hit:
                    if ltp <= trade.target_2:
                        trade.target_2_hit = True
                        if trade.target_1:
                            trade.stop_loss = min(trade.stop_loss, trade.target_1)
                        logger.info(
                            f"Milestone Hit: {trade.symbol} Target 2 reached at ₹{trade.target_2:,.2f}. "
                            f"SL trailed to Target 1 ₹{trade.stop_loss:,.2f}."
                        )
                        if self.on_milestone_hit:
                            self.on_milestone_hit(trade, 2, trade.target_2)

                if ltp <= final_target:
                    self._close_trade(trade, final_target, t_time, ExitReason.TARGET)
                    closed_trades.append(trade)
                    if self.on_target_hit:
                        self.on_target_hit(trade)

        return closed_trades

    def _close_trade(
        self,
        trade: PaperTrade,
        exit_price: float,
        exit_time: datetime,
        exit_reason: ExitReason,
    ) -> None:
        """Finalizes trade calculations, calculates PnL and R-multiple, and updates DB."""
        trade.exit_price = exit_price
        trade.exit_time = exit_time
        trade.exit_reason = exit_reason
        trade.status = "CLOSED"

        orig_sl = trade.initial_stop_loss if trade.initial_stop_loss is not None else trade.stop_loss
        risk_amount = abs(trade.entry_price - orig_sl)

        qty = getattr(trade, "quantity", 1) or 1
        asset_type = getattr(trade, "asset_type", "EQUITY") or "EQUITY"

        if trade.direction == Direction.LONG:
            gross_pnl_per_unit = exit_price - trade.entry_price
        else:
            gross_pnl_per_unit = trade.entry_price - exit_price

        gross_pnl = gross_pnl_per_unit * qty

        if asset_type == "OPTION":
            # Indian F&O option exit regulatory charges: ~Rs 60 per executed lot
            lots = max(1, qty // 75)
            exit_charges = round(60.0 * lots, 2)
            entry_charges = getattr(trade, "entry_charges", 60.0 * lots) or (60.0 * lots)
            total_charges = round(entry_charges + exit_charges, 2)
            net_pnl = round(gross_pnl - total_charges, 2)

            # Cash release and ledger recording
            exit_value = round(exit_price * qty, 2)
            cash_returned = round(exit_value - exit_charges, 2)

            current_cap = db.get_account_balance()
            new_cap = round(current_cap + cash_returned, 2)
            db.set_account_balance(new_cap)

            db.record_ledger_entry(
                transaction_type="CASH_RELEASE",
                amount=exit_value,
                balance_before=current_cap,
                balance_after=current_cap + exit_value,
                description=f"Released gross option proceeds for {trade.symbol} (#{trade.id})",
                trade_id=trade.id,
            )
            db.record_ledger_entry(
                transaction_type="EXIT_CHARGES",
                amount=-exit_charges,
                balance_before=current_cap + exit_value,
                balance_after=new_cap,
                description=f"Statutory exit charges for {trade.symbol} (#{trade.id})",
                trade_id=trade.id,
            )
            db.record_ledger_entry(
                transaction_type="REALIZED_PNL",
                amount=net_pnl,
                balance_before=current_cap,
                balance_after=new_cap,
                description=f"Realized Net PnL for {trade.symbol} (#{trade.id})",
                trade_id=trade.id,
            )
        else:
            cost_bd = calculate_trade_costs(trade.entry_price, exit_price, qty, trade.direction, self.costs)
            exit_charges = round(cost_bd.total_charges / 2.0, 2)
            total_charges = cost_bd.total_charges
            net_pnl = round(gross_pnl - total_charges, 2)
            db.update_account_balance(net_pnl)

        trade.r_multiple = (gross_pnl_per_unit / risk_amount) if risk_amount > 0 else 0.0
        trade.pnl = round(gross_pnl, 2)
        trade.net_pnl = net_pnl

        if trade.id in self.open_trades:
            del self.open_trades[trade.id]

        if trade.id:
            db.close_paper_trade(
                trade_id=trade.id,
                exit_price=trade.exit_price,
                exit_time=trade.exit_time.isoformat(),
                exit_reason=trade.exit_reason.value,
                pnl=trade.pnl,
                r_multiple=round(trade.r_multiple, 2),
                exit_charges=exit_charges,
                net_pnl=net_pnl,
            )

        logger.info(
            f"Closed Virtual Trade #{trade.id} ({trade.symbol}): Reason={exit_reason.value} "
            f"@ ₹{exit_price:.2f} | Net PnL=₹{trade.pnl:.2f} | R={trade.r_multiple:.2f}R"
        )
