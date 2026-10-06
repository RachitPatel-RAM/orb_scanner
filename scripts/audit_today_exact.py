"""
Exact Audit of Today's (06-Oct-2026) Indian Market Session:
Rules:
1. 15-minute timeframe.
2. Candle 1 (09:15 - 09:30): IGNORED.
3. Candle 2 (09:30 - 09:45) + Candle 3 (09:45 - 10:00): Define Range High & Range Low.
4. From 10:00:00 onwards:
   Check candles (Candle 4: 10:00-10:15, Candle 5: 10:15-10:30, etc.)
   When candle CLOSES outside Range:
   - Close > Range High -> LONG (BUY / CALL)
   - Close < Range Low  -> SHORT (SELL / PUT)
   Alert triggers IMMEDIATELY at candle close / next candle open.
5. Exit: Target (1:2 R:R), Stop Loss (Range Mid), or EOD at 15:30.
6. 1 Lot PnL calculated using official F&O lot sizes.
"""

import asyncio
from datetime import date, datetime, timezone
import json
import sys
from pathlib import Path
import zoneinfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import httpx
from app.config import logger
from app.dhan.instruments import instrument_manager

IST_TZ = zoneinfo.ZoneInfo("Asia/Kolkata")
AUDIT_DATE = date(2026, 10, 6)

async def fetch_15m_candles(client: httpx.AsyncClient, yf_symbol: str):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_symbol}?interval=15m&range=5d"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            return []
        data = resp.json()
        result = data.get("chart", {}).get("result")
        if not result:
            return []
        res = result[0]
        timestamps = res.get("timestamp", [])
        quote = res.get("indicators", {}).get("quote", [{}])[0]
        opens = quote.get("open", [])
        highs = quote.get("high", [])
        lows = quote.get("low", [])
        closes = quote.get("close", [])
        volumes = quote.get("volume", [])

        candles = []
        for i in range(len(timestamps)):
            if (
                opens[i] is None or highs[i] is None or lows[i] is None or closes[i] is None
            ):
                continue
            dt = datetime.fromtimestamp(timestamps[i], tz=timezone.utc).astimezone(IST_TZ)
            if dt.date() == AUDIT_DATE:
                candles.append({
                    "time": dt,
                    "time_str": dt.strftime("%H:%M"),
                    "open": float(opens[i]),
                    "high": float(highs[i]),
                    "low": float(lows[i]),
                    "close": float(closes[i]),
                    "volume": float(volumes[i] or 0),
                })
        return candles
    except Exception as e:
        return []

def evaluate_stock_or_index(symbol: str, name: str, is_index: bool, lot_size: int, candles: list):
    if len(candles) < 4:
        return None

    # Candle 0: 09:15 - 09:30 (IGNORED)
    # Candle 1: 09:30 - 09:45
    # Candle 2: 09:45 - 10:00
    c1 = candles[1]
    c2 = candles[2]

    range_high = max(c1["high"], c2["high"])
    range_low = min(c1["low"], c2["low"])
    range_mid = round((range_high + range_low) / 2.0, 2)
    range_pts = round(range_high - range_low, 2)

    # From Candle 3 onwards (10:00 - 10:15, 10:15 - 10:30, ...)
    for idx in range(3, len(candles)):
        c = candles[idx]
        close_p = c["close"]

        # Alert trigger time is the close of this candle (i.e., +15 mins = next candle start)
        trigger_time = candles[idx + 1]["time_str"] if idx + 1 < len(candles) else "15:30"
        candle_closed_at = c["time_str"]

        direction = None
        if close_p > range_high:
            direction = "LONG"
        elif close_p < range_low:
            direction = "SHORT"

        if direction:
            entry_p = close_p
            stop_loss = range_mid
            risk = abs(entry_p - stop_loss)
            if direction == "LONG":
                target = round(entry_p + 2.0 * risk, 2)
            else:
                target = round(entry_p - 2.0 * risk, 2)

            # Check forward simulation through subsequent candles
            result = "EOD"
            exit_p = candles[-1]["close"]
            exit_time = candles[-1]["time_str"]

            for f_idx in range(idx + 1, len(candles)):
                fc = candles[f_idx]
                if direction == "LONG":
                    if fc["low"] <= stop_loss:
                        result = "STOP_LOSS"
                        exit_p = stop_loss
                        exit_time = fc["time_str"]
                        break
                    elif fc["high"] >= target:
                        result = "TARGET"
                        exit_p = target
                        exit_time = fc["time_str"]
                        break
                else:
                    if fc["high"] >= stop_loss:
                        result = "STOP_LOSS"
                        exit_p = stop_loss
                        exit_time = fc["time_str"]
                        break
                    elif fc["low"] <= target:
                        result = "TARGET"
                        exit_p = target
                        exit_time = fc["time_str"]
                        break

            pts = (exit_p - entry_p) if direction == "LONG" else (entry_p - exit_p)
            pct = (pts / entry_p) * 100.0

            if is_index:
                # Option 0.5 delta estimate for Index options
                pnl = round(pts * 0.5 * lot_size, 2)
                action_str = f"BUY {symbol} CALL" if direction == "LONG" else f"BUY {symbol} PUT"
            else:
                # Stock 1 Lot Futures / Options move
                pnl = round(pts * lot_size, 2)
                action_str = f"BUY / CALL (Long)" if direction == "LONG" else f"SELL / PUT (Short)"

            return {
                "symbol": symbol,
                "name": name,
                "is_index": is_index,
                "lot_size": lot_size,
                "direction": direction,
                "action": action_str,
                "breakout_candle": c["time_str"],
                "alert_time": trigger_time,
                "range_high": range_high,
                "range_low": range_low,
                "range_mid": range_mid,
                "range_pts": range_pts,
                "entry_price": entry_p,
                "stop_loss": stop_loss,
                "target": target,
                "exit_price": exit_p,
                "exit_time": exit_time,
                "result": result,
                "pts": round(pts, 2),
                "pct": round(pct, 2),
                "pnl": pnl,
            }
    return None

