"""
SQLite Storage Engine for persistent market data, ORB levels, signals, and virtual trades.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple

from app.config import logger, settings


class Database:
    """Thread-safe SQLite database manager for ORB Scanner."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or settings.database_path
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    @contextmanager
    def get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Provides a database connection with WAL mode and foreign keys enabled."""
        conn = sqlite3.connect(
            self.db_path,
            timeout=30.0,
            detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_db(self) -> None:
        """Initializes database schema and indexes."""
        with self.get_connection() as conn:
            cursor = conn.cursor()

            # Instruments table
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS instruments (
                security_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                display_name TEXT,
                exchange_segment TEXT NOT NULL,
                instrument_type TEXT NOT NULL,
                lot_size INTEGER DEFAULT 1,
                tick_size REAL DEFAULT 0.05,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)

            # Key-Value Store for persistent settings and compounding account balance
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS settings_kv (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)

            # Learned stock predictive models with recency weighting and trap tracking
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS stock_learned_models (
                symbol TEXT PRIMARY KEY,
                security_id TEXT,
                win_rate REAL NOT NULL,
                high_vol_win_rate REAL NOT NULL,
                trap_rate REAL NOT NULL,
                sessions_analyzed INTEGER NOT NULL,
                optimal_vol_ratio REAL DEFAULT 1.3,
                last_trained_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)

            # 1-minute Candles
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS candles_1m (
                security_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                timestamp TEXT NOT NULL, -- ISO-8601
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL NOT NULL,
                is_closed INTEGER DEFAULT 1,
                PRIMARY KEY (security_id, timestamp)
            );
            """)

            # 5-minute Candles
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS candles_5m (
                security_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                timestamp TEXT NOT NULL, -- ISO-8601
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL NOT NULL,
                is_closed INTEGER DEFAULT 1,
                PRIMARY KEY (security_id, timestamp)
            );
            """)

            # 15-minute Candles
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS candles_15m (
                security_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                timestamp TEXT NOT NULL, -- ISO-8601
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL NOT NULL,
                is_closed INTEGER DEFAULT 1,
                PRIMARY KEY (security_id, timestamp)
            );
            """)

            # ORB Daily Levels
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS orb_daily_levels (
                trade_date TEXT NOT NULL, -- YYYY-MM-DD
                security_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                orb_high REAL NOT NULL,
                orb_low REAL NOT NULL,
                orb_mid REAL NOT NULL,
                is_complete INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (trade_date, security_id)
            );
            """)

            # Signals
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date TEXT NOT NULL,
                security_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                strategy TEXT NOT NULL,
                direction TEXT NOT NULL, -- 'LONG' or 'SHORT'
                timestamp TEXT NOT NULL,
                entry_price REAL NOT NULL,
                orb_high REAL NOT NULL,
                orb_low REAL NOT NULL,
                stop_loss REAL NOT NULL,
                target REAL NOT NULL,
                risk_reward REAL NOT NULL,
                idempotency_key TEXT UNIQUE NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)

            # Paper Trades (Virtual Orders)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id INTEGER,
                trade_date TEXT NOT NULL,
                security_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                entry_price REAL NOT NULL,
                entry_time TEXT NOT NULL,
                stop_loss REAL NOT NULL,
                target REAL NOT NULL,
                exit_price REAL,
                exit_time TEXT,
                exit_reason TEXT, -- 'TARGET', 'STOP_LOSS', 'EOD', 'INVALIDATION'
                pnl REAL,
                r_multiple REAL,
                status TEXT NOT NULL DEFAULT 'OPEN', -- 'OPEN', 'CLOSED'
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (signal_id) REFERENCES signals (id)
            );
            """)

            # Alerts
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                idempotency_key TEXT UNIQUE NOT NULL,
                channel TEXT NOT NULL DEFAULT 'TELEGRAM',
                message TEXT NOT NULL,
                sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                success INTEGER NOT NULL,
                error_message TEXT,
                message_id INTEGER,
                chat_id TEXT,
                is_deleted INTEGER DEFAULT 0
            );
            """)

            # Migrate columns if existing database table lacks them
            for col, col_type in [("message_id", "INTEGER"), ("chat_id", "TEXT"), ("is_deleted", "INTEGER DEFAULT 0")]:
                try:
                    cursor.execute(f"ALTER TABLE alerts ADD COLUMN {col} {col_type};")
                except sqlite3.OperationalError:
                    pass

            # Backtest Runs
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS backtest_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                strategy_name TEXT NOT NULL,
                from_date TEXT NOT NULL,
                to_date TEXT NOT NULL,
                universe TEXT NOT NULL,
                total_trades INTEGER NOT NULL,
                win_rate REAL NOT NULL,
                net_pnl REAL NOT NULL,
                metrics_json TEXT NOT NULL
            );
            """)

            # System Events
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS system_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                message TEXT NOT NULL,
                details_json TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)

            # Indexes for ultra-fast lookup
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_candles1m_sec_ts ON candles_1m(security_id, timestamp);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_candles5m_sec_ts ON candles_5m(security_id, timestamp);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_candles15m_sec_ts ON candles_15m(security_id, timestamp);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_orb_date_sec ON orb_daily_levels(trade_date, security_id);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_signals_date_sec ON signals(trade_date, security_id);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_signals_idemp ON signals(idempotency_key);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_paper_status ON paper_trades(status, security_id);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_paper_date ON paper_trades(trade_date);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_alerts_idemp ON alerts(idempotency_key);")

    # Helper methods
    def save_instrument(self, security_id: str, symbol: str, display_name: str,
                        exchange_segment: str, instrument_type: str, lot_size: int = 1, tick_size: float = 0.05) -> None:
        self.save_instruments_bulk([(security_id, symbol, display_name, exchange_segment, instrument_type, lot_size, tick_size)])

    def save_instruments_bulk(self, items: List[Tuple[str, str, str, str, str, int, float]]) -> None:
        """Saves a batch of instruments in a single fast transaction."""
        if not items:
            return
        with self.get_connection() as conn:
            conn.executemany("""
                INSERT INTO instruments (security_id, symbol, display_name, exchange_segment, instrument_type, lot_size, tick_size, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(security_id) DO UPDATE SET
                    symbol=excluded.symbol,
                    display_name=excluded.display_name,
                    exchange_segment=excluded.exchange_segment,
                    instrument_type=excluded.instrument_type,
                    lot_size=excluded.lot_size,
                    tick_size=excluded.tick_size,
                    updated_at=CURRENT_TIMESTAMP;
            """, items)

    def save_candle_1m(self, security_id: str, symbol: str, timestamp: str,
                       open_: float, high: float, low: float, close: float, volume: float, is_closed: bool = True) -> None:
        with self.get_connection() as conn:
            conn.execute("""
                INSERT INTO candles_1m (security_id, symbol, timestamp, open, high, low, close, volume, is_closed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(security_id, timestamp) DO UPDATE SET
                    open=excluded.open,
                    high=excluded.high,
                    low=excluded.low,
                    close=excluded.close,
                    volume=excluded.volume,
                    is_closed=excluded.is_closed;
            """, (security_id, symbol, timestamp, open_, high, low, close, volume, 1 if is_closed else 0))

    def save_candle_5m(self, security_id: str, symbol: str, timestamp: str,
                       open_: float, high: float, low: float, close: float, volume: float, is_closed: bool = True) -> None:
        with self.get_connection() as conn:
            conn.execute("""
                INSERT INTO candles_5m (security_id, symbol, timestamp, open, high, low, close, volume, is_closed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(security_id, timestamp) DO UPDATE SET
                    open=excluded.open,
                    high=excluded.high,
                    low=excluded.low,
                    close=excluded.close,
                    volume=excluded.volume,
                    is_closed=excluded.is_closed;
            """, (security_id, symbol, timestamp, open_, high, low, close, volume, 1 if is_closed else 0))

    def save_candle_15m(self, security_id: str, symbol: str, timestamp: str,
                        open_: float, high: float, low: float, close: float, volume: float, is_closed: bool = True) -> None:
        with self.get_connection() as conn:
            conn.execute("""
                INSERT INTO candles_15m (security_id, symbol, timestamp, open, high, low, close, volume, is_closed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(security_id, timestamp) DO UPDATE SET
                    open=excluded.open,
                    high=excluded.high,
                    low=excluded.low,
                    close=excluded.close,
                    volume=excluded.volume,
                    is_closed=excluded.is_closed;
            """, (security_id, symbol, timestamp, open_, high, low, close, volume, 1 if is_closed else 0))

    def get_recent_candles_15m(self, security_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM candles_15m WHERE security_id = ? ORDER BY timestamp DESC LIMIT ?",
                (security_id, limit)
            ).fetchall()
            return [dict(r) for r in reversed(rows)]

    def save_orb_levels(self, trade_date: str, security_id: str, symbol: str,
                        orb_high: float, orb_low: float, orb_mid: float, is_complete: bool = True) -> None:
        with self.get_connection() as conn:
            conn.execute("""
                INSERT INTO orb_daily_levels (trade_date, security_id, symbol, orb_high, orb_low, orb_mid, is_complete, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(trade_date, security_id) DO UPDATE SET
                    orb_high=excluded.orb_high,
                    orb_low=excluded.orb_low,
                    orb_mid=excluded.orb_mid,
                    is_complete=excluded.is_complete;
            """, (trade_date, security_id, symbol, orb_high, orb_low, orb_mid, 1 if is_complete else 0))

    def get_orb_levels(self, trade_date: str, security_id: str) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM orb_daily_levels WHERE trade_date = ? AND security_id = ?",
                (trade_date, security_id)
            ).fetchone()
            return dict(row) if row else None

    def has_signal_today(self, trade_date: str, security_id: str) -> bool:
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as count FROM signals WHERE trade_date = ? AND security_id = ?",
                (trade_date, security_id)
            ).fetchone()
            return row["count"] > 0 if row else False

    def save_signal(self, trade_date: str, security_id: str, symbol: str,
                    strategy: str, direction: str, timestamp: str,
                    entry_price: float, orb_high: float, orb_low: float,
                    stop_loss: float, target: float, risk_reward: float,
                    idempotency_key: str) -> Optional[int]:
        with self.get_connection() as conn:
            try:
                cursor = conn.execute("""
                    INSERT INTO signals (trade_date, security_id, symbol, strategy, direction,
                                         timestamp, entry_price, orb_high, orb_low, stop_loss, target,
                                         risk_reward, idempotency_key)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (trade_date, security_id, symbol, strategy, direction,
                      timestamp, entry_price, orb_high, orb_low, stop_loss, target,
                      risk_reward, idempotency_key))
                return cursor.lastrowid
            except sqlite3.IntegrityError:
                logger.info(f"Signal already exists for key {idempotency_key} (idempotent skipped)")
                return None

    def save_paper_trade(self, signal_id: Optional[int], trade_date: str, security_id: str,
                         symbol: str, direction: str, entry_price: float,
                         entry_time: str, stop_loss: float, target: float) -> int:
        with self.get_connection() as conn:
            cursor = conn.execute("""
                INSERT INTO paper_trades (signal_id, trade_date, security_id, symbol, direction,
                                          entry_price, entry_time, stop_loss, target, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN')
            """, (signal_id, trade_date, security_id, symbol, direction, entry_price, entry_time, stop_loss, target))
            return cursor.lastrowid

    def close_paper_trade(self, trade_id: int, exit_price: float, exit_time: str,
                          exit_reason: str, pnl: float, r_multiple: float) -> None:
        with self.get_connection() as conn:
            conn.execute("""
                UPDATE paper_trades
                SET exit_price = ?,
                    exit_time = ?,
                    exit_reason = ?,
                    pnl = ?,
                    r_multiple = ?,
                    status = 'CLOSED',
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (exit_price, exit_time, exit_reason, pnl, r_multiple, trade_id))

    def get_open_paper_trades(self, trade_date: Optional[str] = None) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            if trade_date:
                rows = conn.execute(
                    "SELECT * FROM paper_trades WHERE status = 'OPEN' AND trade_date = ?", (trade_date,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM paper_trades WHERE status = 'OPEN'").fetchall()
            return [dict(r) for r in rows]

    def record_alert(self, idempotency_key: str, message: str, success: bool,
                     error_message: Optional[str] = None, channel: str = "TELEGRAM",
                     message_id: Optional[int] = None, chat_id: Optional[str] = None) -> bool:
        with self.get_connection() as conn:
            try:
                conn.execute("""
                    INSERT INTO alerts (idempotency_key, channel, message, success, error_message, message_id, chat_id, is_deleted)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                """, (idempotency_key, channel, message, 1 if success else 0, error_message, message_id, chat_id))
                return True
            except sqlite3.IntegrityError:
                return False

    def is_alert_sent(self, idempotency_key: str) -> bool:
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM alerts WHERE idempotency_key = ? AND success = 1",
                (idempotency_key,)
            ).fetchone()
            return row["cnt"] > 0 if row else False

    def get_uncleaned_alerts(self, older_than_hours: int = 24) -> List[Dict[str, Any]]:
        """Returns successful Telegram alerts sent more than specified hours ago that are not yet deleted."""
        with self.get_connection() as conn:
            cutoff = f"-{older_than_hours} hours"
            rows = conn.execute("""
                SELECT id, message_id, chat_id, sent_at
                FROM alerts
                WHERE success = 1
                  AND is_deleted = 0
                  AND message_id IS NOT NULL
                  AND chat_id IS NOT NULL
                  AND sent_at <= datetime('now', ?)
            """, (cutoff,)).fetchall()
            return [dict(r) for r in rows]

    def mark_alert_deleted(self, alert_id: int) -> None:
        with self.get_connection() as conn:
            conn.execute("UPDATE alerts SET is_deleted = 1 WHERE id = ?", (alert_id,))

    def log_event(self, event_type: str, message: str, details: Optional[Dict[str, Any]] = None) -> None:
        with self.get_connection() as conn:
            conn.execute("""
                INSERT INTO system_events (event_type, message, details_json)
                VALUES (?, ?, ?)
            """, (event_type, message, json.dumps(details) if details else None))

    def get_account_balance(self, default_capital: float = 4322.0) -> float:
        """Retrieves persistent trading capital, defaulting to ₹4,322 if not set."""
        with self.get_connection() as conn:
            row = conn.execute("SELECT value FROM settings_kv WHERE key = 'account_balance'").fetchone()
            if row:
                try:
                    return float(row["value"])
                except (ValueError, TypeError):
                    pass
            return default_capital

    def update_account_balance(self, pnl: float, default_capital: float = 4322.0) -> float:
        """Updates and compounds trading capital with realized trade PnL."""
        current = self.get_account_balance(default_capital)
        new_balance = round(current + pnl, 2)
        with self.get_connection() as conn:
            conn.execute("""
                INSERT INTO settings_kv (key, value, updated_at)
                VALUES ('account_balance', ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
            """, (str(new_balance),))
        logger.info(f"Compounded Trading Capital: ₹{current:.2f} -> ₹{new_balance:.2f} (PnL: {pnl:+.2f})")
        return new_balance

    def save_learned_state(self, state: Dict[str, Any]) -> None:
        """Saves learned ML/statistical model state to key-value store."""
        with self.get_connection() as conn:
            conn.execute("""
                INSERT INTO settings_kv (key, value, updated_at)
                VALUES ('learned_model_state', ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
            """, (json.dumps(state, default=str),))

    def get_learned_state(self) -> Optional[Dict[str, Any]]:
        """Retrieves learned ML/statistical model state from key-value store."""
        with self.get_connection() as conn:
            row = conn.execute("SELECT value FROM settings_kv WHERE key = 'learned_model_state'").fetchone()
            if row:
                try:
                    return json.loads(row["value"])
                except Exception:
                    pass
    def save_stock_learned_model(
        self,
        symbol: str,
        security_id: str,
        win_rate: float,
        high_vol_win_rate: float,
        trap_rate: float,
        sessions_analyzed: int,
        optimal_vol_ratio: float = 1.3,
    ) -> None:
        """
        Saves or updates stock predictive model with recency decay:
        blends 30% new observation with 70% historical memory ('navu shikhtu re, junu bhultu re').
        """
        with self.get_connection() as conn:
            conn.execute("""
                INSERT INTO stock_learned_models (
                    symbol, security_id, win_rate, high_vol_win_rate, trap_rate,
                    sessions_analyzed, optimal_vol_ratio, last_trained_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(symbol) DO UPDATE SET
                    security_id = excluded.security_id,
                    win_rate = round(stock_learned_models.win_rate * 0.70 + excluded.win_rate * 0.30, 1),
                    high_vol_win_rate = round(stock_learned_models.high_vol_win_rate * 0.70 + excluded.high_vol_win_rate * 0.30, 1),
                    trap_rate = round(stock_learned_models.trap_rate * 0.70 + excluded.trap_rate * 0.30, 1),
                    sessions_analyzed = excluded.sessions_analyzed,
                    optimal_vol_ratio = excluded.optimal_vol_ratio,
                    last_trained_at = CURRENT_TIMESTAMP
            """, (symbol, str(security_id), win_rate, high_vol_win_rate, trap_rate, sessions_analyzed, optimal_vol_ratio))

    def get_stock_learned_model(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Retrieves learned predictive parameters for a specific stock."""
        with self.get_connection() as conn:
            row = conn.execute("SELECT * FROM stock_learned_models WHERE symbol = ?", (symbol,)).fetchone()
            return dict(row) if row else None

    def get_all_stock_learned_models(self) -> List[Dict[str, Any]]:
        """Returns all stock learned models ranked by predictive win rate."""
        with self.get_connection() as conn:
            rows = conn.execute("SELECT * FROM stock_learned_models ORDER BY high_vol_win_rate DESC").fetchall()
            return [dict(r) for r in rows]


db = Database()

