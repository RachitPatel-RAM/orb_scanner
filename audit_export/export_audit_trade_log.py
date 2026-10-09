"""
Audit Export Script: Generates full Trade Log CSV, true Peak-to-Trough Drawdown,
and detailed trade parameters for institutional review.
"""

import csv
from datetime import datetime
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, os.path.abspath("."))
import httpx

from app.analysis.pivot_points import calculate_traditional_pivots

def fetch_5year_data() -> list:
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%5ENSEI?interval=1d&range=5y"
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = httpx.get(url, headers=headers)
    d = resp.json()["chart"]["result"][0]
    timestamps = d["timestamp"]
    q = d["indicators"]["quote"][0]
    records = []
    for i in range(len(timestamps)):
        if (
            q["open"][i] is not None
            and q["high"][i] is not None
            and q["low"][i] is not None
            and q["close"][i] is not None
        ):
            records.append({
                "date": datetime.fromtimestamp(timestamps[i]).date(),
                "open": float(q["open"][i]),
                "high": float(q["high"][i]),
                "low": float(q["low"][i]),
                "close": float(q["close"][i]),
            })
    return records


def generate_audit_package():
    records = fetch_5year_data()
    starting_capital = 30000.0
    current_balance = starting_capital
    peak_balance = starting_capital
    max_drawdown_amount = 0.0
    max_drawdown_pct = 0.0
    
    csv_rows = []
    equity_curve = []
    
    trade_id = 0
    total_wins = 0
    total_losses = 0

    for i in range(1, len(records)):
        prev = records[i - 1]
        cur = records[i]
        t_date = cur["date"]
        
        daily_rng = cur["high"] - cur["low"]
        rng_pct = (daily_rng / cur["low"]) * 100.0

        # Scenario B Filter: Daily Range >= 0.85%
        if rng_pct < 0.85:
            continue

        pivots = calculate_traditional_pivots(prev["high"], prev["low"], prev["close"], prev["date"])
        orb_rng = daily_rng * 0.38
        rng_mid = (cur["high"] + cur["low"]) / 2.0
        orb_high = round(rng_mid + (orb_rng / 2.0), 2)
        orb_low = round(rng_mid - (orb_rng / 2.0), 2)

        direction = None
        if cur["high"] > orb_high and cur["close"] > rng_mid:
            direction = "CALL (CE)"
            entry_p = orb_high + 2.0
            sl = rng_mid
            t1 = pivots.r1 if pivots.r1 > entry_p else pivots.r2
        elif cur["low"] < orb_low and cur["close"] < rng_mid:
            direction = "PUT (PE)"
            entry_p = orb_low - 2.0
            sl = rng_mid
            t1 = pivots.s1 if pivots.s1 < entry_p else pivots.s2
        else:
            continue

        trade_id += 1
        
        # Historical NSE Lot Size Logic:
        # Prior to April 2024: Nifty lot was 50 (from July 2021 to April 2024)
        # Nov 2024 onwards: Nifty lot was revised to 75 / 65
        if t_date < datetime(2024, 4, 25).date():
            lot_units = 50
        else:
            lot_units = 75

        # Position Sizing
        active_lots = max(1, min(5, int(current_balance // 30000.0)))
        total_qty = active_lots * lot_units

        # Modeled options execution
        # Entry ATM strike rounded to nearest 50
        atm_strike = round(entry_p / 50.0) * 50
        entry_opt_ltp = 95.0

        if direction == "CALL (CE)":
            if cur["high"] >= t1:
                spot_move = t1 - entry_p if cur["close"] <= t1 else (cur["close"] - entry_p)
                exit_reason = "TARGET_1_HIT"
            elif cur["low"] <= sl:
                spot_move = -(entry_p - sl)
                exit_reason = "STOP_LOSS_HIT"
            else:
                spot_move = cur["close"] - entry_p
                exit_reason = "EOD_EXIT"
        else:
            if cur["low"] <= t1:
                spot_move = entry_p - t1 if cur["close"] >= t1 else (entry_p - cur["close"])
                exit_reason = "TARGET_1_HIT"
            elif cur["high"] >= sl:
                spot_move = -(sl - entry_p)
                exit_reason = "STOP_LOSS_HIT"
            else:
                spot_move = entry_p - cur["close"]
                exit_reason = "EOD_EXIT"

        delta = 0.55 if exit_reason == "TARGET_1_HIT" else 0.48
        gross_opt_pts = spot_move * delta
        
        # Modeled Theta burn (2 hours holding)
        is_expiry_day = (t_date.weekday() == 3)
        theta_decay_pts = 7.0 if is_expiry_day else 3.5
        net_opt_pts = gross_opt_pts - theta_decay_pts - 0.8  # 0.8 slippage
        
        exit_opt_ltp = max(0.5, round(entry_opt_ltp + net_opt_pts, 2))
        gross_pnl = round((exit_opt_ltp - entry_opt_ltp) * total_qty, 2)

        # Accurate Statutory Charges (Dhan Flat Rs. 20/order = Rs. 40 round trip)
        brokerage = 40.0
        # Turnover
        buy_turnover = entry_opt_ltp * total_qty
        sell_turnover = exit_opt_ltp * total_qty
        
        # STT on Options Sale (0.0625% prior to Oct 2024, 0.1% onwards)
        stt_rate = 0.001 if t_date >= datetime(2024, 10, 1).date() else 0.000625
        stt = round(sell_turnover * stt_rate, 2)
        
        # Exchange charges (NSE: ~0.05% of premium turnover)
        exch_turnover = (buy_turnover + sell_turnover) * 0.0005
        gst = round((brokerage + exch_turnover) * 0.18, 2)
        stamp_duty = round(buy_turnover * 0.00003, 2)
        sebi_charges = round((buy_turnover + sell_turnover) * 0.000001, 2)
        
        total_costs = round(brokerage + stt + exch_turnover + gst + stamp_duty + sebi_charges, 2)
        net_pnl = round(gross_pnl - total_costs, 2)

        current_balance = round(current_balance + net_pnl, 2)
        if current_balance > peak_balance:
            peak_balance = current_balance
        
        dd_amt = peak_balance - current_balance
        dd_pct = (dd_amt / peak_balance) * 100.0 if peak_balance > 0 else 0.0
        if dd_amt > max_drawdown_amount:
            max_drawdown_amount = dd_amt
        if dd_pct > max_drawdown_pct:
            max_drawdown_pct = dd_pct

        if net_pnl > 0:
            total_wins += 1
        else:
            total_losses += 1

        csv_rows.append({
            "Trade_ID": trade_id,
            "Date": t_date.isoformat(),
            "Underlying": "NIFTY 50",
            "Direction": direction,
            "Strike": atm_strike,
            "Lots": active_lots,
            "Total_Qty": total_qty,
            "Entry_Spot": round(entry_p, 2),
            "Modeled_Entry_Premium": entry_opt_ltp,
            "Modeled_Exit_Premium": exit_opt_ltp,
            "Spot_Move_Pts": round(spot_move, 2),
            "Gross_PnL": gross_pnl,
            "Brokerage": brokerage,
            "STT": stt,
            "Exchange_Levies_GST": round(total_costs - brokerage - stt, 2),
            "Total_Charges": total_costs,
            "Net_PnL": net_pnl,
            "Exit_Reason": exit_reason,
            "Account_Balance": current_balance,
            "Peak_Balance": peak_balance,
            "Drawdown_Pct": round(dd_pct, 2),
        })

    # Save to CSV
    out_dir = Path("audit_export")
    out_dir.mkdir(exist_ok=True)
    csv_file = out_dir / "audit_5year_trades.csv"
    
    with open(csv_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(csv_rows[0].keys()))
        writer.writeheader()
        writer.writerows(csv_rows)

    metrics = {
        "total_trades": trade_id,
        "wins": total_wins,
        "losses": total_losses,
        "win_rate_pct": round((total_wins / trade_id) * 100.0, 2),
        "starting_capital": starting_capital,
        "ending_capital": current_balance,
        "peak_capital": peak_balance,
        "max_drawdown_amount": round(max_drawdown_amount, 2),
        "max_drawdown_pct": round(max_drawdown_pct, 2),
        "csv_path": str(csv_file.absolute()),
    }
    
    with open(out_dir / "audit_summary_metrics.json", "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)

    print(f"Audit Package Generated Successfully!")
    print(f"Total Trades: {trade_id} | Win Rate: {metrics['win_rate_pct']}%")
    print(f"Starting Capital: Rs. {starting_capital:,.2f}")
    print(f"Ending Capital: Rs. {current_balance:,.2f}")
    print(f"Max Peak-to-Trough Drawdown: Rs. {max_drawdown_amount:,.2f} ({max_drawdown_pct:.2f}%)")
    print(f"Saved Trade CSV to: {csv_file}")

if __name__ == "__main__":
    generate_audit_package()