async def main():
    instrument_manager.load_and_parse()
    universe = instrument_manager.resolve_universe()

    indices = [
        ("%5ENSEI", "NIFTY", "Nifty 50 Index", True, 65),
        ("%5ENSEBANK", "BANKNIFTY", "Bank Nifty Index", True, 30),
    ]

    print(f"Auditing Indian Market for {AUDIT_DATE.isoformat()}...")
    print(f"Total Universe Equities: {len(universe)} stocks + Indices.")

    sem = asyncio.Semaphore(10)
    all_results = []

    async with httpx.AsyncClient(timeout=15.0) as client:
        # 1. Audit Indices
        for yf_sym, sym, name, is_idx, lot_sz in indices:
            candles = await fetch_15m_candles(client, yf_sym)
            res = evaluate_stock_or_index(sym, name, is_idx, lot_sz, candles)
            if res:
                all_results.append(res)

        # 2. Audit Universe Stocks
        async def check_stock(inst):
            async with sem:
                yf_sym = f"{inst.symbol.replace('&', '%26')}.NS"
                candles = await fetch_15m_candles(client, yf_sym)
                lot_sz = instrument_manager.get_lot_size(inst.symbol)
                res = evaluate_stock_or_index(inst.symbol, inst.display_name, False, lot_sz, candles)
                if res:
                    all_results.append(res)

        await asyncio.gather(*[check_stock(inst) for inst in universe])

    # Save to json file for reference
    with open("scripts/audit_results_20261006.json", "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=2)

    print(f"\n========================================================")
    print(f"   INDIAN MARKET BREAKOUT AUDIT REPORT ({AUDIT_DATE.strftime('%d-%b-%Y')})")
    print(f"========================================================")
    print(f"Total Stocks & Indices evaluated: {len(universe) + len(indices)}")
    print(f"Total Confirmed Breakouts Found: {len(all_results)}")

    targets = [r for r in all_results if r["result"] == "TARGET"]
    stops = [r for r in all_results if r["result"] == "STOP_LOSS"]
    eods = [r for r in all_results if r["result"] == "EOD"]

    win_rate = (len(targets) / len(all_results) * 100.0) if all_results else 0.0
    combined_pnl = sum(r["pnl"] for r in all_results)

    print(f"Win Rate: {win_rate:.1f}% ({len(targets)} 🎯 Targets | {len(stops)} 🛑 Stops | {len(eods)} ⏱ EOD)")
    print(f"Combined 1-Lot Return: Rs {combined_pnl:+,.2f}\n")

    # Sort results by PnL descending
    all_results.sort(key=lambda x: x["pnl"], reverse=True)

    for r in all_results:
        icon = "🎯" if r["result"] == "TARGET" else ("🛑" if r["result"] == "STOP_LOSS" else "⏱")
        tag = "[INDEX]" if r["is_index"] else "[STOCK]"
        print(f"{icon} {tag} {r['symbol']:<12} | {r['direction']:<5} | Alert: {r['alert_time']} IST (Candle: {r['breakout_candle']})")
        print(f"   Entry: Rs {r['entry_price']:>9.2f} | SL: Rs {r['stop_loss']:>9.2f} | Tgt: Rs {r['target']:>9.2f} | Exit: Rs {r['exit_price']:>9.2f}")
        print(f"   1 Lot ({r['lot_size']} Qty) PnL: Rs {r['pnl']:>+10.2f} ({r['pct']:>+5.2f}%) ➔ {r['result']}")
        print()

if __name__ == "__main__":
    asyncio.run(main())
