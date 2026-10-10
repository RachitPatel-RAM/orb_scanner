"""
Generate Complete Dated Table from Today (10-Oct-2026) to 100 Lots Milestone.
Includes all months with exact dates, capital, lots, profit, and withdrawal.
"""

from datetime import date
from dateutil.relativedelta import relativedelta

def generate_100_lots_master_table():
    rate_avg = 17440.0
    rate_worst = 8485.0
    rate_best = 22435.0

    # Start date: 10-Oct-2026
    start_d = date(2026, 10, 10)

    # Average-case path
    months_avg = []
    bal = 0.0
    personal_inv = 0.0

    for m in range(1, 15):
        s_date = start_d + relativedelta(months=m-1)
        e_date = start_d + relativedelta(months=m)
        period_str = f"{s_date.strftime('%d-%b-%Y')} to {e_date.strftime('%d-%b-%Y')}"

        if m == 1:
            months_avg.append({
                "m": m, "period": period_str, "stage": "Paper Trade Verification",
                "lots": 0, "start_bal": 0.0, "pnl": 0.0, "end_bal": 0.0, "personal": 0.0
            })
        elif m == 2:
            personal_inv += 30000.0
            pnl = 1 * rate_avg
            bal = 30000.0 + pnl
            months_avg.append({
                "m": m, "period": period_str, "stage": "Live Launch (1 Lot)",
                "lots": 1, "start_bal": 30000.0, "pnl": pnl, "end_bal": bal, "personal": 30000.0
            })
        elif m == 3:
            topup = max(0.0, 60000.0 - bal)
            personal_inv += topup
            bal = 60000.0
            pnl = 2 * rate_avg
            bal += pnl
            months_avg.append({
                "m": m, "period": period_str, "stage": "Top-up to Rs. 60k (2 Lots)",
                "lots": 2, "start_bal": 60000.0, "pnl": pnl, "end_bal": bal, "personal": personal_inv
            })
        elif m == 4:
            topup = max(0.0, 100000.0 - bal)
            personal_inv += topup
            bal = 100000.0
            pnl = 3 * rate_avg
            bal_pre_withdraw = bal + pnl
            # Withdraw 100% personal money
            bal = bal_pre_withdraw - personal_inv
            months_avg.append({
                "m": m, "period": period_str, "stage": f"Top-up to 1L -> WITHDRAW ALL Rs. {personal_inv:,.0f}!",
                "lots": 3, "start_bal": 100000.0, "pnl": pnl, "end_bal": bal, "personal": 0.0
            })
        else:
            # Compounding
            lots = min(100, max(1, int(bal // 32000)))
            start_bal = bal
            pnl = lots * rate_avg
            bal += pnl
            stage_str = "House Money Compounding"
            if lots >= 100:
                stage_str = "TARGET REACHED: 100 LOTS!"
            months_avg.append({
                "m": m, "period": period_str, "stage": f"{stage_str} ({lots} Lots)",
                "lots": lots, "start_bal": start_bal, "pnl": pnl, "end_bal": bal, "personal": 0.0
            })

    print("=== MASTER DATED TABLE: ROADMAP TO 100 LOTS (AVERAGE CASE) ===")
    for r in months_avg:
        print(f"M{r['m']:02d} ({r['period']}) | Lots: {r['lots']:<3} | Start: Rs {r['start_bal']:<10,.0f} | Monthly Net: +Rs {r['pnl']:<11,.0f} | End Bal: Rs {r['end_bal']:<11,.0f} | Personal Risk: Rs {r['personal']:,.0f}")

if __name__ == "__main__":
    generate_100_lots_master_table()
