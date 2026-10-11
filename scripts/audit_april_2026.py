import glob
import json
from pathlib import Path

files = sorted(glob.glob("data/historical/13_2026-04-*_5m.json"))

delta = 0.55
target_opt_pts = 24.0      # +24 opt pts = +Rs. 1,800 gross (+Rs. 1,730.40 net)
target_spot_pts = target_opt_pts / delta   # ~43.6 spot pts

sl_opt_pts = 10.0          # -10 opt pts = -Rs. 750 gross (-Rs. 819.60 net)
sl_spot_pts = sl_opt_pts / delta           # ~18.2 spot pts

trail_opt_pts = 10.0       # Move to Cost Shield at +10 opt pts
trail_spot_pts = trail_opt_pts / delta     # ~18.2 spot pts

lot_size = 75
brokerage = 69.60

capital = 30000.0
trades_count = 0
wins_count = 0
shields_count = 0
losses_count = 0
chops_count = 0

print(f"=== DHAN API REAL HISTORICAL AUDIT: APRIL 2026 ===")
print("| Date | Signal | Spot Entry | Target Level | Stop Loss | Morning Min/Max | Exit Level | Outcome | Net P&L (Rs) | Account Balance |")
print("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :--- | :---: | :---: |")

for fpath in files:
    dt_str = Path(fpath).name.split("_")[1]
    with open(fpath, "r", encoding="utf-8") as f:
        d = json.load(f)
    if "open" not in d or len(d["open"]) < 10:
        print(f"| {dt_str} | - | - | - | - | - | - | MARKET HOLIDAY | Rs. 0.00 | Rs. {capital:,.2f} |")
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

    # Chop filter: require at least 10 pt opening candle range and 48% solid body
    if rng < 10.0 or (body / rng) < 0.48:
        chops_count += 1
        print(f"| {dt_str} | NO TRADE | - | - | - | Range: {rng:.1f} pts | - | CHOP DEFENSE | Rs. 0.00 | Rs. {capital:,.2f} |")
        continue

    is_long = b0_close > b0_open
    entry = b0_close
    trades_count += 1
    net_pnl = 0.0

    if is_long:
        sig = "BUY CE (Long)"
        tgt = entry + target_spot_pts
        sl = entry - sl_spot_pts
        shield_active = False
        outcome = None
        exit_lvl = None

        for i in range(1, len(opens)):
            # Check target
            if highs[i] >= tgt:
                outcome = "TARGET HIT (+44 pts)"
                exit_lvl = tgt
                net_pnl = (target_opt_pts * lot_size) - brokerage
                wins_count += 1
                break
            # Check trail to cost shield
            if (highs[i] - entry) >= trail_spot_pts and not shield_active:
                shield_active = True
                sl = entry + 1.8
            # Check stop
            if lows[i] <= sl:
                if shield_active:
                    outcome = "COST SHIELD (+2 pts)"
                    exit_lvl = sl
                    net_pnl = 5.40
                    shields_count += 1
                else:
                    outcome = "STOP LOSS (-18 pts)"
                    exit_lvl = sl
                    net_pnl = (-sl_opt_pts * lot_size) - brokerage
                    losses_count += 1
                break
        if not outcome:
            outcome = "COST SHIELD" if shield_active else "EOD FLAT"
            exit_lvl = closes[-1]
            net_pnl = 5.40 if shield_active else 0.0
            if shield_active:
                shields_count += 1

        morning_stat = f"High: {max(highs[:12]):,.1f}"
    else:
        sig = "BUY PE (Short)"
        tgt = entry - target_spot_pts
        sl = entry + sl_spot_pts
        shield_active = False
        outcome = None
        exit_lvl = None

        for i in range(1, len(opens)):
            # Check target
            if lows[i] <= tgt:
                outcome = "TARGET HIT (-44 pts)"
                exit_lvl = tgt
                net_pnl = (target_opt_pts * lot_size) - brokerage
                wins_count += 1
                break
            # Check trail to cost shield
            if (entry - lows[i]) >= trail_spot_pts and not shield_active:
                shield_active = True
                sl = entry - 1.8
            # Check stop
            if highs[i] >= sl:
                if shield_active:
                    outcome = "COST SHIELD (-2 pts)"
                    exit_lvl = sl
                    net_pnl = 5.40
                    shields_count += 1
                else:
                    outcome = "STOP LOSS (+18 pts)"
                    exit_lvl = sl
                    net_pnl = (-sl_opt_pts * lot_size) - brokerage
                    losses_count += 1
                break
        if not outcome:
            outcome = "COST SHIELD" if shield_active else "EOD FLAT"
            exit_lvl = closes[-1]
            net_pnl = 5.40 if shield_active else 0.0
            if shield_active:
                shields_count += 1

        morning_stat = f"Low: {min(lows[:12]):,.1f}"

    capital += net_pnl
    pnl_str = f"+Rs. {net_pnl:,.2f}" if net_pnl >= 0 else f"-Rs. {abs(net_pnl):,.2f}"
    print(f"| {dt_str} | {sig} | {entry:,.1f} | {tgt:,.1f} | {entry-sl_spot_pts if is_long else entry+sl_spot_pts:,.1f} | {morning_stat} | {exit_lvl:,.1f} | {outcome} | {pnl_str} | Rs. {capital:,.2f} |")

print("\n=== SUMMARY APRIL 2026 ===")
print(f"Total Trading Sessions: {len(files) - 2}")
print(f"Total Executed Trades: {trades_count}")
print(f"Target Hits (Wins): {wins_count}")
print(f"Cost Shields (Zero Loss): {shields_count}")
print(f"Stop Losses: {losses_count}")
print(f"Chops Defended (Skipped): {chops_count}")
print(f"Starting Capital: Rs. 30,000.00")
print(f"Ending Capital: Rs. {capital:,.2f}")
print(f"Net Realized P&L: Rs. {capital - 30000.0:,.2f}")
