"""
Simulate User Exact Plan:
- Month 1: Paper trade (0 risk)
- Month 2: Rs. 30,000 real (1 lot)
- Month 3: Top-up to Rs. 60,000 (2 lots)
- Month 4: Top-up to Rs. 1,00,000 (3 lots)
- End of Month 4: Withdraw ALL personal principal invested (House Money Mode)
- Month 5 - 12: Compounding on 100% market profits to reach Rs. 1,00,000/month
"""

def main():
    avg_1lot = 17440.0

    # User deposits
    # M2 deposit = 30000
    # M2 end = 30000 + 17440 = 47440
    # M3 top-up = 60000 - 47440 = 12560
    # M3 end = 60000 + (2 * 17440) = 60000 + 34880 = 94880
    # M4 top-up = 100000 - 94880 = 5120
    # M4 end = 100000 + (3 * 17440) = 100000 + 52320 = 152320
    # Total personal money invested = 30000 + 12560 + 5120 = 47680

    total_personal_invested = 30000 + 12560 + 5120
    
    # End of M4 withdrawal of ALL personal money
    m4_post_withdraw = 152320 - total_personal_invested  # Rs. 104,640

    plan = [
        {"m": 1, "action": "Paper Trade Verification", "deposit": 0, "lots": 0, "start": 0, "pnl": 0, "end": 0, "withdrawn": 0},
        {"m": 2, "action": "Live Launch (1 Lot)", "deposit": 30000, "lots": 1, "start": 30000, "pnl": 17440, "end": 47440, "withdrawn": 0},
        {"m": 3, "action": "Top-up to Rs. 60k (2 Lots)", "deposit": 12560, "lots": 2, "start": 60000, "pnl": 34880, "end": 94880, "withdrawn": 0},
        {"m": 4, "action": "Top-up to Rs. 1.0L (3 Lots)", "deposit": 5120, "lots": 3, "start": 100000, "pnl": 52320, "end": 152320, "withdrawn": 47680},
    ]

    current_bal = m4_post_withdraw
    for m in range(5, 13):
        # 1 lot per 30k, cap at 6 lots (target: 1 Lakh/month)
        lots = min(6, int(current_bal // 30000))
        pnl = lots * avg_1lot
        start_bal = current_bal
        current_bal += pnl
        plan.append({
            "m": m,
            "action": f"House Money ({lots} Lots)",
            "deposit": 0,
            "lots": lots,
            "start": start_bal,
            "pnl": pnl,
            "end": current_bal,
            "withdrawn": 0,
        })

    print(f"Total Personal Capital Put In: Rs. {total_personal_invested:,.2f}")
    print(f"Total Personal Capital Withdrawn at Month 4: Rs. {total_personal_invested:,.2f}")
    print("Net Personal Money at Risk from Month 5 onwards: Rs. 0.00 (Zero Risk!)\n")

    print(f"{'Month':<6} | {'Stage / Strategy Action':<28} | {'Lots':<4} | {'Start Bal (Rs)':<14} | {'Monthly Net P&L':<15} | {'Ending Bal (Rs)':<15}")
    print("-" * 95)
    for p in plan:
        print(f"M{p['m']:02d}   | {p['action']:<28} | {p['lots']:<4} | Rs. {p['start']:<10,.0f} | +Rs. {p['pnl']:<11,.0f} | Rs. {p['end']:<11,.0f}")

    print("\n--- AT END OF 1 YEAR (MONTH 12) ---")
    print(f"Final Trading Account Balance: Rs. {current_bal:,.2f} (100% Pure Market Profits)")
    print(f"Your Personal Bank Account: Full Rs. {total_personal_invested:,.2f} returned safely in Month 4")
    print(f"Monthly Passive Income Capacity: Rs. {6 * avg_1lot:,.2f} per month (6 Lots)")

if __name__ == "__main__":
    main()
