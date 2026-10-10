"""
Detailed 12-Month Calendar Projection (October 10, 2026 to October 10, 2027)
Simulates:
1. Worst-Case Scenario (Choppy market, 45% win rate, ~Rs. 8,485/lot/month)
2. Average-Case Scenario (10-year verified norm, 62.2% win rate, ~Rs. 17,440/lot/month)
3. Best-Case Scenario (Strong trending market, 75% win rate, ~Rs. 22,435/lot/month)

Includes:
- Month 1 Paper Trade (Rs. 0 capital, Rs. 0 risk)
- Month 2 Live launch with Rs. 30,000
- Month 3 Top-up to Rs. 60,000 (2 lots)
- Month 4 Top-up to Rs. 1,00,000 (3 lots)
- End of Month 4 Principal Withdrawal (Personal money returned to bank, 100% House Money onwards)
- Month 5 to 12 Uncapped Compounding (Continues scaling lots without stopping at 6 lots)
"""

def run_dated_simulation():
    calendar_months = [
        {"m": 1, "start_date": "10-Oct-2026", "end_date": "10-Nov-2026", "name": "M01 (Oct-Nov 2026)"},
        {"m": 2, "start_date": "10-Nov-2026", "end_date": "10-Dec-2026", "name": "M02 (Nov-Dec 2026)"},
        {"m": 3, "start_date": "10-Dec-2026", "end_date": "10-Jan-2027", "name": "M03 (Dec-Jan 2027)"},
        {"m": 4, "start_date": "10-Jan-2027", "end_date": "10-Feb-2027", "name": "M04 (Jan-Feb 2027)"},
        {"m": 5, "start_date": "10-Feb-2027", "end_date": "10-Mar-2027", "name": "M05 (Feb-Mar 2027)"},
        {"m": 6, "start_date": "10-Mar-2027", "end_date": "10-Apr-2027", "name": "M06 (Mar-Apr 2027)"},
        {"m": 7, "start_date": "10-Apr-2027", "end_date": "10-May-2027", "name": "M07 (Apr-May 2027)"},
        {"m": 8, "start_date": "10-May-2027", "end_date": "10-Jun-2027", "name": "M08 (May-Jun 2027)"},
        {"m": 9, "start_date": "10-Jun-2027", "end_date": "10-Jul-2027", "name": "M09 (Jun-Jul 2027)"},
        {"m": 10, "start_date": "10-Jul-2027", "end_date": "10-Aug-2027", "name": "M10 (Jul-Aug 2027)"},
        {"m": 11, "start_date": "10-Aug-2027", "end_date": "10-Sep-2027", "name": "M11 (Aug-Sep 2027)"},
        {"m": 12, "start_date": "10-Sep-2027", "end_date": "10-Oct-2027", "name": "M12 (Sep-Oct 2027)"},
    ]

    scenarios = {
        "worst": {"name": "Worst-Case Scenario (Choppy, ~45% Win)", "per_lot_month": 8485.0},
        "average": {"name": "Average-Case Scenario (10Y Norm, ~62% Win)", "per_lot_month": 17440.0},
        "best": {"name": "Best-Case Scenario (Trending, ~75% Win)", "per_lot_month": 22435.0},
    }

    results = {}

    for key, sc in scenarios.items():
        rate = sc["per_lot_month"]
        table = []
        personal_invested = 0.0
        current_bal = 0.0

        for cm in calendar_months:
            m_num = cm["m"]
            period = f"{cm['start_date']} to {cm['end_date']}"

            if m_num == 1:
                # Paper trading
                table.append({
                    "month": m_num,
                    "period": period,
                    "action": "Paper Trade Verification",
                    "lots": 0,
                    "start_bal": 0.0,
                    "pnl": 0.0,
                    "end_bal": 0.0,
                    "user_topup": 0.0,
                    "withdrawn": 0.0,
                })
            elif m_num == 2:
                # Live launch with Rs 30,000
                user_in = 30000.0
                personal_invested += user_in
                start = 30000.0
                pnl = 1 * rate
                end = start + pnl
                current_bal = end
                table.append({
                    "month": m_num,
                    "period": period,
                    "action": "Live Launch (1 Lot)",
                    "lots": 1,
                    "start_bal": start,
                    "pnl": pnl,
                    "end_bal": end,
                    "user_topup": user_in,
                    "withdrawn": 0.0,
                })
            elif m_num == 3:
                # Top up to Rs 60,000
                topup_needed = max(0.0, 60000.0 - current_bal)
                personal_invested += topup_needed
                start = 60000.0
                pnl = 2 * rate
                end = start + pnl
                current_bal = end
                table.append({
                    "month": m_num,
                    "period": period,
                    "action": "Top-up to Rs. 60k (2 Lots)",
                    "lots": 2,
                    "start_bal": start,
                    "pnl": pnl,
                    "end_bal": end,
                    "user_topup": topup_needed,
                    "withdrawn": 0.0,
                })
            elif m_num == 4:
                # Top up to Rs 1,00,000
                topup_needed = max(0.0, 100000.0 - current_bal)
                personal_invested += topup_needed
                start = 100000.0
                pnl = 3 * rate
                end_pre_withdraw = start + pnl
                
                # Withdraw ALL personal capital invested!
                withdrawn = personal_invested
                end = end_pre_withdraw - withdrawn
                current_bal = end

                table.append({
                    "month": m_num,
                    "period": period,
                    "action": f"Top-up to Rs. 1L -> Withdraw Principal!",
                    "lots": 3,
                    "start_bal": start,
                    "pnl": pnl,
                    "end_bal": end,
                    "user_topup": topup_needed,
                    "withdrawn": withdrawn,
                })
            else:
                # Month 5 to 12: Continuous Uncapped Compounding (1 lot per Rs. 32,000 balance)
                lots = max(1, int(current_bal // 32000))
                start = current_bal
                pnl = lots * rate
                end = start + pnl
                current_bal = end
                table.append({
                    "month": m_num,
                    "period": period,
                    "action": f"Compounding House Money ({lots} Lots)",
                    "lots": lots,
                    "start_bal": start,
                    "pnl": pnl,
                    "end_bal": end,
                    "user_topup": 0.0,
                    "withdrawn": 0.0,
                })

        results[key] = {
            "name": sc["name"],
            "total_personal_invested": personal_invested,
            "total_personal_withdrawn": personal_invested,
            "final_balance": current_bal,
            "table": table,
        }

    return results

if __name__ == "__main__":
    res = run_dated_simulation()
    for k, v in res.items():
        print("="*90)
        print(f"{v['name'].upper()}")
        print(f"Personal Invested: Rs. {v['total_personal_invested']:,.2f} | Withdrawn at Month 4: Rs. {v['total_personal_withdrawn']:,.2f}")
        print(f"Final Account Balance at Month 12: Rs. {v['final_balance']:,.2f}")
        print("-"*90)
        print(f"{'Month':<5} | {'Date Period':<24} | {'Lots':<4} | {'Start Bal (Rs)':<14} | {'Monthly P&L (Rs)':<16} | {'Ending Bal (Rs)':<14}")
        print("-"*90)
        for r in v["table"]:
            print(f"M{r['month']:02d}   | {r['period']:<24} | {r['lots']:<4} | Rs. {r['start_bal']:<10,.0f} | +Rs. {r['pnl']:<12,.0f} | Rs. {r['end_bal']:<10,.0f}")
