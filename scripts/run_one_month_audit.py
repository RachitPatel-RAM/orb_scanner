"""
1-Month Real Historical Audit & Future Comparison Benchmark Engine.
Period: September 8, 2026 to October 9, 2026 (Exact 1-Month / 23 Trading Sessions).
Capital: Rs. 30,000.00 | Lot Size: 1 Lot Nifty (75 Qty).
Strategy: Pure Morning Fast Scalp (09:12 Pre-Market & 09:16 Opening Momentum).
Friction: Rs. 69.60 Dhan Brokerage & Taxes + 1.0 pt Slippage (Total Rs. 144.60 on loss, Rs. 69.60 on win).
"""

from __future__ import annotations

from datetime import datetime, date
import json
import os
import sqlite3
import sys
from typing import Dict, List

import httpx

LOT_SIZE = 75
BROKERAGE_TAXES = 69.60   # Dhan Rs. 40 + STT Rs. 10.13 + Exchange Rs. 10.13 + GST Rs. 9.02 + SEBI/Stamp Rs. 0.32
SLIPPAGE_PTS = 1.0        # 1.0 pt slippage on entries/exits (Rs. 75.00)

def fetch_data() -> List[dict]:
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%5ENSEI?interval=1d&range=3mo"
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = httpx.get(url, headers=headers, timeout=15.0)
    if resp.status_code != 200:
        raise RuntimeError(f"Failed to fetch data: HTTP {resp.status_code}")

    d = resp.json()["chart"]["result"][0]
    ts = d["timestamp"]
    q = d["indicators"]["quote"][0]
    opens = q["open"]
    highs = q["high"]
    lows = q["low"]
    closes = q["close"]

    bars = []
    for i in range(len(ts)):
        if opens[i] is not None and highs[i] is not None and lows[i] is not None:
            c_val = closes[i] if closes[i] is not None else opens[i]
            dt = datetime.fromtimestamp(ts[i]).date()
            bars.append({
                "date": dt,
                "open": float(opens[i]),
                "high": float(highs[i]),
                "low": float(lows[i]),
                "close": float(c_val),
            })
    return bars

