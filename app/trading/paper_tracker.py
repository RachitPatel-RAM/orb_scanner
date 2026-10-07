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
    ):
        self.costs = costs or settings.strategy.costs
        self.on_target_hit = on_target_hit
        self.on_stop_hit = on_stop_hit
        self.on_eod_squareoff = on_eod_squareoff

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
                target_hit = candle.high >= trade.target
                stop_hit = candle.low <= trade.stop_loss

                if target_hit and stop_hit:
                    # CONSERVATIVE TIE-BREAKING POLICY:
                    # If both SL and Target are within the same candle range and intra-candle ticks
                    # are ambiguous, assume Stop Loss was hit first. Never fabricate optimistic results.
                    self._close_trade(trade, trade.stop_loss, candle_time, ExitReason.STOP_LOSS)
                    closed_trades.append(trade)
                    if self.on_stop_hit:
                        self.on_stop_hit(trade)
                elif target_hit:
                    self._close_trade(trade, trade.target, candle_time, ExitReason.TARGET)
                    closed_trades.append(trade)
                    if self.on_target_hit:
                        self.on_target_hit(trade)
                elif stop_hit:
                    self._close_trade(trade, trade.stop_loss, candle_time, ExitReason.STOP_LOSS)
                    closed_trades.append(trade)
                    if self.on_stop_hit:
                        self.on_stop_hit(trade)

            elif trade.direction == Direction.SHORT:
                target_hit = candle.low <= trade.target
                stop_hit = candle.high >= trade.stop_loss

                if target_hit and stop_hit:
                    # CONSERVATIVE TIE-BREAKING POLICY:
                    self._close_trade(trade, trade.stop_loss, candle_time, ExitReason.STOP_LOSS)
                    closed_trades.append(trade)
                    if self.on_stop_hit:
                        self.on_stop_hit(trade)
                elif target_hit:
                    self._close_trade(trade, trade.target, candle_time, ExitReason.TARGET)
                    closed_trades.append(trade)
                    if self.on_target_hit:
                        self.on_target_hit(trade)
                elif stop_hit:
                    self._close_trade(trade, trade.stop_loss, candle_time, ExitReason.STOP_LOSS)
                    closed_trades.append(trade)
                    if self.on_stop_hit:
                        self.on_stop_hit(trade)

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
                if ltp >= trade.target:
                    self._close_trade(trade, trade.target, t_time, ExitReason.TARGET)
                    closed_trades.append(trade)
                    if self.on_target_hit:
                        self.on_target_hit(trade)
                elif ltp <= trade.stop_loss:
                    self._close_trade(trade, trade.stop_loss, t_time, ExitReason.STOP_LOSS)
                    closed_trades.append(trade)
                    if self.on_stop_hit:
                        self.on_stop_hit(trade)
            elif trade.direction == Direction.SHORT:
                if ltp <= trade.target:
                    self._close_trade(trade, trade.target, t_time, ExitReason.TARGET)
                    closed_trades.append(trade)
                    if self.on_target_hit:
                        self.on_target_hit(trade)
                elif ltp >= trade.stop_loss:
                    self._close_trade(trade, trade.stop_loss, t_time, ExitReason.STOP_LOSS)
                    closed_trades.append(trade)
                    if self.on_stop_hit:
                        self.on_stop_hit(trade)

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

        risk_amount = abs(trade.entry_price - trade.stop_loss)

        if trade.direction == Direction.LONG:
            gross_pnl = exit_price - trade.entry_price
        else:
            gross_pnl = trade.entry_price - exit_price

        # R-Multiple calculation
        trade.r_multiple = (gross_pnl / risk_amount) if risk_amount > 0 else 0.0
        trade.pnl = round(gross_pnl, 2)

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
            )

        logger.info(
            f"Closed Virtual Trade #{trade.id} ({trade.symbol}): Reason={exit_reason.value} "
            f"@ ₹{exit_price:.2f} | PnL=₹{trade.pnl:.2f} | R={trade.r_multiple:.2f}R"
        )
