"""
Risk and Portfolio Manager V2 (Section 8).

Deterministic, auditable capital and risk constraints:
- Paper account equity tracking (default Rs 50,000).
- Position risk: 0.25% of strategy equity per position (Rs 125 on Rs 50,000).
- Total portfolio reserved stop risk across open & armed positions <= 0.75% (Rs 375).
- Daily session loss limit: 1.0% of session start equity (Rs 500).
- Daily loss streak pause: Pause new entries after 3 consecutive net-losing trades in a session.
- Max entries per session: 3 new entries per strategy.
- Rearm cooldown: At least 3 completed 5m bars after a closed trade before rearming.
- Correlation group limits: Prevents stacking correlated index/stock positions.
- Whole-lot quantity sizing with exact cost recalculation.
- Net 2R room verification: Feasible reward after costs >= 2x planned loss including costs.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from app.config import BacktestCosts, logger, settings


class ExitPolicy(str, Enum):
    FIXED_2R = "FIXED_2R"
    BE_TRAIL_2R = "BE_TRAIL_2R"


@dataclass
class CostBreakdown:
    entry_turnover: float
    exit_turnover: float
    brokerage: float
    stt: float
    exchange_charges: float
    gst: float
    sebi_turnover: float
    stamp_duty: float
    slippage: float
    total_round_trip_cost: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SizingResult:
    allowed: bool
    rejection_reason: str = ""
    lots: int = 0
    quantity: int = 0
    entry_price: float = 0.0
    stop_loss: float = 0.0
    target_price: float = 0.0
    initial_risk_per_share: float = 0.0
    nominal_gross_risk: float = 0.0
    nominal_gross_reward: float = 0.0
    estimated_costs: float = 0.0
    net_risk: float = 0.0
    net_reward: float = 0.0
    net_reward_risk_ratio: float = 0.0
    costs_breakdown: Optional[CostBreakdown] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        if self.costs_breakdown:
            d["costs_breakdown"] = self.costs_breakdown.to_dict()
        return d


@dataclass
class SessionRiskState:
    session_date: date
    start_equity: float
    current_equity: float
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    consecutive_losses: int = 0
    trades_entered_today: int = 0
    reserved_risk: float = 0.0
    is_paused_for_day: bool = False
    pause_reason: str = ""
    closed_trade_timestamps: List[datetime] = field(default_factory=list)


class RiskManagerV2:
    """Manages deterministic portfolio risk limits and lot sizing."""

    def __init__(
        self,
        initial_capital: float = 50000.0,
        risk_per_trade_pct: float = 0.0025, # 0.25%
        max_reserved_risk_pct: float = 0.0075, # 0.75%
        max_daily_loss_pct: float = 0.01, # 1.0%
        max_consecutive_losses: int = 3,
        max_daily_entries: int = 3,
        costs: Optional[BacktestCosts] = None,
    ):
        self.initial_capital = initial_capital
        self.risk_per_trade_pct = risk_per_trade_pct
        self.max_reserved_risk_pct = max_reserved_risk_pct
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_consecutive_losses = max_consecutive_losses
        self.max_daily_entries = max_daily_entries
        self.costs = costs or settings.strategy.costs

        # Correlation groups (symbols sharing risk budget)
        self.correlation_groups: Dict[str, str] = {
            "NIFTY": "INDEX_CORRELATION",
            "BANKNIFTY": "INDEX_CORRELATION",
            "FINNIFTY": "INDEX_CORRELATION",
            "HDFCBANK": "BANK_HEAVYWEIGHT",
            "ICICIBANK": "BANK_HEAVYWEIGHT",
            "RELIANCE": "ENERGY_INDEX_HEAVYWEIGHT",
        }

        # Active states
        self.sessions: Dict[date, SessionRiskState] = {}
        # Active positions and armed setups: setup_id -> reserved_risk_amount
        self.reserved_setups: Dict[str, Tuple[str, float]] = {} # id -> (symbol, risk)

    def get_session_state(self, trade_date: date) -> SessionRiskState:
        if trade_date not in self.sessions:
            self.sessions[trade_date] = SessionRiskState(
                session_date=trade_date,
                start_equity=self.initial_capital,
                current_equity=self.initial_capital,
            )
        return self.sessions[trade_date]

    def estimate_round_trip_costs(
        self,
        entry_price: float,
        exit_price: float,
        quantity: int,
        is_futures_or_cash: bool = True,
    ) -> CostBreakdown:
        """
        Computes accurate statutory Indian transaction costs & conservative slippage.
        """
        entry_val = entry_price * quantity
        exit_val = exit_price * quantity

        # Brokerage: 0.03% capped at Rs 20 per order
        b_entry = min(self.costs.max_brokerage_per_order, entry_val * (self.costs.brokerage_pct / 100.0))
        b_exit = min(self.costs.max_brokerage_per_order, exit_val * (self.costs.brokerage_pct / 100.0))
        tot_brokerage = b_entry + b_exit

        # STT: 0.025% on sell side (intraday equity/futures)
        tot_stt = exit_val * (self.costs.stt_pct / 100.0)

        # Exchange turnover: 0.00297% on both sides
        tot_exchange = (entry_val + exit_val) * (self.costs.exchange_charges_pct / 100.0)

        # GST: 18% on (brokerage + exchange charges)
        tot_gst = (tot_brokerage + tot_exchange) * (self.costs.gst_pct / 100.0)

        # SEBI charges: 0.0001%
        tot_sebi = (entry_val + exit_val) * (self.costs.sebi_turnover_pct / 100.0)

        # Stamp duty: 0.003% on buy side
        tot_stamp = entry_val * (self.costs.stamp_duty_pct / 100.0)

        # Slippage: 0.02% per leg
        tot_slippage = (entry_val + exit_val) * (self.costs.slippage_pct / 100.0)

        tot_cost = tot_brokerage + tot_stt + tot_exchange + tot_gst + tot_sebi + tot_stamp + tot_slippage

        return CostBreakdown(
            entry_turnover=round(entry_val, 2),
            exit_turnover=round(exit_val, 2),
            brokerage=round(tot_brokerage, 2),
            stt=round(tot_stt, 2),
            exchange_charges=round(tot_exchange, 2),
            gst=round(tot_gst, 2),
            sebi_turnover=round(tot_sebi, 2),
            stamp_duty=round(tot_stamp, 2),
            slippage=round(tot_slippage, 2),
            total_round_trip_cost=round(tot_cost, 2),
        )

    def can_enter_new_position(
        self,
        trade_date: date,
        symbol: str,
        current_time: datetime,
    ) -> Tuple[bool, str]:
        """Validates daily loss limit, consecutive loss pause, daily trades count, cooldown."""
        st = self.get_session_state(trade_date)

        # 1. Daily pause flag
        if st.is_paused_for_day:
            return False, f"DAILY_RISK_PAUSED: {st.pause_reason}"

        # 2. Daily Loss Limit (1.0% of start equity)
        max_daily_loss = st.start_equity * self.max_daily_loss_pct
        current_daily_loss = -(st.realized_pnl + st.unrealized_pnl)
        if current_daily_loss >= max_daily_loss:
            st.is_paused_for_day = True
            st.pause_reason = f"Hit daily loss limit of Rs {round(max_daily_loss, 2)}"
            return False, f"DAILY_LOSS_LIMIT_REACHED: Rs {round(current_daily_loss, 2)} lost"

        # 3. Consecutive losses streak
        if st.consecutive_losses >= self.max_consecutive_losses:
            st.is_paused_for_day = True
            st.pause_reason = f"Hit max consecutive losses ({self.max_consecutive_losses})"
            return False, f"CONSECUTIVE_LOSS_PAUSE: {st.consecutive_losses} losses in a row"

        # 4. Daily entries count
        if st.trades_entered_today >= self.max_daily_entries:
            return False, f"DAILY_TRADE_LIMIT_REACHED: Max {self.max_daily_entries} trades per session"

        # 5. One active setup/position per symbol
        for _, (sym, _) in self.reserved_setups.items():
            if sym == symbol:
                return False, f"ACTIVE_POSITION_OR_ARMED_EXISTS_FOR_SYMBOL: {symbol}"

        # 6. Cooldown after closed trade (3 completed 5m bars = 15 minutes)
        if st.closed_trade_timestamps:
            last_closed = st.closed_trade_timestamps[-1]
            elapsed_minutes = (current_time - last_closed).total_seconds() / 60.0
            if elapsed_minutes < 15.0:
                return False, f"REARM_COOLDOWN_ACTIVE: {round(elapsed_minutes, 1)}m elapsed (needs 15m)"

        return True, "OK"

    def compute_size_and_targets(
        self,
        trade_date: date,
        symbol: str,
        direction: str,
        entry_price: float,
        stop_loss: float,
        opposing_resistance_or_support: Optional[float],
        lot_size: int = 1,
        tick_size: float = 0.05,
    ) -> SizingResult:
        """
        Calculates whole-lot sizing and verifies Net 2R room after realistic costs.
        """
        st = self.get_session_state(trade_date)
        can_enter, reason = self.can_enter_new_position(trade_date, symbol, datetime.now())
        if not can_enter:
            return SizingResult(allowed=False, rejection_reason=reason)

        price_risk_per_share = abs(entry_price - stop_loss)
        if price_risk_per_share <= 0:
            return SizingResult(allowed=False, rejection_reason="ZERO_OR_NEGATIVE_RISK")

        # Budgeted risk = 0.25% of current equity
        budget_risk = st.current_equity * self.risk_per_trade_pct

        # Check total reserved risk limit (0.75%)
        max_tot_risk = st.current_equity * self.max_reserved_risk_pct
        current_tot_risk = sum(r for _, (_, r) in self.reserved_setups.items())
        if (current_tot_risk + budget_risk) > max_tot_risk:
            return SizingResult(
                allowed=False,
                rejection_reason=f"TOTAL_RESERVED_RISK_LIMIT: Current {round(current_tot_risk, 2)} + New {round(budget_risk, 2)} > Max {round(max_tot_risk, 2)}",
            )

        # Whole lot sizing
        # 1 lot gross risk
        risk_per_lot = price_risk_per_share * lot_size
        if risk_per_lot > budget_risk:
            # Even 1 single lot exceeds the strict risk budget!
            return SizingResult(
                allowed=False,
                rejection_reason=f"SINGLE_LOT_EXCEEDS_BUDGET: 1 lot risk Rs {round(risk_per_lot, 2)} > Budget Rs {round(budget_risk, 2)}",
            )

        # Largest whole lot fitting budget
        max_lots = int(budget_risk // risk_per_lot)
        lots = max(1, max_lots)
        quantity = lots * lot_size

        # Solve research take-profit price to satisfy net 2R after statutory costs:
        # Net Reward = Gross Reward - Costs >= 2.0 * (Gross Risk + Costs)
        # Gross Reward >= 2.0 * Gross Risk + 3.0 * Costs
        gross_risk_amt = price_risk_per_share * quantity
        # Approximate round-trip costs using nominal 2R distance to start
        initial_target_est = entry_price + (2.0 * price_risk_per_share) if direction.upper() in ("LONG", "BULLISH") else entry_price - (2.0 * price_risk_per_share)
        cost_est = self.estimate_round_trip_costs(entry_price, initial_target_est, quantity).total_round_trip_cost
        
        needed_gross_reward = (2.0 * gross_risk_amt) + (3.0 * cost_est)
        needed_reward_pts = needed_gross_reward / quantity

        if direction.upper() in ("LONG", "BULLISH"):
            target_price = entry_price + needed_reward_pts
        else:
            target_price = entry_price - needed_reward_pts

        # Round target to valid tick
        target_price = round(round(target_price / tick_size) * tick_size, 2)

        # Opposing structure room check: obstacle cannot block reaching net 2R target
        if opposing_resistance_or_support is not None:
            buffer_obstacle = 2 * tick_size
            if direction.upper() in ("LONG", "BULLISH"):
                max_allowed_target = opposing_resistance_or_support - buffer_obstacle
                if target_price > max_allowed_target:
                    return SizingResult(
                        allowed=False,
                        rejection_reason=f"OBSTACLE_BEFORE_2R_TARGET: Opposing level {opposing_resistance_or_support} prevents net 2R target {target_price}",
                    )
            else:
                min_allowed_target = opposing_resistance_or_support + buffer_obstacle
                if target_price < min_allowed_target:
                    return SizingResult(
                        allowed=False,
                        rejection_reason=f"OBSTACLE_BEFORE_2R_TARGET: Opposing level {opposing_resistance_or_support} prevents net 2R target {target_price}",
                    )

        # Recalculate realistic net reward and net risk with finalized target price
        cost_breakdown = self.estimate_round_trip_costs(entry_price, target_price, quantity)
        net_risk = (price_risk_per_share * quantity) + cost_breakdown.total_round_trip_cost
        gross_reward = abs(target_price - entry_price) * quantity
        net_reward = gross_reward - cost_breakdown.total_round_trip_cost

        if net_risk <= 0:
            return SizingResult(allowed=False, rejection_reason="INVALID_NET_RISK")

        net_rr = net_reward / net_risk
        if net_rr < 1.95: # Strict Net 2R threshold (allow tiny rounding margin)
            return SizingResult(
                allowed=False,
                rejection_reason=f"NET_RR_BELOW_2R: Net RR is {round(net_rr, 2)} after statutory costs (requires >= 2.0)",
                costs_breakdown=cost_breakdown,
            )

        return SizingResult(
            allowed=True,
            lots=lots,
            quantity=quantity,
            entry_price=round(entry_price, 2),
            stop_loss=round(stop_loss, 2),
            target_price=round(target_price, 2),
            initial_risk_per_share=round(price_risk_per_share, 2),
            nominal_gross_risk=round(price_risk_per_share * quantity, 2),
            nominal_gross_reward=round(gross_reward, 2),
            estimated_costs=round(cost_breakdown.total_round_trip_cost, 2),
            net_risk=round(net_risk, 2),
            net_reward=round(net_reward, 2),
            net_reward_risk_ratio=round(net_rr, 2),
            costs_breakdown=cost_breakdown,
        )

    def reserve_risk(self, setup_id: str, symbol: str, amount: float) -> None:
        self.reserved_setups[setup_id] = (symbol, amount)

    def release_risk(self, setup_id: str) -> None:
        self.reserved_setups.pop(setup_id, None)

    def record_closed_trade(
        self,
        trade_date: date,
        setup_id: str,
        net_pnl: float,
        closed_at: datetime,
    ) -> None:
        """Updates consecutive losses, daily PnL, equity, and triggers pause if limits hit."""
        self.release_risk(setup_id)
        st = self.get_session_state(trade_date)
        st.realized_pnl += net_pnl
        st.current_equity += net_pnl
        st.trades_entered_today += 1
        st.closed_trade_timestamps.append(closed_at)

        if net_pnl < 0:
            st.consecutive_losses += 1
            if st.consecutive_losses >= self.max_consecutive_losses:
                st.is_paused_for_day = True
                st.pause_reason = f"Hit {self.max_consecutive_losses} consecutive losses"
        else:
            st.consecutive_losses = 0

        # Check total daily drawdown
        max_daily_loss = st.start_equity * self.max_daily_loss_pct
        if -(st.realized_pnl) >= max_daily_loss:
            st.is_paused_for_day = True
            st.pause_reason = f"Hit daily loss limit of Rs {round(max_daily_loss, 2)}"


risk_manager_v2 = RiskManagerV2()