def run_one_month_audit():
    all_bars = fetch_data()
    
    # User's exact verified historical anchor for Oct 7, 8, 9
    user_anchors = {
        date(2026, 10, 7): {"open": 22690.45, "high": 22717.65, "low": 22546.30, "close": 22603.05},
        date(2026, 10, 8): {"open": 22599.05, "high": 22599.05, "low": 22179.90, "close": 22231.80},
        date(2026, 10, 9): {"open": 22314.95, "high": 22580.75, "low": 22294.75, "close": 22520.45},
    }
    for b in all_bars:
        if b["date"] in user_anchors:
            b.update(user_anchors[b["date"]])

    # Find bars for the last month ending Oct 9, 2026 (approx last 23 trading sessions)
    end_date = date(2026, 10, 9)
    # Take sessions from Sept 8, 2026 to Oct 9, 2026
    month_bars = [b for b in all_bars if date(2026, 9, 8) <= b["date"] <= end_date]
    if len(month_bars) < 15:
        # Fallback to last 23 bars
        month_bars = all_bars[-23:]

    # We need the previous day's close for the very first bar
    first_idx = all_bars.index(month_bars[0])
    prev_bar = all_bars[first_idx - 1] if first_idx > 0 else month_bars[0]

    starting_capital = 30000.0
    balance = starting_capital
    peak_balance = starting_capital
    max_drawdown = 0.0

    trades = []
    wins = 0
    shields = 0
    losses = 0
    skipped_chops = 0

    delta = 0.55
    target_opt_pts = 28.0
    target_spot_pts = target_opt_pts / delta   # ~50.9 index pts
    sl_opt_pts = 12.0
    sl_spot_pts = sl_opt_pts / delta           # ~21.8 index pts
    trail_spot_pts = 12.0 / delta              # ~21.8 index pts for +12 pt trail

    current_prev = prev_bar
    for bar in month_bars:
        d = bar["date"]
        pdh = current_prev["high"]
        pdl = current_prev["low"]
        pdc = current_prev["close"]
        today_open = bar["open"]
        day_high = bar["high"]
        day_low = bar["low"]
        day_close = bar["close"]

        gap_pts = today_open - pdc
        gap_pct = (gap_pts / pdc) * 100.0

        direction = None
        setup_type = None

        if gap_pct >= 0.25 and today_open >= pdh * 0.999:
            direction = "CALL"
            setup_type = "PRE_MARKET_GAP_UP"
        elif gap_pct <= -0.25 and today_open <= pdl * 1.001:
            direction = "PUT"
            setup_type = "PRE_MARKET_GAP_DOWN"
        elif abs(gap_pct) < 0.25:
            rng_mid = (pdh + pdl) / 2.0
            if day_high > pdh and today_open >= rng_mid:
                direction = "CALL"
                setup_type = "OPEN_0916_EXPANSION"
            elif day_low < pdl and today_open <= rng_mid:
                direction = "PUT"
                setup_type = "OPEN_0916_EXPANSION"
            else:
                direction = "NO_TRADE"
                setup_type = "FLAT_CHOP_DEFENSE"
        else:
            direction = "NO_TRADE"
            setup_type = "FLAT_CHOP_DEFENSE"

        trade_outcome = None
        gross_pnl = 0.0
        brokerage = 0.0
        net_pnl = 0.0

        if direction == "NO_TRADE":
            skipped_chops += 1
            trade_outcome = "SKIPPED_CHOP"
            gross_pnl = 0.0
            brokerage = 0.0
            net_pnl = 0.0
        elif direction == "CALL":
            brokerage = BROKERAGE_TAXES
            favorable = day_high - today_open
            adverse = today_open - day_low

            if favorable >= target_spot_pts:
                trade_outcome = "TARGET_HIT (+28 pts)"
                wins += 1
                gross_pnl = (target_opt_pts - SLIPPAGE_PTS) * LOT_SIZE  # +Rs. 2,025.00
                net_pnl = gross_pnl - brokerage                         # +Rs. 1,955.40
            elif favorable >= trail_spot_pts:
                trade_outcome = "COST_SHIELD (+1.0 pt)"
                shields += 1
                gross_pnl = 1.0 * LOT_SIZE                              # +Rs. 75.00
                net_pnl = gross_pnl - brokerage                         # +Rs. 5.40
            elif adverse >= sl_spot_pts:
                trade_outcome = "STOP_LOSS (-12 pts)"
                losses += 1
                gross_pnl = (-sl_opt_pts - SLIPPAGE_PTS) * LOT_SIZE    # -Rs. 975.00
                net_pnl = gross_pnl - brokerage                         # -Rs. 1,044.60
            else:
                trade_outcome = "COST_SHIELD (Flat Close)"
                shields += 1
                net_pnl = 0.0
        elif direction == "PUT":
            brokerage = BROKERAGE_TAXES
            favorable = today_open - day_low
            adverse = day_high - today_open

            if favorable >= target_spot_pts:
                trade_outcome = "TARGET_HIT (+28 pts)"
                wins += 1
                gross_pnl = (target_opt_pts - SLIPPAGE_PTS) * LOT_SIZE
                net_pnl = gross_pnl - brokerage
            elif favorable >= trail_spot_pts:
                trade_outcome = "COST_SHIELD (+1.0 pt)"
                shields += 1
                gross_pnl = 1.0 * LOT_SIZE
                net_pnl = gross_pnl - brokerage
            elif adverse >= sl_spot_pts:
                trade_outcome = "STOP_LOSS (-12 pts)"
                losses += 1
                gross_pnl = (-sl_opt_pts - SLIPPAGE_PTS) * LOT_SIZE
                net_pnl = gross_pnl - brokerage
            else:
                trade_outcome = "COST_SHIELD (Flat Close)"
                shields += 1
                net_pnl = 0.0

        balance = round(balance + net_pnl, 2)
        if balance > peak_balance:
            peak_balance = balance
        dd = peak_balance - balance
        if dd > max_drawdown:
            max_drawdown = dd

        trade_record = {
            "session_num": len(trades) + 1,
            "date": d.isoformat(),
            "nifty_open": today_open,
            "nifty_high": day_high,
            "nifty_low": day_low,
            "nifty_close": day_close,
            "gap_pts": round(gap_pts, 1),
            "setup_type": setup_type,
            "trade_direction": direction,
            "outcome": trade_outcome,
            "gross_pnl": round(gross_pnl, 2),
            "brokerage_taxes": round(brokerage, 2),
            "net_pnl": round(net_pnl, 2),
            "running_capital": balance,
        }
        trades.append(trade_record)
        current_prev = bar

    # Store in JSON file
    audit_data = {
        "metadata": {
            "period": f"{month_bars[0]['date'].isoformat()} to {month_bars[-1]['date'].isoformat()}",
            "total_sessions": len(month_bars),
            "starting_capital": starting_capital,
            "ending_capital": balance,
            "net_profit": round(balance - starting_capital, 2),
            "roi_percent": round((balance - starting_capital) / starting_capital * 100.0, 2),
            "total_trades": len([t for t in trades if t["trade_direction"] != "NO_TRADE"]),
            "targets_hit": wins,
            "cost_shields": shields,
            "stop_losses": losses,
            "skipped_chops": skipped_chops,
            "capital_defense_rate": round((wins + shields) / max(1, (wins + shields + losses)) * 100.0, 1),
            "max_drawdown": round(max_drawdown, 2),
            "audit_timestamp": datetime.now().isoformat(),
        },
        "sessions": trades,
    }

    os.makedirs("data", exist_ok=True)
    with open("data/scalp_audit_monthly.json", "w", encoding="utf-8") as f:
        json.dump(audit_data, f, indent=2)

    # Store in SQLite database
    db_path = "data/orb_scanner.db"
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("""
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
        )
    """)
    for t in trades:
        cur.execute("""
            INSERT OR REPLACE INTO audit_scalp_sessions (
                session_num, trade_date, nifty_open, nifty_high, nifty_low, nifty_close,
                gap_pts, setup_type, trade_direction, outcome, gross_pnl, brokerage_taxes,
                net_pnl, running_capital
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            t["session_num"], t["date"], t["nifty_open"], t["nifty_high"], t["nifty_low"],
            t["nifty_close"], t["gap_pts"], t["setup_type"], t["trade_direction"],
            t["outcome"], t["gross_pnl"], t["brokerage_taxes"], t["net_pnl"], t["running_capital"]
        ))
    conn.commit()
    conn.close()

    print(json.dumps(audit_data["metadata"], indent=2))
    return audit_data

if __name__ == "__main__":
    run_one_month_audit()
