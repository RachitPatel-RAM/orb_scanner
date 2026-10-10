"""
Timeline simulation to reach:
1. 108 Lots in NIFTY (Auspicious sacred ceiling, Capital: Rs. 35 Lakhs)
2. Multi-Index Portfolio: NIFTY 108 + BankNifty 60 + Sensex 50 (Capital: Rs. 70 Lakhs)
"""

from datetime import date
from dateutil.relativedelta import relativedelta

def project_multi_index_milestones():
    # Average case rate per Nifty lot: Rs. 17,440/month
    # Starting Oct 10, 2026
    start_d = date(2026, 10, 10)

    # Path from M01 to M24
    bal = 0.0
    personal_inv = 0.0
    rate = 17440.0

    timeline = []

    for m in range(1, 25):
        s_date = start_d + relativedelta(months=m-1)
        e_date = start_d + relativedelta(months=m)
        period_str = f"{s_date.strftime('%d-%b-%Y')} to {e_date.strftime('%d-%b-%Y')}"

        if m == 1:
            timeline.append({
                "m": m, "period": period_str, "lots": 0, "bal": 0.0, "pnl": 0.0, "milestone": "Paper Trading Verification"
            })
        elif m == 2:
            personal_inv = 30000.0
            pnl = 1 * rate
            bal = 30000.0 + pnl
            timeline.append({
                "m": m, "period": period_str, "lots": 1, "bal": bal, "pnl": pnl, "milestone": "Live Launch 1 Lot"
            })
        elif m == 3:
            topup = max(0.0, 60000.0 - bal)
            personal_inv += topup
            bal = 60000.0 + (2 * rate)
            timeline.append({
                "m": m, "period": period_str, "lots": 2, "bal": bal, "pnl": 2*rate, "milestone": "2 Lots Scale-up"
            })
        elif m == 4:
            topup = max(0.0, 100000.0 - bal)
            personal_inv += topup
            pnl = 3 * rate
            bal = (100000.0 + pnl) - personal_inv # Withdraw principal!
            timeline.append({
                "m": m, "period": period_str, "lots": 3, "bal": bal, "pnl": pnl, "milestone": "Withdraw Principal! House Money"
            })
        else:
            lots = max(1, int(bal // 32000))
            pnl = lots * rate
            bal += pnl
            ms = "Compounding"
            if lots >= 108 and lots < 218:
                ms = "MILESTONE 1: 108 LOTS NIFTY!"
            elif lots >= 218:
                ms = "MILESTONE 2: FULL MULTI-INDEX (218 LOTS)!"
            timeline.append({
                "m": m, "period": period_str, "lots": lots, "bal": bal, "pnl": pnl, "milestone": ms
            })

    print("=== MULTI-INDEX COMPOUNDING MILESTONES ===")
    for t in timeline:
        print(f"M{t['m']:02d} ({t['period']}) | Lots: {t['lots']:<3} | Bal: Rs {t['bal']:<12,.0f} | Monthly PnL: Rs {t['pnl']:<11,.0f} | {t['milestone']}")

if __name__ == "__main__":
    project_multi_index_milestones()
