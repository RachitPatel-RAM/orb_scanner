import glob
import json
from pathlib import Path
from collections import defaultdict

files = sorted(glob.glob("data/historical/13_*_5m.json"))

delta = 0.55
# Proper Institutional Scalp:
# Risk: 10 opt pts = ~18.2 spot pts (Loss: -Rs. 819.60 net)
# Target: 24 opt pts = ~43.6 spot pts (Gain: +Rs. 1,730.40 net) -> 1:2.4 Risk/Reward!
# Cost Shield: Trail to Cost at +10 opt pts (~18.2 spot pts)
target_spot = 24.0 / delta  # ~43.6 pts
sl_spot = 10.0 / delta      # ~18.2 pts
trail_spot = 10.0 / delta   # ~18.2 pts

monthly = defaultdict(lambda: {"trades": 0, "wins": 0, "shields": 0, "losses": 0, "chops": 0, "net_pnl": 0.0})

for fpath in files:
    dt_str = Path(fpath).name.split("_")[1]
    m_str = dt_str[:7]
    with open(fpath, "r", encoding="utf-8") as f:
        d = json.load(f)
    if "open" not in d or len(d["open"]) < 10:
        continue
    opens, highs, lows, closes = d["open"], d["high"], d["low"], d["close"]
    b0_open, b0_high, b0_low, b0_close = opens[0], highs[0], lows[0], closes[0]

    rng = b0_high - b0_low
    body = abs(b0_close - b0_open)
    if rng <= 4.0 or (body / rng) < 0.45:
        monthly[m_str]["chops"] += 1
        continue

    is_long = b0_close > b0_open
    entry = b0_close
    net_pnl = 0.0
    monthly[m_str]["trades"] += 1

    if is_long:
        tgt = entry + target_spot
        sl = entry - sl_spot
        shield = False
        outcome = None
        for i in range(1, len(opens)):
            if highs[i] >= tgt:
                outcome = "WIN"
                net_pnl = (24.0 * 75) - 69.60  # +Rs. 1,730.40
                monthly[m_str]["wins"] += 1
                break
            if (highs[i] - entry) >= trail_spot and not shield:
                shield = True
                sl = entry + 1.8
            if lows[i] <= sl:
                if shield:
                    outcome = "SHIELD"
                    net_pnl = 5.40
                    monthly[m_str]["shields"] += 1
                else:
                    outcome = "LOSS"
                    net_pnl = (-10.0 * 75) - 69.60  # -Rs. 819.60
                    monthly[m_str]["losses"] += 1
                break
        if not outcome:
            net_pnl = 5.40 if shield else 0.0
            if shield:
                monthly[m_str]["shields"] += 1
    else:
        tgt = entry - target_spot
        sl = entry + sl_spot
        shield = False
        outcome = None
        for i in range(1, len(opens)):
            if lows[i] <= tgt:
                outcome = "WIN"
                net_pnl = (24.0 * 75) - 69.60  # +Rs. 1,730.40
                monthly[m_str]["wins"] += 1
                break
            if (entry - lows[i]) >= trail_spot and not shield:
                shield = True
                sl = entry - 1.8
            if highs[i] >= sl:
                if shield:
                    outcome = "SHIELD"
                    net_pnl = 5.40
                    monthly[m_str]["shields"] += 1
                else:
                    outcome = "LOSS"
                    net_pnl = (-10.0 * 75) - 69.60  # -Rs. 819.60
                    monthly[m_str]["losses"] += 1
                break
        if not outcome:
            net_pnl = 5.40 if shield else 0.0
            if shield:
                monthly[m_str]["shields"] += 1

    monthly[m_str]["net_pnl"] += net_pnl

print("Month   | Trades | Wins | Shields | Losses | Chops | Net P&L (1 Lot) | Win+Shield Rate")
print("-" * 75)
total_pnl = 0.0
for m, st in sorted(monthly.items()):
    w = st["wins"]
    s = st["shields"]
    l = st["losses"]
    tot = st["trades"]
    rate = ((w + s) / max(1, tot)) * 100.0
    pnl = st["net_pnl"]
    total_pnl += pnl
    print(f"{m} | {tot:<6} | {w:<4} | {s:<7} | {l:<6} | {st['chops']:<5} | Rs. {pnl:<12,.2f} | {rate:.1f}%")

print(f"\nTotal Net P&L across all Dhan sessions: Rs. {total_pnl:,.2f}")
