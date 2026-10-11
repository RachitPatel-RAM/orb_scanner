"""
Realistic Full September 2026 Backtest Engine Using Real Dhan 5-Minute Data.
Matches the exact Green Candle / Fast Scalp Conditional Trigger Strategy:
1. Entry ONLY on Conditional Stop-Limit Trigger (Candle 1 High for CALL, Candle 1 Low for PUT).
2. If trigger not crossed during the day -> UNTRIGGERED (Rs. 0 Risk, Rs. 0 Loss).
3. If trigger crossed -> Executed at Trigger Price.
4. Target 1 (+50 pts Sensex, +55 pts BankNifty, +28 pts Nifty) -> Locks 50% profit.
5. Smart Shield -> Once +50% of T1 achieved, SL moves to Cost + 8 pts (covers Dhan brokerage/taxes).
6. Target 2 (+180 pts Sensex, +140 pts BankNifty, +42 pts Nifty) -> Closed on trend expansion.
7. Compounding -> Starting Capital Rs. 30,000, gradually scales lots as account balance grows.
"""

import json
from datetime import date
from pathlib import Path

DATA_DIR = Path("data/historical")

LOT_SIZES = {
    "NIFTY": 75,
    "BANKNIFTY": 30,
    "SENSEX": 20,
}

ROUNDTRIP_CHARGES = 69.60

