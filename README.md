# ⚡ ORB Stock Scanner & Backtest Engine (DhanHQ API v2 + Telegram)

A production-grade real-time Opening Range Breakout (ORB) scanner and historical backtesting backend for the Indian stock market (NSE) using **DhanHQ API v2** and **Telegram notifications**.

> **IMPORTANT: SIGNAL & ALERT ONLY**
> 
> This application is strictly an alert/scanner and paper tracking system. It **NEVER** calls any order placement, order modification, Super Order, Forever Order, or real position-taking endpoints. Real order execution is completely disabled.

---

## 🌟 Key Features

1. **Direct Live Feed WebSocket (DhanHQ API v2)**:
   - Connects to DhanHQ binary market feed (`wss://api-feed.dhan.co`).
   - Binary packet unpacking for **Ticker (Response Code 2)** and **Quote (Response Code 4)** packets.
   - Batch subscription partitioning (100 instruments/batch).
   - Auto-reconnect with exponential backoff and heartbeat/stale feed watchdog.
   - Resubscribes automatically upon reconnection.
2. **Deterministic Candle Engine**:
   - Aggregates incoming live ticks into strict 1-minute Asia/Kolkata candles.
   - Rolls 1m candles into 5-minute candles (09:15-09:20, 09:20-09:25, 09:25-09:30, 09:30-09:35, etc.).
   - Signals trigger **only on finalized/closed candles**. Incomplete bars are never used.
3. **Exact ORB Strategy with TradingView Parity**:
   - **Opening Range**: Calculated strictly from `09:15:00` inclusive to `09:30:00` exclusive.
   - **Breakout Checks**: Start strictly after `09:30:00` IST.
   - **Long Breakout**: Current closed 5m candle `close > ORB_HIGH` AND previous closed 5m candle `close <= ORB_HIGH`.
   - **Short Breakout**: Current closed 5m candle `close < ORB_LOW` AND previous closed 5m candle `close >= ORB_LOW`.
   - **Stop Loss**: Configurable (`opposite_or` [default], `orb_midpoint`, `fixed_percent`).
   - **Target**: Configurable Risk:Reward ratio (default `2.0R`).
   - **Trade Limits**: Max 1 signal per symbol per day (per-symbol limit, not global).
   - **Idempotency Key**: Prevents duplicate alerts across restarts.
4. **Paper Signal Tracker (Virtual Positions)**:
   - Tracks simulated entries, stop losses, and profit targets.
   - **Conservative Conflict Resolution**: If both Target and Stop Loss are touched inside the same historical candle, the engine assumes **Stop Loss hit first** to eliminate optimistic bias.
   - Automatic EOD square-off at `15:25:00` IST.
   - Real-world Indian regulatory cost modeling (Brokerage, STT, Exchange Turnover charges, GST, SEBI charges, Stamp Duty, Slippage).
5. **Dhan Official Scrip Master & Dynamic Universe**:
   - Downloads and indexes official Dhan CSV (`https://images.dhan.co/api-data/api-scrip-master.csv`).
   - Maps `symbol <-> security_id`.
   - Supports modes: `custom`, `nifty50`, `nifty100`, `all_nse_equity`.
6. **Crash & Restart Recovery**:
   - If started after 09:15, backfills missing intraday 1m candles using Dhan's historical intraday API.
   - Reconstructs the day's correct ORB levels before connecting to the live stream.
7. **Telegram Real-Time Alerts**:
   - Rich HTML alerts for Breakouts, Target Hits, Stop Loss Hits, Startup, and EOD Daily Summaries.
   - Isolated from market data loop with retry backoff and rate-limit handling.
8. **Shared Strategy Historical Backtester**:
   - Reuses the **exact same strategy class** for backtesting and live trading.
   - Exports `reports/trades.csv`, `reports/backtest_summary.csv`, `reports/monthly.csv`, `reports/by_symbol.csv`, and `reports/report.html`.

---

## 📁 Project Structure

