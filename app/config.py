"""
Application Configuration and Settings Loader.

Handles environment variables, strategy configuration, universe definitions,
credential masking, and centralized rotating logging.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
import zoneinfo
import yaml
from pydantic import BaseModel, Field
from dotenv import load_dotenv

# Load .env file
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
REPORTS_DIR = BASE_DIR / "reports"
CONFIG_DIR = BASE_DIR / "config"

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_DIR.mkdir(parents=True, exist_ok=True)


class SensitiveDataFilter(logging.Filter):
    """Filter that masks sensitive tokens and IDs from logging output."""

    def __init__(self, secrets: Optional[List[str]] = None):
        super().__init__()
        self.secrets = [s.strip() for s in (secrets or []) if s and len(s.strip()) > 4]

    def filter(self, record: logging.LogRecord) -> bool:
        if not isinstance(record.msg, str):
            record.msg = str(record.msg)
        for secret in self.secrets:
            if secret in record.msg:
                masked = secret[:2] + "****" + secret[-2:]
                record.msg = record.msg.replace(secret, masked)
        return True


def setup_logger(name: str = "orb_scanner", log_level: str = "INFO") -> logging.Logger:
    """Configures rotating file handler and console logging with security scrubbing."""
    logger = logging.getLogger(name)
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)
    logger.setLevel(numeric_level)

    if not logger.handlers:
        formatter = logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] [%(name)s:%(lineno)d]: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        # Console handler
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        console_handler.setLevel(numeric_level)
        logger.addHandler(console_handler)

        # Rotating file handler (10MB per file, 5 backups)
        log_file = LOGS_DIR / "orb_scanner.log"
        file_handler = RotatingFileHandler(
            log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(numeric_level)
        logger.addHandler(file_handler)

        # Mask secrets
        secrets = [
            os.getenv("DHAN_ACCESS_TOKEN", ""),
            os.getenv("TELEGRAM_BOT_TOKEN", ""),
        ]
        sensitive_filter = SensitiveDataFilter(secrets)
        logger.addFilter(sensitive_filter)

    return logger


logger = setup_logger()


class SessionConfig(BaseModel):
    timezone: str = "Asia/Kolkata"
    market_open: str = "09:15"
    orb_start: str = "09:30"
    orb_end: str = "10:00"
    entry_end: str = "15:25"
    market_close: str = "15:30"


class EntryConfig(BaseModel):
    confirmation: str = "candle_close"
    breakout_buffer_pct: float = 0.0
    max_signals_per_symbol_per_day: int = 1
    direction: str = "both"  # both, long, short


class RiskConfig(BaseModel):
    stop_method: str = "opposite_or"  # opposite_or, orb_midpoint, fixed_percent
    fixed_stop_pct: float = 0.5
    risk_reward: float = 2.0
    min_tick_size: float = 0.05


class FilterEMAConfig(BaseModel):
    enabled: bool = False
    period: int = 50


class FilterVolumeConfig(BaseModel):
    enabled: bool = False
    period: int = 20
    multiplier: float = 1.0


class FilterPriceRangeConfig(BaseModel):
    enabled: bool = False
    min_price: float = 50.0
    max_price: float = 10000.0


class FilterORBWidthConfig(BaseModel):
    enabled: bool = False
    min_width_pct: float = 0.2
    max_width_pct: float = 3.5


class FilterLiquidityConfig(BaseModel):
    enabled: bool = False
    min_volume: int = 100000


class StrategyFilters(BaseModel):
    ema: FilterEMAConfig = Field(default_factory=FilterEMAConfig)
    volume: FilterVolumeConfig = Field(default_factory=FilterVolumeConfig)
    price_range: FilterPriceRangeConfig = Field(default_factory=FilterPriceRangeConfig)
    orb_width: FilterORBWidthConfig = Field(default_factory=FilterORBWidthConfig)
    liquidity: FilterLiquidityConfig = Field(default_factory=FilterLiquidityConfig)


class BacktestCosts(BaseModel):
    brokerage_pct: float = 0.03
    max_brokerage_per_order: float = 20.0
    stt_pct: float = 0.025
    exchange_charges_pct: float = 0.00297
    gst_pct: float = 18.0
    sebi_turnover_pct: float = 0.0001
    stamp_duty_pct: float = 0.003
    slippage_pct: float = 0.02


class StrategyConfig(BaseModel):
    name: str = "ORB-15"
    session: SessionConfig = Field(default_factory=SessionConfig)
    signal_timeframe: int = 5
    entry: EntryConfig = Field(default_factory=EntryConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    filters: StrategyFilters = Field(default_factory=StrategyFilters)
    costs: BacktestCosts = Field(default_factory=BacktestCosts)


class UniverseConfig(BaseModel):
    mode: str = "nifty50"
    exchange: str = "NSE"
    exchange_segment: str = "NSE_EQ"
    instrument_type: str = "EQUITY"
    custom_symbols: List[str] = Field(default_factory=list)
    nifty50_symbols: List[str] = Field(default_factory=list)
    master_file_url: str = "https://images.dhan.co/api-data/api-scrip-master.csv"
    local_path: str = "data/scrip_master.csv"
    refresh_days: int = 1


class AppSettings:
    """Central settings containing environment variables, strategy configs, and universe."""

    def __init__(self):
        self.dhan_client_id: str = os.getenv("DHAN_CLIENT_ID", "").strip()
        self.dhan_access_token: str = os.getenv("DHAN_ACCESS_TOKEN", "").strip()
        self.telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        self.timezone_name: str = os.getenv("TIMEZONE", "Asia/Kolkata").strip()
        self.database_path: str = os.getenv(
            "DATABASE_PATH", str(DATA_DIR / "orb_scanner.db")
        ).strip()
        self.log_level: str = os.getenv("LOG_LEVEL", "INFO").strip()
        self.firebase_project_id: str = os.getenv("FIREBASE_PROJECT_ID", "orbscanner-cb055").strip()
        self.firebase_api_key: str = os.getenv("FIREBASE_API_KEY", "").strip()
        self.firebase_storage_bucket: str = os.getenv("FIREBASE_STORAGE_BUCKET", "orbscanner-cb055.firebasestorage.app").strip()

        # Validate timezone
        try:
            self.tz = zoneinfo.ZoneInfo(self.timezone_name)
        except Exception:
            self.tz = zoneinfo.ZoneInfo("Asia/Kolkata")
            self.timezone_name = "Asia/Kolkata"

        # Load YAML files
        self.strategy = self._load_strategy_config()
        self.universe = self._load_universe_config()

    def _load_strategy_config(self) -> StrategyConfig:
        strategy_file = CONFIG_DIR / "strategy.yaml"
        if not strategy_file.exists():
            return StrategyConfig()
        try:
            with open(strategy_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            
            strat_info = data.get("strategy", {})
            strat_name = strat_info.get("name", "ORB-15") if isinstance(strat_info, dict) else "ORB-15"
            
            candles_info = data.get("candles", {})
            tf = candles_info.get("signal_timeframe", 5) if isinstance(candles_info, dict) else 5

            return StrategyConfig(
                name=strat_name,
                session=SessionConfig(**data.get("session", {})),
                signal_timeframe=tf,
                entry=EntryConfig(**data.get("entry", {})),
                risk=RiskConfig(**data.get("risk", {})),
                filters=StrategyFilters(**data.get("filters", {})),
                costs=BacktestCosts(**data.get("backtest", {}).get("costs", {})),
            )
        except Exception as e:
            logger.warning(f"Failed to parse strategy.yaml, using defaults: {e}")
            return StrategyConfig()

    def _load_universe_config(self) -> UniverseConfig:
        universe_file = CONFIG_DIR / "universe.yaml"
        if not universe_file.exists():
            return UniverseConfig()
        try:
            with open(universe_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            cache_info = data.get("cache", {})
            return UniverseConfig(
                mode=data.get("mode", "nifty50"),
                exchange=data.get("exchange", "NSE"),
                exchange_segment=data.get("exchange_segment", "NSE_EQ"),
                instrument_type=data.get("instrument_type", "EQUITY"),
                custom_symbols=data.get("custom_symbols", []),
                nifty50_symbols=data.get("nifty50_symbols", []),
                master_file_url=cache_info.get(
                    "master_file_url", "https://images.dhan.co/api-data/api-scrip-master.csv"
                ),
                local_path=cache_info.get("local_path", "data/scrip_master.csv"),
                refresh_days=cache_info.get("refresh_days", 1),
            )
        except Exception as e:
            logger.warning(f"Failed to parse universe.yaml, using defaults: {e}")
            return UniverseConfig()

    @property
    def has_dhan_credentials(self) -> bool:
        return bool(self.dhan_client_id and self.dhan_access_token)

    @property
    def has_telegram_credentials(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    def __repr__(self) -> str:
        masked_dhan = (
            f"{self.dhan_client_id[:2]}****" if self.dhan_client_id else "<NOT_SET>"
        )
        masked_tg = (
            f"{self.telegram_bot_token[:3]}****"
            if self.telegram_bot_token
            else "<NOT_SET>"
        )
        return (
            f"AppSettings(dhan_client_id={masked_dhan}, "
            f"telegram_bot_token={masked_tg}, timezone={self.timezone_name})"
        )


settings = AppSettings()
