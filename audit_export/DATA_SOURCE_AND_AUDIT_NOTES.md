# Data Source & Methodology Disclosure for Audit

## 1. Input Data Source
- **Underlying Index:** NIFTY 50 (`^NSEI`)
- **API Endpoint:** Yahoo Finance Chart API (`https://query1.finance.yahoo.com/v8/finance/chart/%5ENSEI?interval=1d&range=5y`)
- **Candle Interval:** 1-Day (Daily OHLCV candles).
- **Time Range:** October 2021 to October 2026 (5 calendar years).
- **Sample Raw Data:** Included in `raw_nifty_daily_sample.json`.

---

## 2. Core Model Flaws & Audit Findings (Acknowledged)

1. **Margin Sufficiency & Negative Balance Failure (Fatal Flaw):**
   - The simulation script `simulate_compounding_5year.py` and `export_audit_trade_log.py` used `active_lots = max(1, ...)` without verifying if `current_balance >= contract_margin_required`.
   - On Trade #119 (16-May-2022), available cash was ₹4,367.10, but the modeled option purchase cost was ₹4,750 (₹95 × 50).
   - The simulation booked **323 unaffordable trades** with zero margin, allowing equity to reach negative (−₹30,051.77 on 13-Aug-2024).
   - In real-world broker execution (Dhan), the account would have been locked out due to margin shortfall on Trade #119 in May 2022, causing capital depletion.

2. **Synthetic Options Modeling vs Real Tick Data:**
   - The backtest used daily spot candles and approximated option premiums:
     - Fixed entry premium baseline of ₹95.0.
     - Synthetic delta: 0.55 on Target hits, 0.48 on Stop hits.
     - Synthetic theta deduction: 3.5 pts on regular days, 7.0 pts on expiry days.
   - It did NOT use historical tick-level or 1-minute NSE options orderbook quotes with real implied volatility and dynamic bid-ask spreads.

3. **NSE Regulatory & Statutory Discrepancies:**
   - **Lot Sizes:** Nifty lot size was 50 (July 2021 – April 2024), 25 (from 26 April 2024 under circular FAOP61415), and subsequently revised to 65. The initial script assumed constant 75 units.
   - **STT Hike:** 0.15% sell turnover STT post 1-April-2026 was not modeled in the later trades.

---

## 3. Conclusion & Risk Verdict
- **Live Trading Status:** Strictly **DISABLED**.
- The ₹6,16,879 compounding claim is invalid because it relied on 323 unfunded ghost trades during periods of negative balance.
- Live capital must not be deployed. Any future testing must be confined strictly to Forward Paper Trading (`TRADING_MODE=PAPER`) with real-time Dhan market data and zero real funds.
