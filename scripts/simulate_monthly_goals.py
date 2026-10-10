"""
Simulate Monthly Income Goals & Compounding Path to Rs. 1,00,000/Month.
Analyzes exact capital required, lot sizes, and compounding timeline.
"""

from __future__ import annotations

import json
from scripts.run_10year_real_backtest import fetch_10y_nifty_data

def run_simulation():
    bars = fetch_10y_nifty_data()
    print(f"Loaded {len(bars)} bars.")

    # 1. Base Metrics per Lot
    total_months = 120
    # From 10-year audit of Combined System:
    # 2,014 trades over 120 months = ~16.8 trades/month
    # 1,253 wins (+28 pts), 417 shields (+1 pt), 344 losses (-12 pts)
    # Total net PnL across 10 years for 1 Lot = Rs. 20,93,025.60
    avg_month_1_lot = 2093025.60 / 120.0  # Rs. 17,441.88 per month

    print(f"Average Monthly Net Profit for 1 Lot: Rs. {avg_month_1_lot:,.2f}")

    # Tiers table
    tiers = [
        {"capital": 30000, "lots": 1},
        {"capital": 50000, "lots": 1},
        {"capital": 60000, "lots": 2},
        {"capital": 100000, "lots": 3},
        {"capital": 150000, "lots": 5},
        {"capital": 180000, "lots": 6},
        {"capital": 200000, "lots": 6},
    ]

    print("\n--- FIXED CAPITAL TIERS & EXPECTED MONTHLY INCOME ---")
    for t in tiers:
        exp_income = t["lots"] * avg_month_1_lot
        print(f"Capital: Rs. {t['capital']:<8} | Lots: {t['lots']} | Avg Monthly Net: Rs. {exp_income:,.2f}")

    # 2. Compounding Simulation starting at Rs. 30,000
    # Safe rule: 1 Lot per Rs. 30,000 in account, max 6 lots (target: 1 Lakh/month)
    balance = 30000.0
    records = []
    current_m = None
    m_start_bal = balance
    m_pnl = 0.0

    delta = 0.55
    target_spot = 28.0 / delta
    trail_spot = 12.0 / delta
    sl_spot = 12.0 / delta

    for i in range(1, len(bars)):
        cur = bars[i]
        prev = bars[i-1]
        d = cur['date']
        m_key = f"{d.year}-{d.month:02d}"

        if current_m is None:
            current_m = m_key
            m_start_bal = balance

        if m_key != current_m:
            records.append({
                "month": current_m,
                "start": m_start_bal,
                "lots": max(1, min(6, int(m_start_bal // 30000))),
                "pnl": m_pnl,
                "end": balance,
            })
            current_m = m_key
            m_start_bal = balance
            m_pnl = 0.0

        pdh = prev['high']
        pdl = prev['low']
        pdc = prev['close']
        topen = cur['open']
        dhigh = cur['high']
        dlow = cur['low']
        gap_pct = (topen - pdc) / pdc * 100.0

        direction = None
        if gap_pct >= 0.25 and topen >= pdh * 0.999:
            direction = 'CALL'
        elif gap_pct <= -0.25 and topen <= pdl * 1.001:
            direction = 'PUT'
        elif abs(gap_pct) < 0.25:
            rng_mid = (pdh + pdl) / 2.0
            if dhigh > pdh and topen >= rng_mid:
                direction = 'CALL'
            elif dlow < pdl and topen <= rng_mid:
                direction = 'PUT'
            elif dhigh >= pdh * 1.002:
                direction = 'CALL'
            elif dlow <= pdl * 0.998:
                direction = 'PUT'
            else:
                continue
        else:
            if gap_pct > 0 and dhigh >= pdh * 1.002:
                direction = 'CALL'
            elif gap_pct < 0 and dlow <= pdl * 0.998:
                direction = 'PUT'
            else:
                continue

        lots = max(1, min(6, int(balance // 30000)))
        dhan_fees = 69.60 * lots
        lot_size = 75

        if direction == 'CALL':
            fav = dhigh - topen
            adv = topen - dlow
        else:
            fav = topen - dlow
            adv = dhigh - topen

        if fav >= target_spot:
            trade_pnl = (27.0 * lot_size * lots) - dhan_fees
        elif fav >= trail_spot:
            trade_pnl = (1.0 * lot_size * lots) - dhan_fees
        elif adv >= sl_spot:
            trade_pnl = (-13.0 * lot_size * lots) - dhan_fees
        else:
            trade_pnl = (1.0 * lot_size * lots) - dhan_fees

        balance += trade_pnl
        m_pnl += trade_pnl

    print("\n--- COMPOUNDING JOURNEY FROM RS. 30,000 (FIRST 12 MONTHS) ---")
    for r in records[:12]:
        print(f"Month: {r['month']} | Start: Rs. {r['start']:<9,.0f} | Lots: {r['lots']} | Net PnL: Rs. {r['pnl']:<9,.0f} | End Bal: Rs. {r['end']:<9,.0f}")

if __name__ == "__main__":
    run_simulation()
