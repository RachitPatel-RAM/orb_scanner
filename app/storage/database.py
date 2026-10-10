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

            # 60-minute Candles (Hourly trend evaluation, session-open anchored)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS candles_60m (
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

            # Trend Sweep FVG V1 Setups & Lifecycle Tracking
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS trend_sweep_setups (
                signal_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                security_id TEXT NOT NULL,
                strategy_version TEXT NOT NULL DEFAULT 'TREND_SWEEP_FVG_V1',
                direction TEXT NOT NULL,
                status TEXT NOT NULL,
                detected_at TEXT,
                known_at TEXT,
                trade_date TEXT NOT NULL,
                impulse_high REAL,
                impulse_low REAL,
                fib_retracement_min REAL,
                fib_retracement_max REAL,
                poi_id TEXT,
                poi_type TEXT,
                poi_high REAL,
                poi_low REAL,
                swept_level REAL,
                sweep_extreme REAL,
                reclaim_time TEXT,
                pre_breach_structure_ref REAL,
                displacement_bar_time TEXT,
                fvg_id TEXT,
                fvg_top REAL,
                fvg_bottom REAL,
                planned_entry REAL,
                initial_stop REAL,
                target_price REAL,
                nominal_rr REAL,
                net_rr REAL,
                heuristic_score INTEGER,
                score_breakdown_json TEXT,
                rejection_reason TEXT,
                exit_version TEXT,
                filled_price REAL,
                filled_time TEXT,
                exit_price REAL,
                exit_time TEXT,
                exit_reason TEXT,
                realized_pnl REAL,
                realized_r REAL,
                quantity INTEGER,
                raw_setup_json TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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

            # Auditable Paper Account Ledger
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS paper_account_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                trade_id INTEGER,
                transaction_type TEXT NOT NULL, -- 'CASH_RESERVATION', 'CASH_RELEASE', 'ENTRY_CHARGES', 'EXIT_CHARGES', 'REALIZED_PNL'
                amount REAL NOT NULL,
                balance_before REAL NOT NULL,
                balance_after REAL NOT NULL,
                description TEXT NOT NULL,
                FOREIGN KEY (trade_id) REFERENCES paper_trades (id)
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

            # Migrate paper_trades columns for option paper trading
            paper_cols = [
                ("quantity", "INTEGER DEFAULT 1"),
                ("asset_type", "TEXT DEFAULT 'EQUITY'"),
                ("strike_price", "REAL"),
                ("option_type", "TEXT"),
                ("margin_reserved", "REAL DEFAULT 0.0"),
                ("entry_charges", "REAL DEFAULT 0.0"),
                ("exit_charges", "REAL DEFAULT 0.0"),
                ("total_charges", "REAL DEFAULT 0.0"),
                ("net_pnl", "REAL DEFAULT 0.0"),
            ]
            for col, col_type in paper_cols:
                try:
                    cursor.execute(f"ALTER TABLE paper_trades ADD COLUMN {col} {col_type};")
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

            # VIP Channel Subscribers
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS vip_subscribers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                plan_months INTEGER NOT NULL,
                start_date TEXT NOT NULL,
                expiry_date TEXT NOT NULL,
                is_active INTEGER DEFAULT 1,
                invite_link TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)

            # Pending Orders Table (Persistent 1-click execution across server restarts)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS pending_orders (
                sig_key TEXT PRIMARY KEY,
                security_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                entry_price REAL NOT NULL,
                stop_loss REAL NOT NULL,
                target REAL NOT NULL,
                lot_size INTEGER NOT NULL,
                margin_req REAL NOT NULL,
                total_lot_price REAL NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)

            # Daily Bias Snapshots Table (Section 6 immutable snapshots)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS daily_bias_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                security_id TEXT NOT NULL,
                exchange TEXT NOT NULL DEFAULT 'NSE',
                trading_date TEXT NOT NULL,
                previous_session_date TEXT NOT NULL,
                reference_session_date TEXT NOT NULL,
                previous_close REAL NOT NULL,
                reference_high REAL NOT NULL,
                reference_low REAL NOT NULL,
                daily_bias TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                source_data_timestamp TEXT,
                calculated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                rule_version TEXT NOT NULL,
                data_health TEXT NOT NULL,
                raw_details_json TEXT,
                is_corrected INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(symbol, trading_date, rule_version)
            );
            """)

            # Intraday Liquidity Context Events Table (Section 7 swing/sweep state)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS liquidity_context_events (
                event_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                security_id TEXT NOT NULL,
                timeframe_minutes INTEGER DEFAULT 60,
                pivot_type TEXT NOT NULL,
                level_price REAL NOT NULL,
                breach_bar_time TEXT NOT NULL,
                confirmation_time TEXT NOT NULL,
                reclaim_bars INTEGER NOT NULL,
                sweep_direction TEXT NOT NULL,
                sweep_extreme REAL NOT NULL,
                status TEXT NOT NULL,
                expiry_time TEXT NOT NULL,
                invalidation_time TEXT,
                rule_version TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)

            # Bias Gate Decisions Table (Section 9 auditable gate decisions)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS bias_gate_decisions (
                decision_id TEXT PRIMARY KEY,
                candidate_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                security_id TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                candidate_direction TEXT NOT NULL,
                gate_mode TEXT NOT NULL,
                decision TEXT NOT NULL,
                would_allow INTEGER NOT NULL,
                is_allowed INTEGER NOT NULL,
                daily_bias_snapshot_id TEXT,
                liquidity_context_event_id TEXT,
                reason_code TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                rule_version TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)

            # Audit Scalp Sessions (Historical Benchmark & Daily Journal)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS audit_scalp_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_num INTEGER,
                trade_date TEXT UNIQUE,
                nifty_open REAL,
                nifty_high REAL,
                nifty_low REAL,
                nifty_close REAL,
                gap_pts REAL,
                setup_type TEXT,
                trade_direction TEXT,
                outcome TEXT,
                gross_pnl REAL,
                brokerage_taxes REAL,
                net_pnl REAL,
                running_capital REAL,
                recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)

            # Auto-seed from scalp_audit_monthly.json if table is empty
            try:
                cursor.execute("SELECT COUNT(*) FROM audit_scalp_sessions")
                if cursor.fetchone()[0] == 0:
                    json_path = Path(self.db_path).parent / "scalp_audit_monthly.json"
                    if json_path.exists():
                        import json
                        with open(json_path, "r", encoding="utf-8") as f:
                            s_data = json.load(f)
                        for t in s_data.get("sessions", []):
                            cursor.execute("""
                                INSERT OR IGNORE INTO audit_scalp_sessions (
                                    session_num, trade_date, nifty_open, nifty_high, nifty_low, nifty_close,
                                    gap_pts, setup_type, trade_direction, outcome, gross_pnl, brokerage_taxes,
                                    net_pnl, running_capital
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """, (
                                t["session_num"], t["date"], t["nifty_open"], t["nifty_high"], t["nifty_low"],
                                t["nifty_close"], t["gap_pts"], t["setup_type"], t["trade_direction"],
                                t["outcome"], t["gross_pnl"], t["brokerage_taxes"], t["net_pnl"], t["running_capital"]
                            ))
            except Exception as e:
                logger.debug(f"Error seeding audit_scalp_sessions: {e}")

            # Indexes for ultra-fast lookup
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_candles1m_sec_ts ON candles_1m(security_id, timestamp);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_candles5m_sec_ts ON candles_5m(security_id, timestamp);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_candles15m_sec_ts ON candles_15m(security_id, timestamp);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_candles60m_sec_ts ON candles_60m(security_id, timestamp);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_trend_sweep_status ON trend_sweep_setups(status, symbol);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_trend_sweep_date ON trend_sweep_setups(trade_date, symbol);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_orb_date_sec ON orb_daily_levels(trade_date, security_id);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_signals_date_sec ON signals(trade_date, security_id);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_signals_idemp ON signals(idempotency_key);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_paper_status ON paper_trades(status, security_id);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_paper_date ON paper_trades(trade_date);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_alerts_idemp ON alerts(idempotency_key);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_daily_bias_sym_date ON daily_bias_snapshots(symbol, trading_date);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_liq_ctx_sym_status ON liquidity_context_events(symbol, status);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_gate_dec_date_sym ON bias_gate_decisions(trade_date, symbol);")

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

    def save_candle_60m(self, security_id: str, symbol: str, timestamp: str,
                        open_: float, high: float, low: float, close: float, volume: float, is_closed: bool = True) -> None:
        with self.get_connection() as conn:
            conn.execute("""
                INSERT INTO candles_60m (security_id, symbol, timestamp, open, high, low, close, volume, is_closed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(security_id, timestamp) DO UPDATE SET
                    open=excluded.open,
                    high=excluded.high,
                    low=excluded.low,
                    close=excluded.close,
                    volume=excluded.volume,
                    is_closed=excluded.is_closed;
            """, (security_id, symbol, timestamp, open_, high, low, close, volume, 1 if is_closed else 0))

    def get_recent_candles_60m(self, security_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM candles_60m WHERE security_id = ? ORDER BY timestamp DESC LIMIT ?",
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

    def save_paper_trade(
        self,
        signal_id: Optional[int],
        trade_date: str,
        security_id: str,
        symbol: str,
        direction: str,
        entry_price: float,
        entry_time: str,
        stop_loss: float,
        target: float,
        quantity: int = 1,
        asset_type: str = "EQUITY",
        strike_price: Optional[float] = None,
        option_type: Optional[str] = None,
        margin_reserved: float = 0.0,
        entry_charges: float = 0.0,
    ) -> int:
        with self.get_connection() as conn:
            cursor = conn.execute("""
                INSERT INTO paper_trades (
                    signal_id, trade_date, security_id, symbol, direction,
                    entry_price, entry_time, stop_loss, target, status,
                    quantity, asset_type, strike_price, option_type,
                    margin_reserved, entry_charges
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, ?)
            """, (
                signal_id, trade_date, security_id, symbol, direction,
                entry_price, entry_time, stop_loss, target,
                quantity, asset_type, strike_price, option_type,
                margin_reserved, entry_charges,
            ))
            return cursor.lastrowid

    def close_paper_trade(
        self,
        trade_id: int,
        exit_price: float,
        exit_time: str,
        exit_reason: str,
        pnl: float,
        r_multiple: float,
        exit_charges: float = 0.0,
        net_pnl: Optional[float] = None,
    ) -> None:
        with self.get_connection() as conn:
            conn.execute("""
                UPDATE paper_trades
                SET exit_price = ?,
                    exit_time = ?,
                    exit_reason = ?,
                    pnl = ?,
                    r_multiple = ?,
                    exit_charges = ?,
                    total_charges = COALESCE(entry_charges, 0.0) + ?,
                    net_pnl = ?,
                    status = 'CLOSED',
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (
                exit_price, exit_time, exit_reason, pnl, r_multiple,
                exit_charges, exit_charges, net_pnl if net_pnl is not None else pnl,
                trade_id,
            ))

    def record_ledger_entry(
        self,
        transaction_type: str,
        amount: float,
        balance_before: float,
        balance_after: float,
        description: str,
        trade_id: Optional[int] = None,
        timestamp: Optional[str] = None,
    ) -> int:
        ts = timestamp or datetime.now().isoformat()
        with self.get_connection() as conn:
            cursor = conn.execute("""
                INSERT INTO paper_account_ledger (timestamp, trade_id, transaction_type, amount, balance_before, balance_after, description)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (ts, trade_id, transaction_type, round(amount, 2), round(balance_before, 2), round(balance_after, 2), description))
            return cursor.lastrowid

    def get_paper_ledger(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM paper_account_ledger ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

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

    def set_account_balance(self, balance: float) -> None:
        """Sets the exact persistent account balance in settings_kv."""
        with self.get_connection() as conn:
            conn.execute("""
                INSERT INTO settings_kv (key, value, updated_at)
                VALUES ('account_balance', ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
            """, (str(round(balance, 2)),))

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

    def record_scalp_session(
        self,
        trade_date: str,
        nifty_open: float,
        nifty_high: float,
        nifty_low: float,
        nifty_close: float,
        gap_pts: float,
        setup_type: str,
        trade_direction: str,
        outcome: str,
        gross_pnl: float,
        brokerage_taxes: float,
        net_pnl: float,
        running_capital: float,
    ) -> None:
        """Records an auditable scalp session into audit_scalp_sessions."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT MAX(session_num) FROM audit_scalp_sessions")
            row = cur.fetchone()
            next_num = (row[0] or 0) + 1 if row else 1

            conn.execute("""
                INSERT INTO audit_scalp_sessions (
                    session_num, trade_date, nifty_open, nifty_high, nifty_low, nifty_close,
                    gap_pts, setup_type, trade_direction, outcome, gross_pnl, brokerage_taxes,
                    net_pnl, running_capital
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_date) DO UPDATE SET
                    nifty_open=excluded.nifty_open,
                    nifty_high=excluded.nifty_high,
                    nifty_low=excluded.nifty_low,
                    nifty_close=excluded.nifty_close,
                    gap_pts=excluded.gap_pts,
                    setup_type=excluded.setup_type,
                    trade_direction=excluded.trade_direction,
                    outcome=excluded.outcome,
                    gross_pnl=excluded.gross_pnl,
                    brokerage_taxes=excluded.brokerage_taxes,
                    net_pnl=excluded.net_pnl,
                    running_capital=excluded.running_capital,
                    recorded_at=CURRENT_TIMESTAMP
            """, (
                next_num, trade_date, nifty_open, nifty_high, nifty_low, nifty_close,
                gap_pts, setup_type, trade_direction, outcome, gross_pnl, brokerage_taxes,
                net_pnl, running_capital
            ))
        logger.info(f"Recorded scalp session #{next_num} for {trade_date}: {outcome} (Net: ₹{net_pnl:+.2f})")

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

    def add_vip_subscriber(self, telegram_id: str, name: str, plan_months: int,
                           start_date: str, expiry_date: str, invite_link: Optional[str] = None) -> int:
        with self.get_connection() as conn:
            cur = conn.execute("""
                INSERT INTO vip_subscribers (telegram_id, name, plan_months, start_date, expiry_date, is_active, invite_link)
                VALUES (?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    name = excluded.name,
                    plan_months = excluded.plan_months,
                    start_date = excluded.start_date,
                    expiry_date = excluded.expiry_date,
                    is_active = 1,
                    invite_link = excluded.invite_link;
            """, (telegram_id, name, plan_months, start_date, expiry_date, invite_link))
            return cur.lastrowid

    def get_active_vip_subscribers(self) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            rows = conn.execute("SELECT * FROM vip_subscribers WHERE is_active = 1 ORDER BY expiry_date ASC").fetchall()
            return [dict(r) for r in rows]

    def get_expired_vip_subscribers(self, current_date_str: str) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            rows = conn.execute("SELECT * FROM vip_subscribers WHERE is_active = 1 AND expiry_date < ?", (current_date_str,)).fetchall()
            return [dict(r) for r in rows]

    def deactivate_vip_subscriber(self, telegram_id: str) -> None:
        with self.get_connection() as conn:
            conn.execute("UPDATE vip_subscribers SET is_active = 0 WHERE telegram_id = ?", (telegram_id,))

    def get_vip_subscriber(self, telegram_id: str) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            row = conn.execute("SELECT * FROM vip_subscribers WHERE telegram_id = ?", (telegram_id,)).fetchone()
            return dict(row) if row else None

    def save_pending_order(
        self,
        sig_key: str,
        security_id: str,
        symbol: str,
        direction: str,
        entry_price: float,
        stop_loss: float,
        target: float,
        lot_size: int,
        margin_req: float,
        total_lot_price: float,
    ) -> None:
        """Persists pending 1-click execution order so buttons remain active across server reboots."""
        with self.get_connection() as conn:
            conn.execute("""
                INSERT INTO pending_orders (
                    sig_key, security_id, symbol, direction, entry_price,
                    stop_loss, target, lot_size, margin_req, total_lot_price
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(sig_key) DO UPDATE SET
                    security_id = excluded.security_id,
                    symbol = excluded.symbol,
                    direction = excluded.direction,
                    entry_price = excluded.entry_price,
                    stop_loss = excluded.stop_loss,
                    target = excluded.target,
                    lot_size = excluded.lot_size,
                    margin_req = excluded.margin_req,
                    total_lot_price = excluded.total_lot_price,
                    created_at = CURRENT_TIMESTAMP;
            """, (sig_key, str(security_id), symbol, str(direction), float(entry_price),
                  float(stop_loss), float(target), int(lot_size), float(margin_req), float(total_lot_price)))

    def get_pending_order(self, sig_key: str) -> Optional[Dict[str, Any]]:
        """Retrieves persistent pending order by sig_key."""
        with self.get_connection() as conn:
            row = conn.execute("SELECT * FROM pending_orders WHERE sig_key = ?", (sig_key,)).fetchone()
            return dict(row) if row else None

    def get_latest_signal_for_sec(self, security_id: str) -> Optional[Dict[str, Any]]:
        """Fallback to retrieve latest signal for security_id if order is not in pending_orders."""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM signals WHERE security_id = ? ORDER BY id DESC LIMIT 1",
                (str(security_id),)
            ).fetchone()
            return dict(row) if row else None

    def get_latest_signal_by_symbol(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Fallback to retrieve latest signal by stock symbol."""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM signals WHERE symbol = ? ORDER BY id DESC LIMIT 1",
                (symbol.upper(),)
            ).fetchone()
            return dict(row) if row else None

    # -------------------------------------------------------------
    # Daily Bias Engine Repository Methods
    # -------------------------------------------------------------
    def save_daily_bias_snapshot(self, snapshot: Dict[str, Any]) -> str:
        """Persists immutable daily bias snapshot."""
        with self.get_connection() as conn:
            conn.execute("""
                INSERT INTO daily_bias_snapshots (
                    snapshot_id, symbol, security_id, exchange, trading_date,
                    previous_session_date, reference_session_date,
                    previous_close, reference_high, reference_low,
                    daily_bias, reason_code, source_data_timestamp,
                    calculated_at, rule_version, data_health, raw_details_json, is_corrected
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(snapshot_id) DO UPDATE SET
                    is_corrected = excluded.is_corrected,
                    raw_details_json = excluded.raw_details_json;
            """, (
                snapshot["snapshot_id"],
                snapshot["symbol"],
                str(snapshot.get("security_id", "")),
                snapshot.get("exchange", "NSE"),
                snapshot["trading_date"],
                snapshot["previous_session_date"],
                snapshot["reference_session_date"],
                float(snapshot["previous_close"]),
                float(snapshot["reference_high"]),
                float(snapshot["reference_low"]),
                snapshot["daily_bias"],
                snapshot["reason_code"],
                snapshot.get("source_data_timestamp"),
                snapshot.get("calculated_at", datetime.now().isoformat()),
                snapshot.get("rule_version", "v1.0"),
                snapshot.get("data_health", "HEALTHY"),
                json.dumps(snapshot.get("raw_details", {})),
                1 if snapshot.get("is_corrected") else 0,
            ))
        return snapshot["snapshot_id"]

    def get_daily_bias_snapshot(
        self, symbol: str, trading_date: str, rule_version: str = "v1.0"
    ) -> Optional[Dict[str, Any]]:
        """Retrieves daily bias snapshot for symbol and date."""
        with self.get_connection() as conn:
            row = conn.execute("""
                SELECT * FROM daily_bias_snapshots
                WHERE symbol = ? AND trading_date = ? AND rule_version = ?
                ORDER BY is_corrected DESC, created_at DESC LIMIT 1
            """, (symbol.upper(), trading_date, rule_version)).fetchone()
            if row:
                d = dict(row)
                if d.get("raw_details_json"):
                    try:
                        d["raw_details"] = json.loads(d["raw_details_json"])
                    except Exception:
                        d["raw_details"] = {}
                return d
            return None

    def get_all_daily_bias_snapshots(self, trading_date: str, rule_version: str = "v1.0") -> List[Dict[str, Any]]:
        """Retrieves all daily bias snapshots for a given trading session."""
        with self.get_connection() as conn:
            rows = conn.execute("""
                SELECT * FROM daily_bias_snapshots
                WHERE trading_date = ? AND rule_version = ?
                ORDER BY symbol ASC
            """, (trading_date, rule_version)).fetchall()
            return [dict(r) for r in rows]

    # -------------------------------------------------------------
    # Liquidity Context Repository Methods
    # -------------------------------------------------------------
    def save_liquidity_context_event(self, event: Dict[str, Any]) -> str:
        """Persists liquidity context event (sweep, pivot state)."""
        with self.get_connection() as conn:
            conn.execute("""
                INSERT INTO liquidity_context_events (
                    event_id, symbol, security_id, timeframe_minutes,
                    pivot_type, level_price, breach_bar_time, confirmation_time,
                    reclaim_bars, sweep_direction, sweep_extreme,
                    status, expiry_time, invalidation_time, rule_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    status = excluded.status,
                    invalidation_time = excluded.invalidation_time;
            """, (
                event["event_id"],
                event["symbol"],
                str(event.get("security_id", "")),
                int(event.get("timeframe_minutes", 60)),
                event["pivot_type"],
                float(event["level_price"]),
                event["breach_bar_time"],
                event["confirmation_time"],
                int(event["reclaim_bars"]),
                event["sweep_direction"],
                float(event["sweep_extreme"]),
                event.get("status", "ACTIVE"),
                event["expiry_time"],
                event.get("invalidation_time"),
                event.get("rule_version", "v1.0"),
            ))
        return event["event_id"]

    def get_active_liquidity_context(self, symbol: str, current_time: Optional[str] = None) -> List[Dict[str, Any]]:
        """Retrieves active (unexpired, not invalidated) context events for a symbol."""
        with self.get_connection() as conn:
            sql = """
                SELECT * FROM liquidity_context_events
                WHERE symbol = ? AND status = 'ACTIVE'
            """
            params = [symbol.upper()]
            if current_time:
                sql += " AND expiry_time > ?"
                params.append(current_time)
            sql += " ORDER BY confirmation_time DESC"
            rows = conn.execute(sql, tuple(params)).fetchall()
            return [dict(r) for r in rows]

    def update_liquidity_context_status(
        self, event_id: str, status: str, invalidation_time: Optional[str] = None
    ) -> None:
        """Updates the status of a context event (e.g. INVALIDATED, EXPIRED, CONSUMED)."""
        with self.get_connection() as conn:
            conn.execute("""
                UPDATE liquidity_context_events
                SET status = ?, invalidation_time = COALESCE(?, invalidation_time)
                WHERE event_id = ?
            """, (status, invalidation_time, event_id))

    # -------------------------------------------------------------
    # Bias Gate Decisions Repository Methods
    # -------------------------------------------------------------
    def save_bias_gate_decision(self, decision: Dict[str, Any]) -> str:
        """Persists an auditable bias gate decision."""
        with self.get_connection() as conn:
            conn.execute("""
                INSERT INTO bias_gate_decisions (
                    decision_id, candidate_id, symbol, security_id,
                    trade_date, candidate_direction, gate_mode, decision,
                    would_allow, is_allowed, daily_bias_snapshot_id,
                    liquidity_context_event_id, reason_code, timestamp, rule_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(decision_id) DO UPDATE SET
                    decision = excluded.decision,
                    is_allowed = excluded.is_allowed;
            """, (
                decision["decision_id"],
                decision["candidate_id"],
                decision["symbol"],
                str(decision.get("security_id", "")),
                decision["trade_date"],
                decision["candidate_direction"],
                decision["gate_mode"],
                decision["decision"],
                1 if decision.get("would_allow") else 0,
                1 if decision.get("is_allowed") else 0,
                decision.get("daily_bias_snapshot_id"),
                decision.get("liquidity_context_event_id"),
                decision["reason_code"],
                decision.get("timestamp", datetime.now().isoformat()),
                decision.get("rule_version", "v1.0"),
            ))
        return decision["decision_id"]

    def get_bias_gate_decisions_for_date(self, trade_date: str) -> List[Dict[str, Any]]:
        """Retrieves all gate decisions made on a specific date for reporting."""
        with self.get_connection() as conn:
            rows = conn.execute("""
                SELECT * FROM bias_gate_decisions
                WHERE trade_date = ?
                ORDER BY timestamp ASC
            """, (trade_date,)).fetchall()
            return [dict(r) for r in rows]

    # -------------------------------------------------------------
    # Trend Sweep FVG Setups Repository Methods
    # -------------------------------------------------------------
    def save_trend_sweep_setup(self, setup: Dict[str, Any]) -> str:
        """Persists or updates an auditable Trend Sweep FVG V1 setup."""
        sig_id = setup["signal_id"]
        with self.get_connection() as conn:
            conn.execute("""
                INSERT INTO trend_sweep_setups (
                    signal_id, symbol, security_id, strategy_version, direction,
                    status, detected_at, known_at, trade_date,
                    impulse_high, impulse_low, fib_retracement_min, fib_retracement_max,
                    poi_id, poi_type, poi_high, poi_low,
                    swept_level, sweep_extreme, reclaim_time, pre_breach_structure_ref,
                    displacement_bar_time, fvg_id, fvg_top, fvg_bottom,
                    planned_entry, initial_stop, target_price,
                    nominal_rr, net_rr, heuristic_score, score_breakdown_json,
                    rejection_reason, exit_version, filled_price, filled_time,
                    exit_price, exit_time, exit_reason, realized_pnl, realized_r,
                    quantity, raw_setup_json, created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?, ?,
                    ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                ON CONFLICT(signal_id) DO UPDATE SET
                    status = excluded.status,
                    filled_price = COALESCE(excluded.filled_price, trend_sweep_setups.filled_price),
                    filled_time = COALESCE(excluded.filled_time, trend_sweep_setups.filled_time),
                    exit_price = COALESCE(excluded.exit_price, trend_sweep_setups.exit_price),
                    exit_time = COALESCE(excluded.exit_time, trend_sweep_setups.exit_time),
                    exit_reason = COALESCE(excluded.exit_reason, trend_sweep_setups.exit_reason),
                    realized_pnl = COALESCE(excluded.realized_pnl, trend_sweep_setups.realized_pnl),
                    realized_r = COALESCE(excluded.realized_r, trend_sweep_setups.realized_r),
                    rejection_reason = COALESCE(excluded.rejection_reason, trend_sweep_setups.rejection_reason),
                    raw_setup_json = excluded.raw_setup_json,
                    updated_at = CURRENT_TIMESTAMP;
            """, (
                sig_id,
                setup["symbol"],
                str(setup.get("security_id", "")),
                setup.get("strategy_version", "TREND_SWEEP_FVG_V1"),
                setup["direction"],
                setup["status"],
                setup.get("detected_at"),
                setup.get("known_at"),
                setup["trade_date"],
                setup.get("impulse_high"),
                setup.get("impulse_low"),
                setup.get("fib_retracement_min"),
                setup.get("fib_retracement_max"),
                setup.get("poi_id"),
                setup.get("poi_type"),
                setup.get("poi_high"),
                setup.get("poi_low"),
                setup.get("swept_level"),
                setup.get("sweep_extreme"),
                setup.get("reclaim_time"),
                setup.get("pre_breach_structure_ref"),
                setup.get("displacement_bar_time"),
                setup.get("fvg_id"),
                setup.get("fvg_top"),
                setup.get("fvg_bottom"),
                setup.get("planned_entry"),
                setup.get("initial_stop"),
                setup.get("target_price"),
                setup.get("nominal_rr"),
                setup.get("net_rr"),
                setup.get("heuristic_score"),
                json.dumps(setup.get("score_breakdown", {})) if isinstance(setup.get("score_breakdown"), dict) else setup.get("score_breakdown_json"),
                setup.get("rejection_reason"),
                setup.get("exit_version"),
                setup.get("filled_price"),
                setup.get("filled_time"),
                setup.get("exit_price"),
                setup.get("exit_time"),
                setup.get("exit_reason"),
                setup.get("realized_pnl"),
                setup.get("realized_r"),
                setup.get("quantity"),
                json.dumps(setup) if not isinstance(setup.get("raw_setup_json"), str) else setup.get("raw_setup_json"),
            ))
        return sig_id

    def update_trend_sweep_setup(self, signal_id: str, updates: Dict[str, Any]) -> None:
        """Updates specific fields of an existing trend sweep setup."""
        if not updates:
            return
        fields = []
        params = []
        for k, v in updates.items():
            fields.append(f"{k} = ?")
            if isinstance(v, (dict, list)):
                params.append(json.dumps(v))
            else:
                params.append(v)
        fields.append("updated_at = CURRENT_TIMESTAMP")
        params.append(signal_id)
        sql = f"UPDATE trend_sweep_setups SET {', '.join(fields)} WHERE signal_id = ?"
        with self.get_connection() as conn:
            conn.execute(sql, tuple(params))

    def get_trend_sweep_setup(self, signal_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves a single trend sweep setup by signal ID."""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM trend_sweep_setups WHERE signal_id = ?",
                (signal_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_trend_sweep_setups(
        self,
        trade_date: Optional[str] = None,
        symbol: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Queries trend sweep setups with optional filters."""
        with self.get_connection() as conn:
            clauses = []
            params = []
            if trade_date:
                clauses.append("trade_date = ?")
                params.append(trade_date)
            if symbol:
                clauses.append("symbol = ?")
                params.append(symbol.upper())
            if status:
                clauses.append("status = ?")
                params.append(status)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            sql = f"SELECT * FROM trend_sweep_setups {where} ORDER BY created_at DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, tuple(params)).fetchall()
            return [dict(r) for r in rows]


db = Database()


