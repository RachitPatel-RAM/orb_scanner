"""
Opening Range Breakout (ORB) Strategy Engine.

Implements exact ORB rules matching TradingView parity:
- 09:15:00 to 09:30:00 Opening Range calculation
- Closed 5-minute candle breakout checks only after 09:30:00
- Opposite OR / Midpoint / Fixed% Stop Loss and Configurable R:R Target
- Per-symbol daily signal limits and strict idempotency
- Full debug comparison mode for TradingView validation
"""

from __future__ import annotations

from datetime import datetime, date
from typing import Dict, List, Optional, Tuple

from app.config import StrategyConfig, logger, settings
from app.market.session import default_session
from app.storage.database import db
from app.storage.models import Candle, Direction, ORBLevels, Signal


class ORBStrategy:
    """Production ORB strategy engine shared between Live Trading and Backtesting."""

    def __init__(self, config: Optional[StrategyConfig] = None, debug_mode: bool = False):
        self.config = config or settings.strategy
        self.debug_mode = debug_mode

        # Daily state: trade_date -> security_id -> ORBLevels
        self.daily_orb: Dict[date, Dict[str, ORBLevels]] = {}

        # Tracking 1m candles during opening range: (trade_date, security_id) -> List[Candle]
        self.orb_candles_buffer: Dict[Tuple[date, str], List[Candle]] = {}

        # Previous closed 5m candle: (trade_date, security_id) -> Candle
        self.prev_closed_5m: Dict[Tuple[date, str], Candle] = {}

        # Daily signals count: (trade_date, security_id) -> int
        self.daily_signal_counts: Dict[Tuple[date, str], int] = {}

    def reset_day(self, trade_date: date) -> None:
        """Cleans memory for older days, keeping current day fresh."""
        old_dates = [d for d in self.daily_orb if d < trade_date]
        for d in old_dates:
            del self.daily_orb[d]
        for k in list(self.orb_candles_buffer.keys()):
            if k[0] < trade_date:
                del self.orb_candles_buffer[k]
        for k in list(self.prev_closed_5m.keys()):
            if k[0] < trade_date:
                del self.prev_closed_5m[k]
        for k in list(self.daily_signal_counts.keys()):
            if k[0] < trade_date:
                del self.daily_signal_counts[k]

    def register_orb_candle(self, candle: Candle) -> Optional[ORBLevels]:
        """
        Registers candles (1m or 5m) falling inside the 09:15:00 - 09:30:00 opening window.
        Returns the finalized ORBLevels once the opening range completes.
        """
        c_time = default_session.localize(candle.timestamp)
        t_date = c_time.date()
        sec_id = candle.security_id

        # Only process if strictly within 09:15:00 to 09:30:00
        if not default_session.is_orb_period(c_time):
            return self.get_orb_levels(t_date, sec_id)

        key = (t_date, sec_id)
        if key not in self.orb_candles_buffer:
            self.orb_candles_buffer[key] = []
        self.orb_candles_buffer[key].append(candle)
        # Keep track of last closed candle during ORB window so first post-10:00 candle can evaluate immediately
        self.prev_closed_5m[key] = candle

        # Update running levels
        if t_date not in self.daily_orb:
            self.daily_orb[t_date] = {}

        existing = self.daily_orb[t_date].get(sec_id)
        if existing is None:
            orb = ORBLevels(
                trade_date=t_date,
                security_id=sec_id,
                symbol=candle.symbol,
                high=candle.high,
                low=candle.low,
                mid=(candle.high + candle.low) / 2.0,
                is_complete=False,
            )
            self.daily_orb[t_date][sec_id] = orb
        else:
            existing.high = max(existing.high, candle.high)
            existing.low = min(existing.low, candle.low)
            existing.mid = (existing.high + existing.low) / 2.0

        return self.daily_orb[t_date][sec_id]

    def finalize_orb_levels(self, trade_date: date, security_id: str, symbol: str) -> Optional[ORBLevels]:
        """Marks the ORB levels as finalized and persists them."""
        key = (trade_date, security_id)
        candles = self.orb_candles_buffer.get(key, [])

        if not candles:
            # Check if already saved in DB
            db_levels = db.get_orb_levels(trade_date.isoformat(), security_id)
            if db_levels:
                orb = ORBLevels(
                    trade_date=trade_date,
                    security_id=security_id,
                    symbol=symbol,
                    high=db_levels["orb_high"],
                    low=db_levels["orb_low"],
                    mid=db_levels["orb_mid"],
                    is_complete=bool(db_levels["is_complete"]),
                )
                if trade_date not in self.daily_orb:
                    self.daily_orb[trade_date] = {}
                self.daily_orb[trade_date][security_id] = orb
                return orb
            return None

        orb_high = max(c.high for c in candles)
        orb_low = min(c.low for c in candles)
        orb_mid = (orb_high + orb_low) / 2.0

        orb = ORBLevels(
            trade_date=trade_date,
            security_id=security_id,
            symbol=symbol,
            high=orb_high,
            low=orb_low,
            mid=orb_mid,
            is_complete=True,
        )

        if trade_date not in self.daily_orb:
            self.daily_orb[trade_date] = {}
        self.daily_orb[trade_date][security_id] = orb

        # Persist to database
        db.save_orb_levels(
            trade_date=trade_date.isoformat(),
            security_id=security_id,
            symbol=symbol,
            orb_high=orb_high,
            orb_low=orb_low,
            orb_mid=orb_mid,
            is_complete=True,
        )

        logger.info(
            f"Finalized ORB for {symbol} on {trade_date}: High={orb_high:.2f}, Low={orb_low:.2f}, Mid={orb_mid:.2f}"
        )
        return orb

    def set_orb_levels(self, orb: ORBLevels) -> None:
        """Manually sets ORB levels (e.g. during recovery or backtest)."""
        t_date = orb.trade_date
        sec_id = orb.security_id
        if t_date not in self.daily_orb:
            self.daily_orb[t_date] = {}
        self.daily_orb[t_date][sec_id] = orb

    def get_orb_levels(self, trade_date: date, security_id: str) -> Optional[ORBLevels]:
        """Retrieves active ORB levels from memory or DB."""
        if trade_date in self.daily_orb and security_id in self.daily_orb[trade_date]:
            return self.daily_orb[trade_date][security_id]

        db_levels = db.get_orb_levels(trade_date.isoformat(), security_id)
        if db_levels:
            orb = ORBLevels(
                trade_date=trade_date,
                security_id=security_id,
                symbol=db_levels["symbol"],
                high=db_levels["orb_high"],
                low=db_levels["orb_low"],
                mid=db_levels["orb_mid"],
                is_complete=bool(db_levels["is_complete"]),
            )
            if trade_date not in self.daily_orb:
                self.daily_orb[trade_date] = {}
            self.daily_orb[trade_date][security_id] = orb
            return orb

        return None

    def calculate_stop_loss(
        self,
        direction: Direction,
        entry_price: float,
        orb: ORBLevels,
    ) -> float:
        """
        Calculates Stop Loss based on strategy configuration:
        - opposite_or: Long SL = ORB_LOW, Short SL = ORB_HIGH
        - orb_midpoint: SL = ORB_MID
        - fixed_percent: SL = entry * (1 +- pct)
        """
        method = self.config.risk.stop_method
        tick_size = self.config.risk.min_tick_size

        if method == "opposite_or":
            sl = orb.low if direction == Direction.LONG else orb.high
        elif method == "orb_midpoint":
            sl = orb.mid
        elif method == "fixed_percent":
            pct = self.config.risk.fixed_stop_pct / 100.0
            sl = entry_price * (1.0 - pct) if direction == Direction.LONG else entry_price * (1.0 + pct)
        else:
            sl = orb.low if direction == Direction.LONG else orb.high

        # Round to tick size
        return round(round(sl / tick_size) * tick_size, 2)

    def calculate_target(
        self,
        direction: Direction,
        entry_price: float,
        stop_loss: float,
    ) -> float:
        """
        Calculates Target using Risk:Reward multiplier:
        - Long: target = entry + risk * RR
        - Short: target = entry - risk * RR
        """
        rr = self.config.risk.risk_reward
        tick_size = self.config.risk.min_tick_size

        if direction == Direction.LONG:
            risk = entry_price - stop_loss
            target = entry_price + (risk * rr)
        else:
            risk = stop_loss - entry_price
            target = entry_price - (risk * rr)

        return round(round(target / tick_size) * tick_size, 2)

    def _passes_optional_filters(
        self,
        candle: Candle,
        direction: Direction,
        orb: ORBLevels,
        history: Optional[List[Candle]] = None,
    ) -> bool:
        """Evaluates optional filters (EMA, volume, price range, width). Disabled by default."""
        filters = self.config.filters

        # Price range filter
        if filters.price_range.enabled:
            if not (filters.price_range.min_price <= candle.close <= filters.price_range.max_price):
                return False

        # ORB width filter
        if filters.orb_width.enabled:
            width_pct = orb.range_pct
            if not (filters.orb_width.min_width_pct <= width_pct <= filters.orb_width.max_width_pct):
                return False

        # Liquidity / volume filter
        if filters.liquidity.enabled:
            if candle.volume < filters.liquidity.min_volume:
                return False

        # EMA filter
        if filters.ema.enabled and history and len(history) >= filters.ema.period:
            closes = [c.close for c in history[-filters.ema.period:]]
            # Simple or exponential moving average check
            ema = sum(closes) / len(closes)
            if direction == Direction.LONG and candle.close <= ema:
                return False
            if direction == Direction.SHORT and candle.close >= ema:
                return False

        # Relative Volume filter
        if filters.volume.enabled and history and len(history) >= filters.volume.period:
            vols = [c.volume for c in history[-filters.volume.period:]]
            avg_vol = sum(vols) / len(vols)
            if candle.volume < (avg_vol * filters.volume.multiplier):
                return False

        # Learned Stock Model Intelligence: Filter persistent false breakout traps
        try:
            learned = db.get_stock_learned_model(candle.symbol)
            if learned and learned.get("trap_rate", 0) > 35.0:
                logger.info(f"[AI Filter] Skipping {candle.symbol}: learned trap rate {learned['trap_rate']}% exceeds 35% threshold.")
                return False
        except Exception:
            pass

        return True

    def on_candle_closed(
        self,
        candle: Candle,
        history: Optional[List[Candle]] = None,
    ) -> Optional[Signal]:
        """
        Core ORB Signal Check on a finalized candle (5m or 15m).
        Must be strictly after 09:30:00 IST and within entry window.
        """
        assert candle.is_closed, "Confirmation requires a closed candle!"

        c_time = default_session.localize(candle.timestamp)
        t_date = c_time.date()
        sec_id = candle.security_id
        symbol = candle.symbol

        # Check if inside opening range (09:15 - 09:30)
        # Note: A 5m candle with start time 09:25 represents [09:25, 09:30), which closes at 09:30:00.
        # It is part of the opening range! Breakout signals can only evaluate on candles starting >= 09:30:00!
        if c_time.time() < default_session.orb_end_time:
            # Candle is within opening range
            self.register_orb_candle(candle)
            self.prev_closed_5m[(t_date, sec_id)] = candle
            return None

        # At or after 09:30:00, ensure ORB levels are finalized
        orb = self.get_orb_levels(t_date, sec_id)
        if orb is None or not orb.is_complete:
            orb = self.finalize_orb_levels(t_date, sec_id, symbol)
            if orb is None:
                logger.debug(f"Cannot evaluate ORB for {symbol}: No ORB levels established.")
                self.prev_closed_5m[(t_date, sec_id)] = candle
                return None

        # Check entry session window (09:30 - 15:25)
        if not default_session.is_entry_allowed(c_time):
            self.prev_closed_5m[(t_date, sec_id)] = candle
            return None

        # Check per-symbol trade limit for the day
        sig_count = self.daily_signal_counts.get((t_date, sec_id), 0)
        max_allowed = self.config.entry.max_signals_per_symbol_per_day
        if sig_count >= max_allowed:
            self.prev_closed_5m[(t_date, sec_id)] = candle
            return None

        # Check database for today's signal to preserve state across restarts
        if db.has_signal_today(t_date.isoformat(), sec_id):
            self.daily_signal_counts[(t_date, sec_id)] = max_allowed
            self.prev_closed_5m[(t_date, sec_id)] = candle
            return None

        # Get previous closed candle
        prev_candle = self.prev_closed_5m.get((t_date, sec_id))
        self.prev_closed_5m[(t_date, sec_id)] = candle

        # Calculate breakout levels with optional buffer
        buffer_pct = self.config.entry.breakout_buffer_pct / 100.0
        long_breakout_level = orb.high * (1.0 + buffer_pct)
        short_breakout_level = orb.low * (1.0 - buffer_pct)

        # TradingView parity conditions:
        # If prev_candle is None, default prev_close to orb.mid (inside the range)
        # so Candle 4 (10:00 - 10:15) triggers immediately on close at 10:15!
        prev_close = prev_candle.close if prev_candle is not None else orb.mid
        long_condition = (candle.close > long_breakout_level) and (prev_close <= long_breakout_level)
        short_condition = (candle.close < short_breakout_level) and (prev_close >= short_breakout_level)

        if self.debug_mode:
            logger.info(
                f"[ORB DEBUG] Time: {candle.timestamp.strftime('%H:%M')} | Sym: {symbol} | "
                f"Close: {candle.close:.2f} | PrevClose: {prev_close:.2f} | "
                f"ORB_H: {orb.high:.2f} | ORB_L: {orb.low:.2f} | "
                f"LongCond: {long_condition} | ShortCond: {short_condition}"
            )

        allowed_dir = self.config.entry.direction.lower()
        signal_dir: Optional[Direction] = None

        if long_condition and allowed_dir in ("both", "long"):
            signal_dir = Direction.LONG
        elif short_condition and allowed_dir in ("both", "short"):
            signal_dir = Direction.SHORT

        if signal_dir is None:
            return None

        # Filter check
        if not self._passes_optional_filters(candle, signal_dir, orb, history):
            logger.debug(f"{signal_dir.value} breakout for {symbol} skipped due to optional filter.")
            return None

        entry_price = candle.close
        stop_loss = self.calculate_stop_loss(signal_dir, entry_price, orb)
        target = self.calculate_target(signal_dir, entry_price, stop_loss)

        # Risk validation: entry - stop must be greater than tick size
        risk = abs(entry_price - stop_loss)
        if risk <= self.config.risk.min_tick_size:
            logger.warning(f"Calculated risk {risk:.2f} <= min tick size {self.config.risk.min_tick_size} for {symbol}. Skipping.")
            return None

        # Idempotency key: trade_date + security_id + strategy + direction
        idempotency_key = f"{t_date.isoformat()}_{sec_id}_{self.config.name}_{signal_dir.value}"

        signal = Signal(
            trade_date=t_date,
            security_id=sec_id,
            symbol=symbol,
            strategy=self.config.name,
            direction=signal_dir,
            timestamp=c_time,
            entry_price=entry_price,
            orb_high=orb.high,
            orb_low=orb.low,
            stop_loss=stop_loss,
            target=target,
            risk_reward=self.config.risk.risk_reward,
            idempotency_key=idempotency_key,
        )

        # Update per-symbol counter
        self.daily_signal_counts[(t_date, sec_id)] = sig_count + 1

        return signal

    # Backwards-compatible alias for 5m handler
    on_5m_candle_closed = on_candle_closed