```
paper_lab/
├── main.py                     # CLI Entrypoint (validate, instruments, live, backtest, status)
├── requirements.txt            # Python dependencies
├── .env.example                # Template for environment variables
├── .gitignore                  # Git exclusions for secrets, sqlite, cache
├── README.md                   # Documentation
│
├── app/
│   ├── config.py               # Pydantic & YAML settings, sensitive token masking, logging
│   │
│   ├── dhan/
│   │   ├── auth.py             # DhanHQ v2 profile validation & Data plan check
│   │   ├── instruments.py      # Dhan Scrip Master downloader & universe resolver
│   │   ├── historical.py       # Historical intraday downloader & restart recovery
│   │   └── live_feed.py        # WebSocket client, binary packet parser, reconnect loop
│   │
│   ├── market/
│   │   ├── candle_builder.py   # Deterministic 1m and 5m candle engine
│   │   └── session.py          # Asia/Kolkata session boundaries & trading calendar
│   │
│   ├── strategies/
│   │   └── orb.py              # Exact ORB strategy with TradingView parity & debug mode
│   │
│   ├── trading/
│   │   └── paper_tracker.py    # Virtual trade tracker & Indian regulatory cost calculator
│   │
│   ├── notifications/
│   │   └── telegram.py         # Async Telegram alerts with retry & rate limiting
│   │
│   ├── storage/
│   │   ├── database.py         # SQLite manager (WAL mode, transactions, schemas)
│   │   └── models.py           # Data models (Candle, ORBLevels, Signal, PaperTrade)
│   │
│   └── backtest/
│       ├── downloader.py       # Batch historical intraday data downloader
│       ├── engine.py           # Simulation engine (zero lookahead, shared strategy)
│       ├── metrics.py          # Quant metrics (win rate, PF, expectancy, drawdown)
│       └── orb_backtester.py   # Backtest orchestrator and report exporter
│
├── config/
│   ├── strategy.yaml           # Strategy parameters, risk, and filters
│   └── universe.yaml           # Universe definitions and stock lists
│
├── data/                       # Local SQLite DB, cached scrip master, historical OHLCV
├── reports/                    # Backtest output CSVs and HTML report
├── logs/                       # Rotating log files with redacted secrets
└── tests/                      # Pytest unit and integration test suite
```

---

## 🚀 Setup & Installation

### 1. Requirements
- Python 3.11+ (Python 3.13 supported)
- DhanHQ API v2 credentials (Client ID & Access Token)
- Telegram Bot Token & Chat ID (Optional for testing, required for live alerts)

### 2. Environment Setup
```bash
# Clone or navigate to directory
cd "d:\micro tool\paper_lab"

# Create virtual environment
python -m venv .venv

# Activate virtual environment
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 3. Configure Credentials
Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```
Edit `.env`:
```ini
DHAN_CLIENT_ID=your_dhan_client_id
DHAN_ACCESS_TOKEN=your_dhan_access_token

TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_telegram_chat_id

TIMEZONE=Asia/Kolkata
LOG_LEVEL=INFO
```

---

## 🛠️ CLI Commands

### 1. System Validation
Tests your SQLite database, verifies Dhan credentials, checks Data API subscription, and sends a test message to Telegram:
```bash
python main.py validate
```

### 2. Download / Refresh Dhan Instrument Master
Downloads the 25MB official Dhan Scrip Master, indexes all NSE Equity instruments, and verifies universe resolution:
```bash
python main.py instruments
```

### 3. Inspect System Status
Shows market hours status, credentials configuration, and database record counts:
```bash
python main.py status
```

### 4. Run Historical Backtest
Runs historical simulation over actual historical intraday OHLCV bars:
```bash
# Backtest Nifty 50 universe
python main.py backtest --from 2026-09-01 --to 2026-09-25 --universe nifty50

# Backtest specific symbols with TradingView debug output:
python main.py backtest --from 2026-09-25 --to 2026-09-25 --symbols RELIANCE,TCS --debug
```
Outputs generated in `reports/`:
- `reports/backtest_summary.csv`
- `reports/trades.csv`
- `reports/monthly.csv`
- `reports/by_symbol.csv`
- `reports/report.html` (interactive HTML report)

### 5. Start Live Scanner
Launches the live feed, performs late-start recovery if started after 09:15, listens to WebSocket ticks, builds closed candles, and sends Telegram alerts:
```bash
python main.py live

# With TradingView comparison debug prints:
python main.py live --debug
```

---

## 🧪 Running Unit & Integration Tests

The test suite validates ORB calculations, boundaries, break rules, buffer, stops, targets, candle engine, tie-break conservatism, and mocked Dhan/Telegram APIs:
```bash
pytest -v
```

---

## 🔒 Security & Safety Guarantees

- **No Order Placement**: The codebase does not import or call any order execution APIs.
- **Credential Redaction**: Tokens and IDs are automatically masked in console outputs, rotating log files (`logs/orb_scanner.log`), and exception traces.
- **Fail-Safe Disconnects**: If market data goes stale or WebSocket drops, auto-reconnection is triggered with exponential backoff.
- **Idempotency Protection**: Alerts and signals are checked against SQLite to avoid duplicate alerts during restarts.
