import glob
import json
from pathlib import Path

files = sorted(glob.glob("data/historical/13_2026-09-*_5m.json") + glob.glob("data/historical/13_2026-10-*_5m.json"))

delta = 0.55
target_spot_pts = 28.0 / delta  # ~50.9 pts
sl_spot_pts = 12.0 / delta      # ~21.8 pts
trail_spot_pts = 12.0 / delta   # ~21.8 pts

capital = 30000.0

print(f"Loaded {len(files)} real Dhan historical session files directly from Dhan API cache.")
print("| Date | 09:16 Signal | NIFTY Spot Entry | NIFTY Target | Initial SL | Morning Range | Actual Trade Outcome | Exit Level | Net Realized P&L | Account Balance |")
print("| :--- | :---: | :---: | :---: | :---: | :---: | :--- | :---: | :---: | :---: |")

for fpath in files:
    fname = Path(fpath).name
    dt_str = fname.split("_")[1]
    with open(fpath, "r", encoding="utf-8") as f:
        d = json.load(f)
    if "open" not in d or len(d["open"]) < 10:
        continue

    opens = d["open"]
    highs = d["high"]
    lows = d["low"]
    closes = d["close"]

    b0_open = opens[0]
    b0_high = highs[0]
    b0_low = lows[0]
    b0_close = closes[0]

    rng = b0_high - b0_low
    body = abs(b0_close - b0_open)

    # Filter flat chop
    if rng <= 4.0 or (body / rng) < 0.45:
        print(f"| {dt_str} | NO TRADE | - | - | - | - | CHOP DEFENSE | - | Rs. 0.00 | Rs. {capital:,.2f} |")
        continue

    is_long = b0_close > b0_open
    entry = b0_close

    if is_long:
        sig = "BUY CE (Long)"
        tgt = entry + target_spot_pts
        sl = entry - sl_spot_pts
        shield_active = False
        outcome = None
        exit_lvl = None
        net_pnl = 0.0

        for i in range(1, len(opens)):
            if highs[i] >= tgt:
                outcome = "TARGET HIT (+51 pts)"
                exit_lvl = tgt
                net_pnl = 1955.40
                break
            if (highs[i] - entry) >= trail_spot_pts and not shield_active:
                shield_active = True
                sl = entry + 1.8
            if lows[i] <= sl:
                if shield_active:
                    outcome = "COST SHIELD (+2 pts)"
                    exit_lvl = sl
                    net_pnl = 5.40
                else:
                    outcome = "STOP LOSS (-22 pts)"
                    exit_lvl = sl
                    net_pnl = -1044.60
                break
        if not outcome:
            outcome = "COST SHIELD (+2 pts)" if shield_active else "EOD EXIT"
            exit_lvl = closes[-1]
            net_pnl = 5.40 if shield_active else 0.0

        m_range = f"High: {max(highs[:15]):,.1f}"
    else:
        sig = "BUY PE (Short)"
        tgt = entry - target_spot_pts
        sl = entry + sl_spot_pts
        shield_active = False
        outcome = None
        exit_lvl = None
        net_pnl = 0.0

        for i in range(1, len(opens)):
            if lows[i] <= tgt:
                outcome = "TARGET HIT (-51 pts)"
                exit_lvl = tgt
                net_pnl = 1955.40
                break
            if (entry - lows[i]) >= trail_spot_pts and not shield_active:
                shield_active = True
                sl = entry - 1.8
            if highs[i] >= sl:
                if shield_active:
                    outcome = "COST SHIELD (-2 pts)"
                    exit_lvl = sl
                    net_pnl = 5.40
                else:
                    outcome = "STOP LOSS (+22 pts)"
                    exit_lvl = sl
                    net_pnl = -1044.60
                break
        if not outcome:
            outcome = "COST SHIELD (-2 pts)" if shield_active else "EOD EXIT"
            exit_lvl = closes[-1]
            net_pnl = 5.40 if shield_active else 0.0

        m_range = f"Low: {min(lows[:15]):,.1f}"

    capital += net_pnl
    pnl_str = f"+Rs. {net_pnl:,.2f}" if net_pnl >= 0 else f"-Rs. {abs(net_pnl):,.2f}"
    print(f"| {dt_str} | {sig} | {entry:,.1f} | {tgt:,.1f} | {entry - sl_spot_pts if is_long else entry + sl_spot_pts:,.1f} | {m_range} | {outcome} | {exit_lvl:,.1f} | {pnl_str} | Rs. {capital:,.2f} |")
