"""
Data Models and Type Definitions for ORB Scanner.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, date
from enum import Enum
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class ExitReason(str, Enum):
    TARGET = "TARGET"
    STOP_LOSS = "STOP_LOSS"
    EOD = "EOD"
    INVALIDATION = "INVALIDATION"


@dataclass
class Candle:
    security_id: str
    symbol: str
    timestamp: datetime  # localized to Asia/Kolkata
    open: float
    high: float
    low: float
    close: float
    volume: float
    is_closed: bool = True

    @property
    def iso_timestamp(self) -> str:
        return self.timestamp.isoformat()


@dataclass
class ORBLevels:
    trade_date: date
    security_id: str
    symbol: str
    high: float
    low: float
    mid: float
    is_complete: bool = False
    breached_high: bool = False
    breached_low: bool = False

    @property
    def range_size(self) -> float:
        return self.high - self.low

    @property
    def range_pct(self) -> float:
        return (self.range_size / self.low * 100.0) if self.low > 0 else 0.0


@dataclass
class Signal:
    trade_date: date
    security_id: str
    symbol: str
    strategy: str
    direction: Direction
    timestamp: datetime
    entry_price: float
    orb_high: float
    orb_low: float
    stop_loss: float
    target: float
    risk_reward: float
    idempotency_key: str
    target_1: Optional[float] = None
    target_2: Optional[float] = None
    target_3: Optional[float] = None

    @property
    def risk_amount(self) -> float:
        return abs(self.entry_price - self.stop_loss)


@dataclass
class PaperTrade:
    id: Optional[int]
    signal_id: Optional[int]
    trade_date: date
    security_id: str
    symbol: str
    direction: Direction
    entry_price: float
    entry_time: datetime
    stop_loss: float
    target: float
    exit_price: Optional[float] = None
    exit_time: Optional[datetime] = None
    exit_reason: Optional[ExitReason] = None
    pnl: Optional[float] = None
    r_multiple: Optional[float] = None
    status: str = "OPEN"
    target_1: Optional[float] = None
    target_2: Optional[float] = None
    target_3: Optional[float] = None
    target_1_hit: bool = False
    target_2_hit: bool = False
    target_3_hit: bool = False
    initial_stop_loss: Optional[float] = None
    quantity: int = 1
    asset_type: str = "EQUITY"
    strike_price: Optional[float] = None
    option_type: Optional[str] = None
    margin_reserved: float = 0.0
    entry_charges: float = 0.0
    exit_charges: float = 0.0
    total_charges: float = 0.0
    net_pnl: Optional[float] = None
