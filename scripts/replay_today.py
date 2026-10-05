"""
Replays today's session (05-Oct-2026) across Universe + Major Indices to find:
- Which stocks and indices broke out today
- What time the breakout occurred
- Entry price, Exit price, Stop Loss, Target
- Target Hit (🎯) vs Stop Loss (🛑) vs EOD Square-Off (⏱)
- Percentage move and 1-Lot PnL for every single setup
"""

import asyncio
from datetime import date, time
import os
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import httpx
from app.config import logger, settings
from app.dhan.auth import auth
from app.dhan.instruments import instrument_manager
from app.market.session import default_session
from app.storage.models import Direction

async def replay_session(trade_date: date):
    instrument_manager.load_and_parse()
    universe = instrument_manager.resolve_universe()
    headers = auth.get_headers()
    url = "https://api.dhan.co/v2/charts/intraday"

    print(f"\n========================================================")
    print(f"   FULL ORB REPLAY & PERFORMANCE AUDIT: {trade_date.isoformat()}")
    print(f"========================================================\n")

    # 1. Audit Major Indices (NIFTY, BANKNIFTY, SENSEX)
    indices = [
        ("13", "NIFTY", "Nifty 50", "NSE", "IDX_I", 65, 50),
        ("25", "BANKNIFTY", "Nifty Bank", "NSE", "IDX_I", 30, 100),
        ("51", "SENSEX", "Sensex", "BSE", "IDX_I", 20, 100),
    ]

    all_breakouts = []

    async with httpx.AsyncClient(timeout=10.0) as client:
        # Check Indices first
        for sid, sym, name, exch, seg, lot_sz, strike_step in indices:
            payload = {
                "securityId": sid,
                "exchangeSegment": seg,
                "instrument": "INDEX",
                "fromDate": f"{trade_date.isoformat()} 09:15:00",
                "toDate": f"{trade_date.isoformat()} 15:30:00",
                "interval": "15",
            }
            try:
                resp = await client.post(url, headers=headers, json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    highs = data.get("high", [])
                    lows = data.get("low", [])
                    closes = data.get("close", [])
                    opens = data.get("open", [])
                    ts = data.get("start_Time", [])
                    if len(highs) >= 3:
                        orb_high = max(highs[1], highs[2])
                        orb_low = min(lows[1], lows[2])
                        orb_mid = round((orb_high + orb_low) / 2.0, 2)

                        # Check for breakout in subsequent candles
                        broken = False
                        for idx in range(3, len(closes)):
                            c_close = closes[idx]
                            c_high = highs[idx]
                            c_low = lows[idx]

                            if not broken:
                                if c_close > orb_high:
                                    broken = True
                                    direction = Direction.LONG
                                    entry_p = c_close
                                    sl_p = orb_mid
                                    tgt_p = round(entry_p + (entry_p - sl_p) * 2.0, 2)
                                    opt_type = f"BUY {sym} {round(entry_p / strike_step) * strike_step} CE (Call)"
                                elif c_close < orb_low:
                                    broken = True
                                    direction = Direction.SHORT
                                    entry_p = c_close
                                    sl_p = orb_mid
                                    tgt_p = round(entry_p - (sl_p - entry_p) * 2.0, 2)
                                    opt_type = f"BUY {sym} {round(entry_p / strike_step) * strike_step} PE (Put)"

                                if broken:
                                    # Simulate forward movement until target, stop loss, or EOD
                                    result = "EOD"
                                    exit_p = closes[-1]
                                    for f_idx in range(idx + 1, len(closes)):
                                        f_high = highs[f_idx]
                                        f_low = lows[f_idx]
                                        if direction == Direction.LONG:
                                            if f_low <= sl_p:
                                                result = "STOP_LOSS"
                                                exit_p = sl_p
                                                break
                                            elif f_high >= tgt_p:
                                                result = "TARGET"
                                                exit_p = tgt_p
                                                break
                                        else:
                                            if f_high >= sl_p:
                                                result = "STOP_LOSS"
                                                exit_p = sl_p
                                                break
                                            elif f_low <= tgt_p:
                                                result = "TARGET"
                                                exit_p = tgt_p
                                                break

                                    pts = (exit_p - entry_p) if direction == Direction.LONG else (entry_p - exit_p)
                                    pct = (pts / entry_p) * 100.0
                                    # 1-lot option delta estimate (~0.5 delta)
                                    opt_pnl = round(pts * 0.5 * lot_sz, 2)

                                    all_breakouts.append({
                                        "type": "INDEX",
                                        "symbol": sym,
                                        "name": name,
                                        "direction": direction.value,
                                        "entry_price": entry_p,
                                        "exit_price": exit_p,
                                        "orb_high": orb_high,
                                        "orb_low": orb_low,
                                        "stop_loss": sl_p,
                                        "target": tgt_p,
                                        "result": result,
                                        "pts": round(pts, 2),
                                        "pct": round(pct, 2),
                                        "lot_size": lot_sz,
                                        "pnl": opt_pnl,
                                        "opt_recommendation": opt_type,
                                    })
                                    break
            except Exception as e:
                logger.debug(f"Error checking {sym}: {e}")

        # Check Top FNO Stocks (Sampling universe with concurrency)
        sem = asyncio.Semaphore(5)

        async def check_stock(inst):
            async with sem:
                payload = {
                    "securityId": str(inst.security_id),
                    "exchangeSegment": "NSE_EQ",
                    "instrument": "EQUITY",
                    "fromDate": f"{trade_date.isoformat()} 09:15:00",
                    "toDate": f"{trade_date.isoformat()} 15:30:00",
                    "interval": "15",
                }
                try:
                    r = await client.post(url, headers=headers, json=payload)
                    if r.status_code == 200:
                        d = r.json()
                        highs = d.get("high", [])
                        lows = d.get("low", [])
                        closes = d.get("close", [])
                        if len(highs) >= 4:
                            orb_high = max(highs[1], highs[2])
                            orb_low = min(lows[1], lows[2])
                            orb_mid = round((orb_high + orb_low) / 2.0, 2)

                            broken = False
                            for idx in range(3, len(closes)):
                                c_close = closes[idx]
                                if not broken:
                                    if c_close > orb_high:
                                        broken = True
                                        direction = Direction.LONG
                                        entry_p = c_close
                                        sl_p = orb_mid
                                        tgt_p = round(entry_p + (entry_p - sl_p) * 2.0, 2)
                                    elif c_close < orb_low:
                                        broken = True
                                        direction = Direction.SHORT
                                        entry_p = c_close
                                        sl_p = orb_mid
                                        tgt_p = round(entry_p - (sl_p - entry_p) * 2.0, 2)

                                    if broken:
                                        result = "EOD"
                                        exit_p = closes[-1]
                                        for f_idx in range(idx + 1, len(closes)):
                                            f_h = highs[f_idx]
                                            f_l = lows[f_idx]
                                            if direction == Direction.LONG:
                                                if f_l <= sl_p:
                                                    result = "STOP_LOSS"
                                                    exit_p = sl_p
                                                    break
                                                elif f_h >= tgt_p:
                                                    result = "TARGET"
                                                    exit_p = tgt_p
                                                    break
                                            else:
                                                if f_h >= sl_p:
                                                    result = "STOP_LOSS"
                                                    exit_p = sl_p
                                                    break
                                                elif f_l <= tgt_p:
                                                    result = "TARGET"
                                                    exit_p = tgt_p
                                                    break

                                        pts = (exit_p - entry_p) if direction == Direction.LONG else (entry_p - exit_p)
                                        pct = (pts / entry_p) * 100.0
                                        lot = instrument_manager.get_lot_size(inst.symbol)
                                        lot_pnl = round(pts * lot, 2)

                                        all_breakouts.append({
                                            "type": "STOCK",
                                            "symbol": inst.symbol,
                                            "name": inst.display_name,
                                            "direction": direction.value,
                                            "entry_price": entry_p,
                                            "exit_price": exit_p,
                                            "orb_high": orb_high,
                                            "orb_low": orb_low,
                                            "stop_loss": sl_p,
                                            "target": tgt_p,
                                            "result": result,
                                            "pts": round(pts, 2),
                                            "pct": round(pct, 2),
                                            "lot_size": lot,
                                            "pnl": lot_pnl,
                                            "opt_recommendation": f"{direction.value} {inst.symbol}",
                                        })
                                        break
                except Exception as e:
                    pass

        # Check universe stocks
        await asyncio.gather(*[check_stock(inst) for inst in universe])

    print(f"Total Confirmed Breakouts Found Today: {len(all_breakouts)}\n")
    targets = [b for b in all_breakouts if b["result"] == "TARGET"]
    stops = [b for b in all_breakouts if b["result"] == "STOP_LOSS"]
    eods = [b for b in all_breakouts if b["result"] == "EOD"]
    total_pnl = sum(b["pnl"] for b in all_breakouts)

    print(f"Breakdown: {len(targets)} 🎯 TARGETS | {len(stops)} 🛑 STOPS | {len(eods)} ⏱ EOD")
    print(f"Combined 1-Lot Return: Rs {total_pnl:+,.2f}\n")
    print("-" * 75)
    for b in all_breakouts:
        icon = "🎯" if b["result"] == "TARGET" else ("🛑" if b["result"] == "STOP_LOSS" else "⏱")
        print(f"[{b['type']}] {b['symbol']:<12} | {b['direction']:<5} | Entry: Rs {b['entry_price']:>9.2f} | Exit: Rs {b['exit_price']:>9.2f} | PnL: Rs {b['pnl']:>9.2f} ({b['pct']:>+5.2f}%) {icon}")
        if "BUY" in b.get("opt_recommendation", ""):
            print(f"       -> Action: {b['opt_recommendation']}")
    print("-" * 75)

if __name__ == "__main__":
    asyncio.run(replay_session(date(2026, 10, 5)))