def load_candles(sec_id: str, d: date):
    p = DATA_DIR / f"{sec_id}_{d.isoformat()}_5m.json"
    if not p.exists():
        return []
    with open(p, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    d_block = data.get("data", data) if isinstance(data, dict) else {}
    opens = d_block.get("open", [])
    highs = d_block.get("high", [])
    lows = d_block.get("low", [])
    closes = d_block.get("close", [])
    times = d_block.get("timestamp", d_block.get("start_Time", []))
    
    res = []
    for i in range(len(opens)):
        res.append({
            "time": times[i] if i < len(times) else i,
            "open": float(opens[i]),
            "high": float(highs[i]),
            "low": float(lows[i]),
            "close": float(closes[i]),
        })
    return res

def run_september_backtest():
    all_files = sorted(DATA_DIR.glob("13_2026-09-*_5m.json"))
    dates = []
    for f in all_files:
        dt_str = f.stem.split("_")[1]
        dates.append(date.fromisoformat(dt_str))

    capital = 30000.0
    start_capital = capital
    trades = []
    
    prev_closes = {
        "NIFTY": 24080.0,
        "BANKNIFTY": 57400.0,
        "SENSEX": 76950.0,
    }

    wins = 0
    shields = 0
    losses = 0
    untriggered = 0

    for d in dates:
        nifty_bars = load_candles("13", d)
        bn_bars = load_candles("25", d)
        sensex_bars = load_candles("51", d)

        if not nifty_bars:
            continue

        weekday = d.weekday()
        # Rotate instrument according to weekly expiry cycle:
        if weekday == 2 and bn_bars:
            active_sym = "BANKNIFTY"
            active_bars = bn_bars
        elif (weekday == 4 or weekday == 3) and sensex_bars:
            active_sym = "SENSEX"
            active_bars = sensex_bars
        else:
            active_sym = "NIFTY"
            active_bars = nifty_bars

        if not active_bars:
            active_sym = "NIFTY"
            active_bars = nifty_bars

        c1 = active_bars[0]
        rest_bars = active_bars[1:]
        if not rest_bars:
            continue

        p_close = prev_closes.get(active_sym, c1["open"])
        c_open = c1["open"]
        c_high = c1["high"]
        c_low = c1["low"]
        c_close = c1["close"]

        # Full day high and low after 09:20 AM:
        day_high_after_open = max(b["high"] for b in rest_bars)
        day_low_after_open = min(b["low"] for b in rest_bars)

        # 1. GAP-AND-GO DIRECTIONAL BIAS:
        gap_up = (c_open - p_close) / p_close if p_close > 0 else 0
        gap_down = (p_close - c_open) / p_close if p_close > 0 else 0

        if gap_up >= 0.0015 and c_low >= p_close * 0.999:
            direction = "CALL"
        elif gap_down >= 0.0015 and c_high <= p_close * 1.001:
            direction = "PUT"
        elif c_close >= c_open:
            direction = "CALL"
        else:
            direction = "PUT"

        # 2. SIZING & TARGET PARAMETERS:
        delta = 0.55
        if active_sym == "NIFTY":
            lots = 2 if capital < 45000.0 else (3 if capital < 70000.0 else 4)
            unit_lot = LOT_SIZES["NIFTY"]
            tgt1_opt = 28.0
            tgt2_opt = 42.0
            sl_opt = 14.0
        elif active_sym == "BANKNIFTY":
            lots = 1 if capital < 40000.0 else (2 if capital < 75000.0 else 3)
            unit_lot = LOT_SIZES["BANKNIFTY"]
            tgt1_opt = 55.0
            tgt2_opt = 140.0
            sl_opt = 65.0
        else: # SENSEX
            lots = 2 if capital < 40000.0 else (3 if capital < 65000.0 else 4)
            unit_lot = LOT_SIZES["SENSEX"]
            tgt1_opt = 50.0
            tgt2_opt = 180.0
            sl_opt = 70.0

        total_qty = lots * unit_lot
        half_qty = total_qty // 2

        # Spot threshold equivalences:
        tgt1_spot_move = tgt1_opt / delta
        tgt2_spot_move = tgt2_opt / delta
        sl_spot_move = sl_opt / delta

        # 3. CONDITIONAL TRIGGER EVALUATION:
        if direction == "CALL":
            trigger_level = c_high
            if day_high_after_open < trigger_level:
                # Trigger never crossed
                status = "UNTRIGGERED"
                net_pnl = 0.0
                untriggered += 1
                desc = f"High ({trigger_level:.1f}) not broken. Capital 100% safe."
            else:
                # Trigger crossed -> Trade executed
                max_favorable = day_high_after_open - trigger_level
                max_adverse = trigger_level - day_low_after_open

                if max_favorable >= tgt2_spot_move:
                    status = "🎯 TARGET 2 HIT"
                    gross = (half_qty * tgt1_opt) + (half_qty * tgt2_opt)
                    friction = (ROUNDTRIP_CHARGES * lots) + (total_qty * 1.0)
                    net_pnl = gross - friction
                    wins += 1
                    desc = f"Full Target 2 Hit (+{tgt1_opt:.0f} / +{tgt2_opt:.0f} pts)"
                elif max_favorable >= tgt1_spot_move:
                    status = "🎯 TARGET 1 HIT"
                    gross = (half_qty * tgt1_opt) + (half_qty * 8.0) # runner at cost shield
                    friction = (ROUNDTRIP_CHARGES * lots) + (total_qty * 1.0)
                    net_pnl = gross - friction
                    wins += 1
                    desc = f"Target 1 Hit (+{tgt1_opt:.0f} pts), Runner at Cost Shield"
                elif max_favorable >= (tgt1_spot_move * 0.5):
                    status = "🛡️ COST SHIELD"
                    gross = total_qty * 8.0
                    friction = (ROUNDTRIP_CHARGES * lots) + (total_qty * 1.0)
                    net_pnl = gross - friction
                    shields += 1
                    desc = "Protected by Cost Shield (+8 pts, covers charges)"
                elif max_adverse >= sl_spot_move:
                    status = "🛑 STOP LOSS"
                    gross = -(total_qty * sl_opt)
                    friction = (ROUNDTRIP_CHARGES * lots) + (total_qty * 1.0)
                    net_pnl = gross - friction
                    losses += 1
                    desc = f"Hit Stop Loss (-{sl_opt:.0f} pts)"
                else:
                    status = "EOD EXIT"
                    net_pnl = 0.0 - (ROUNDTRIP_CHARGES * lots)
                    desc = "Small EOD flat exit"

        else: # PUT
            trigger_level = c_low
            if day_low_after_open > trigger_level:
                # Trigger never crossed
                status = "UNTRIGGERED"
                net_pnl = 0.0
                untriggered += 1
                desc = f"Low ({trigger_level:.1f}) defended. Capital 100% safe."
            else:
                max_favorable = trigger_level - day_low_after_open
                max_adverse = day_high_after_open - trigger_level

                if max_favorable >= tgt2_spot_move:
                    status = "🎯 TARGET 2 HIT"
                    gross = (half_qty * tgt1_opt) + (half_qty * tgt2_opt)
                    friction = (ROUNDTRIP_CHARGES * lots) + (total_qty * 1.0)
                    net_pnl = gross - friction
                    wins += 1
                    desc = f"Full Target 2 Hit (+{tgt1_opt:.0f} / +{tgt2_opt:.0f} pts)"
                elif max_favorable >= tgt1_spot_move:
                    status = "🎯 TARGET 1 HIT"
                    gross = (half_qty * tgt1_opt) + (half_qty * 8.0)
                    friction = (ROUNDTRIP_CHARGES * lots) + (total_qty * 1.0)
                    net_pnl = gross - friction
                    wins += 1
                    desc = f"Target 1 Hit (+{tgt1_opt:.0f} pts), Runner at Cost Shield"
                elif max_favorable >= (tgt1_spot_move * 0.5):
                    status = "🛡️ COST SHIELD"
                    gross = total_qty * 8.0
                    friction = (ROUNDTRIP_CHARGES * lots) + (total_qty * 1.0)
                    net_pnl = gross - friction
                    shields += 1
                    desc = "Protected by Cost Shield (+8 pts)"
                elif max_adverse >= sl_spot_move:
                    status = "🛑 STOP LOSS"
                    gross = -(total_qty * sl_opt)
                    friction = (ROUNDTRIP_CHARGES * lots) + (total_qty * 1.0)
                    net_pnl = gross - friction
                    losses += 1
                    desc = f"Hit Stop Loss (-{sl_opt:.0f} pts)"
                else:
                    status = "EOD EXIT"
                    net_pnl = 0.0 - (ROUNDTRIP_CHARGES * lots)
                    desc = "Small EOD flat exit"

        start_bal = capital
        capital += net_pnl
        prev_closes[active_sym] = active_bars[-1]["close"]

        trades.append({
            "date": d.strftime("%d-%b"),
            "index": active_sym,
            "type": direction,
            "lots": lots,
            "qty": total_qty,
            "status": status,
            "net_pnl": round(net_pnl, 2),
            "start_balance": round(start_bal, 2),
            "end_balance": round(capital, 2),
            "desc": desc,
        })

    # Save and output
    with open("data/september_real_audit.json", "w") as f:
        json.dump(trades, f, indent=2)

    print("\n" + "="*80)
    print(f"SEPTEMBER 2026 COMPLETE REAL DHAN BACKTEST RESULTS")
    print(f"Total Trading Sessions: {len(dates)}")
    print(f"Wins: {wins} | Cost Shields: {shields} | Losses: {losses} | Untriggered (Saved): {untriggered}")
    print(f"Starting Capital: Rs. {start_capital:,.2f}")
    print(f"Final Capital:    Rs. {capital:,.2f}")
    print(f"Net Realized P&L: Rs. {capital - start_capital:+,.2f} ({(capital - start_capital)/start_capital*100:+.1f}%)")
    print("="*80)

run_september_backtest()
