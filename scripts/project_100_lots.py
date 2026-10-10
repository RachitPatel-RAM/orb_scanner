"""
Find exact dates when account reaches 100 Lots (Rs. 32 Lakhs capital)
for:
1. Worst-Case Scenario
2. Average-Case Scenario
3. Best-Case Scenario
"""

from scripts.simulate_3_scenarios_dated import run_dated_simulation

def project_to_100_lots():
    # Per lot monthly rates:
    rates = {
        "worst": {"name": "Worst-Case (~45% Win)", "rate": 8485.0},
        "average": {"name": "Average-Case (62.2% Win, 10Y Norm)", "rate": 17440.0},
        "best": {"name": "Best-Case (~75% Win)", "rate": 22435.0},
    }

    # Capital required for 100 lots: Rs. 32,00,000 (Rs. 32,000 per lot)
    target_capital = 3200000.0

    from datetime import date, timedelta
    from dateutil.relativedelta import relativedelta

    start_date = date(2026, 10, 10)

    for key, info in rates.items():
        rate = info["rate"]
        # M1: Paper
        # M2: 30k (1 lot)
        # M3: 60k (2 lots)
        # M4: 100k (3 lots) -> withdraw principal
        
        # In M2:
        m2_end = 30000.0 + rate
        # In M3:
        m3_topup = max(0.0, 60000.0 - m2_end)
        m3_end = 60000.0 + (2 * rate)
        # In M4:
        m4_topup = max(0.0, 100000.0 - m3_end)
        m4_pnl = 3 * rate
        total_principal = 30000.0 + m3_topup + m4_topup
        bal = (100000.0 + m4_pnl) - total_principal

        current_d = date(2027, 2, 10) # start of M5
        month_idx = 5
        reached = None

        history = []

        while month_idx <= 36: # check up to 3 years
            lots = max(1, int(bal // 32000))
            if lots >= 100 and reached is None:
                reached = (month_idx, current_d, bal, lots)

            next_d = current_d + relativedelta(months=1)
            pnl = lots * rate
            end_bal = bal + pnl
            history.append({
                "m": month_idx,
                "start_d": current_d.strftime("%d-%b-%Y"),
                "end_d": next_d.strftime("%d-%b-%Y"),
                "lots": lots,
                "start_bal": bal,
                "pnl": pnl,
                "end_bal": end_bal
            })
            bal = end_bal
            current_d = next_d
            month_idx += 1
            if lots >= 100 and reached is not None and month_idx > reached[0] + 1:
                break

        print("="*80)
        print(f"SCENARIO: {info['name']}")
        if reached:
            print(f"--> REACHES 100 LOTS IN: Month {reached[0]} ({reached[1].strftime('%B %Y')})")
            print(f"Account Balance at Milestone: Rs. {reached[2]:,.2f} (Trades {reached[3]} Lots)")
        else:
            print("Did not reach in 36 months.")


        print("\nMilestone Progression around 100 Lots:")
        for h in history[-5:]:
            print(f"M{h['m']:02d} ({h['start_d']} to {h['end_d']}) | Lots: {h['lots']:<3} | Bal: Rs {h['start_bal']:<11,.0f} | Monthly PnL: Rs {h['pnl']:<11,.0f} | End Bal: Rs {h['end_bal']:<11,.0f}")

if __name__ == "__main__":
    project_to_100_lots()
