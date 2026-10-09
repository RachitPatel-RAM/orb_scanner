"""
Scalp Journal Reporting and Verification Tool.
Queries the SQLite audit table and prints clean, auditable reports for:
- Last 10 days
- Last 15 days
- Full 1 Month (22-23 sessions)
- Any custom period requested by the trader.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from tabulate import tabulate

def generate_journal_report(limit: int = 30) -> None:
    conn = sqlite3.connect("data/orb_scanner.db")
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("""
        SELECT session_num, trade_date, nifty_open, gap_pts, setup_type,
               trade_direction, outcome, net_pnl, running_capital
        FROM audit_scalp_sessions
        ORDER BY trade_date ASC
    """)
    rows = cur.fetchall()
    conn.close()

    if not rows:
        print("No audit sessions found in database.")
        return

    selected_rows = rows[-limit:] if limit < len(rows) else rows

    table_data = []
    total_trades = 0
    wins = 0
    shields = 0
    losses = 0
    skips = 0
    total_net = 0.0

    start_cap = selected_rows[0]["running_capital"] - selected_rows[0]["net_pnl"]

    for r in selected_rows:
        d = r["trade_date"]
        direction = r["trade_direction"]
        outcome = r["outcome"]
        net = r["net_pnl"]
        cap = r["running_capital"]
        total_net += net

        if direction != "NO_TRADE":
            total_trades += 1
            if "TARGET_HIT" in outcome:
                wins += 1
            elif "COST_SHIELD" in outcome:
                shields += 1
            elif "STOP_LOSS" in outcome:
                losses += 1
        else:
            skips += 1

        table_data.append([
            r["session_num"],
            d,
            f"{r['nifty_open']:,.1f}",
            f"{r['gap_pts']:+,.1f}",
            direction,
            outcome,
            f"{net:+,.2f}",
            f"Rs. {cap:,.2f}"
        ])

    headers = ["#", "Date", "Nifty Open", "Gap Pts", "Dir", "Outcome", "Net P&L", "Running Capital"]
    print(f"\n=========================================================================================")
    print(f"  BORN BULL FAST SCALP JOURNAL AUDIT (LAST {len(selected_rows)} SESSIONS: {selected_rows[0]['trade_date']} TO {selected_rows[-1]['trade_date']})")
    print(f"=========================================================================================\n")
    print(tabulate(table_data, headers=headers, tablefmt="github"))

    end_cap = selected_rows[-1]["running_capital"]
    roi = (total_net / start_cap * 100.0) if start_cap else 0.0
    defense_rate = ((wins + shields) / max(1, total_trades)) * 100.0 if total_trades else 0.0

    print(f"\n-----------------------------------------------------------------------------------------")
    print(f"SUMMARY METRICS:")
    print(f"• Total Sessions Analyzed: {len(selected_rows)}")
    print(f"• Trades Executed: {total_trades} | Chop Days Defended (0 loss): {skips}")
    print(f"• Targets Hit (+28 pts): {wins} ({wins/max(1, total_trades)*100:.1f}%)")
    print(f"• Cost Shields (+1.0 pt, 0 loss): {shields} ({shields/max(1, total_trades)*100:.1f}%)")
    print(f"• Stop Losses Hit (-12 pts): {losses} ({losses/max(1, total_trades)*100:.1f}%)")
    print(f"• Capital Defense Rate: {defense_rate:.1f}% (Win + Cost Shield)")
    print(f"• Starting Balance: Rs. {start_cap:,.2f}")
    print(f"• Ending Balance: Rs. {end_cap:,.2f}")
    print(f"• Net Profit (After Brokerage & Taxes): Rs. {total_net:+,.2f} ({roi:+.2f}% ROI)")
    print(f"-----------------------------------------------------------------------------------------\n")

if __name__ == "__main__":
    limit_val = 30
    if len(sys.argv) > 1:
        try:
            limit_val = int(sys.argv[1])
        except ValueError:
            pass
    generate_journal_report(limit_val)
