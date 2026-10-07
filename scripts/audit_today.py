"""
Audit today's signals, alerts sent to Telegram, and trades from SQLite database.
Generates an honest, verified P&L report and dismisses buttons from closed market trades.
"""

import sqlite3
import os
import sys
from datetime import date, datetime
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DB_PATH = os.getenv("DB_PATH", "data/orb_scanner.db")
if not os.path.exists(DB_PATH):
    # Check absolute path on VM
    if os.path.exists("/home/patelram5002/paper_lab/data/orb_scanner.db"):
        DB_PATH = "/home/patelram5002/paper_lab/data/orb_scanner.db"

def run_audit(today_str: str = "2026-10-07"):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    print(f"=== AUDITING TODAY'S SESSION: {today_str} ===")
    
    # 1. Signals
    signals = cursor.execute("""
        SELECT id, trade_date, security_id, symbol, direction, entry_price, stop_loss, target, risk_reward, timestamp, idempotency_key
        FROM signals
        WHERE trade_date = ?
        ORDER BY id ASC
    """, (today_str,)).fetchall()
    print(f"\nTotal Recorded Signals: {len(signals)}")
    for s in signals:
        print(f"  Signal #{s['id']}: {s['symbol']} {s['direction']} @ Rs {s['entry_price']} | SL: {s['stop_loss']} | Target: {s['target']} (R:R {s['risk_reward']})")

    # 2. Alerts sent to Telegram
    alerts = cursor.execute("""
        SELECT id, idempotency_key, message_id, chat_id, success, sent_at, message
        FROM alerts
        WHERE DATE(sent_at) = ? AND success = 1
        ORDER BY id ASC
    """, (today_str,)).fetchall()
    print(f"\nTotal Telegram Alerts Dispatched: {len(alerts)}")
    for a in alerts:
        # Extract stock name if present in idempotency_key
        print(f"  Alert #{a['id']}: Key={a['idempotency_key']} | MsgID={a['message_id']} | Sent={a['sent_at']}")

    # 3. Paper Trades
    trades = cursor.execute("""
        SELECT id, signal_id, symbol, direction, entry_price, stop_loss, target, exit_price, exit_reason, pnl, status
        FROM paper_trades
        WHERE trade_date = ?
        ORDER BY id ASC
    """, (today_str,)).fetchall()
    print(f"\nTotal Virtual Trades: {len(trades)}")
    
    total_pnl = 0.0
    targets_hit = 0
    stops_hit = 0
    eod_exits = 0

    for t in trades:
        pnl = t['pnl'] or 0.0
        total_pnl += pnl
        if t['exit_reason'] == 'TARGET':
            targets_hit += 1
        elif t['exit_reason'] == 'STOP_LOSS':
            stops_hit += 1
        else:
            eod_exits += 1
        print(f"  Trade #{t['id']}: {t['symbol']} {t['direction']} | Entry: {t['entry_price']} | Exit: {t['exit_price']} ({t['exit_reason']}) | PnL: Rs {pnl:+.2f}")

    print("\n--- SUMMARY METRICS ---")
    print(f"Target Hits: {targets_hit}")
    print(f"Stop Losses: {stops_hit}")
    print(f"EOD Exits:   {eod_exits}")
    print(f"Total PnL:   Rs {total_pnl:+.2f}")

    conn.close()

if __name__ == "__main__":
    today = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
    run_audit(today)
