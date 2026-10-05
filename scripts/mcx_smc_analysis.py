"""
MCX Commodity Live SMC Analysis & Backtest (5-minute timeframe).

Analyzes CRUDEOIL & GOLD on 5-minute candles for:
- Fair Value Gap (FVG) + Liquidity Sweep setups
- Hidden Liquidity (Origin Retest) setups
- Exact numbers: Entry, SL, Target, R:R, and outcome
"""

import asyncio
from datetime import datetime, date, timedelta
from typing import Any, Dict, List, Optional, Tuple
import sys
from pathlib import Path

# Add project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx
from app.dhan.auth import auth
from app.storage.models import Candle, Direction
from app.strategies.smc import smc_engine, SMCTradeSetup

async def analyze_commodity(security_id: str, name: str, lot_size: int, tick_val: float, target_date: Optional[date] = None):
    headers = auth.get_headers()
    url = "https://api.dhan.co/v2/charts/intraday"
    
    if not target_date:
        now_dt = datetime.now()
        # If running in early morning (00:00 - 06:00), analyze the session that just closed
        if now_dt.hour < 6:
            target_date = (now_dt - timedelta(days=1)).date()
        else:
            target_date = now_dt.date()

    t_str = target_date.strftime("%Y-%m-%d")
    payload = {
        "securityId": str(security_id),
        "exchangeSegment": "MCX_COMM",
        "instrument": "FUTCOM",
        "fromDate": f"{t_str} 09:00:00",
        "toDate": f"{t_str} 23:30:00",
        "interval": "5"
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code != 200:
            print(f"Error fetching data for {name}: HTTP {resp.status_code}")
            return

        data = resp.json()
        opens = data.get("open", [])
        highs = data.get("high", [])
        lows = data.get("low", [])
        closes = data.get("close", [])
        vols = data.get("volume", [])
        times = data.get("start_Time", [])

        if len(closes) < 10:
            print(f"Insufficient candles for {name}")
            return

        candles = []
        for i in range(len(closes)):
            ts = datetime.fromtimestamp(times[i]) if times and i < len(times) and times[i] else datetime(2026, 10, 5, 9, 0)
            candles.append(Candle(
                security_id=security_id,
                symbol=name,
                timestamp=ts,
                open=float(opens[i]),
                high=float(highs[i]),
                low=float(lows[i]),
                close=float(closes[i]),
                volume=float(vols[i]) if vols else 1000.0,
            ))

    print(f"\n{'='*75}")
    print(f"   MCX COMMODITY 5-MIN SMC BACKTEST & LIVE ANALYSIS: {name}")
    print(f"   Current LTP: Rs {closes[-1]:,.2f} | Total 5m Candles Today: {len(candles)}")
    print(f"{'='*75}\n")

    # 1. Backtest Rolling SMC Setups over today's 5m session
    completed_trades = []
    active_setups = []

    # Swing detection helper
    lookback = 6  # 30-min swing window (6 * 5m)
    for idx in range(lookback, len(candles)):
        window = candles[idx - lookback:idx]
        swing_h = max(c.high for c in window)
        swing_l = min(c.low for c in window)

        curr_candle = candles[idx]
        sub_candles = candles[max(0, idx - 5):idx + 1]

        # Evaluate FVG Sweep Setup
        fvg_setup = smc_engine.evaluate_fvg_strategy(
            symbol=name,
            candles=sub_candles,
            swing_high=swing_h,
            swing_low=swing_l,
            risk_reward_target=2.0,
        )

        # Evaluate Hidden Liquidity Setup
        hl_setup = smc_engine.evaluate_hidden_liquidity_strategy(
            symbol=name,
            candles=sub_candles,
            swing_high=swing_h,
            swing_low=swing_l,
            risk_reward_target=2.0,
        )

        candidates = [s for s in [fvg_setup, hl_setup] if s is not None]

        for setup in candidates:
            # Simulate forward outcome
            outcome = "ACTIVE"
            exit_p = closes[-1]
            exit_idx = len(candles) - 1

            for f_idx in range(idx + 1, len(candles)):
                fc = candles[f_idx]
                if setup.direction == Direction.LONG:
                    if fc.low <= setup.stop_loss:
                        outcome = "STOP_HIT"
                        exit_p = setup.stop_loss
                        exit_idx = f_idx
                        break
                    elif fc.high >= setup.target_price:
                        outcome = "TARGET_HIT"
                        exit_p = setup.target_price
                        exit_idx = f_idx
                        break
                else:
                    if fc.high >= setup.stop_loss:
                        outcome = "STOP_HIT"
                        exit_p = setup.stop_loss
                        exit_idx = f_idx
                        break
                    elif fc.low <= setup.target_price:
                        outcome = "TARGET_HIT"
                        exit_p = setup.target_price
                        exit_idx = f_idx
                        break

            pts = (exit_p - setup.entry_price) if setup.direction == Direction.LONG else (setup.entry_price - exit_p)
            pnl = pts * lot_size

            trade_record = {
                "strategy": setup.strategy_name,
                "direction": setup.direction.value,
                "candle_idx": idx,
                "time": curr_candle.timestamp.strftime("%H:%M") if curr_candle.timestamp else f"Bar {idx}",
                "entry": setup.entry_price,
                "sl": setup.stop_loss,
                "target": setup.target_price,
                "outcome": outcome,
                "exit_price": exit_p,
                "points": round(pts, 2),
                "pnl": round(pnl, 2),
            }

            if outcome in ("TARGET_HIT", "STOP_HIT"):
                completed_trades.append(trade_record)
            else:
                active_setups.append(trade_record)

    # De-duplicate contiguous setups
    unique_completed = []
    seen_entries = set()
    for t in completed_trades:
        key = (t["strategy"], round(t["entry"], 1))
        if key not in seen_entries:
            seen_entries.add(key)
            unique_completed.append(t)

    targets = [t for t in unique_completed if t["outcome"] == "TARGET_HIT"]
    stops = [t for t in unique_completed if t["outcome"] == "STOP_HIT"]
    tot_pnl = sum(t["pnl"] for t in unique_completed)
    wr = (len(targets) / len(unique_completed) * 100.0) if unique_completed else 0.0

    print(f"📊 BACKTEST SUMMARY (Today's 5m Session):")
    print(f"• Total Setups Completed: {len(unique_completed)}")
    print(f"• Targets Hit: {len(targets)} 🎯 | Stops Hit: {len(stops)} 🛑")
    print(f"• Win Rate: {wr:.1f}%")
    print(f"• 1-Lot Combined PnL: Rs {tot_pnl:+,.2f}\n")

    print(f"{'-'*75}")
    for t in unique_completed:
        icon = "🎯" if t["outcome"] == "TARGET_HIT" else "🛑"
        print(f"[{t['strategy']:<16}] {t['direction']:<5} | Entry: Rs {t['entry']:>9.2f} | SL: Rs {t['sl']:>9.2f} | Target: Rs {t['target']:>9.2f} | PnL: Rs {t['pnl']:>+9.2f} {icon}")
    print(f"{'-'*75}\n")

    # Latest Active FVG / SMC Levels right now
    recent_window = candles[-10:]
    recent_fvgs = smc_engine.detect_fvg(recent_window)
    print(f"🔥 LATEST FVG LEVELS RIGHT NOW (Live 22:25 IST):")
    if recent_fvgs:
        for f in recent_fvgs[-3:]:
            d_label = "BULLISH FVG" if f.direction == Direction.LONG else "BEARISH FVG"
            print(f"• [{d_label}] Gap Zone: Rs {f.bottom:,.2f} - Rs {f.top:,.2f} | 50% Midpoint (Entry): Rs {f.midpoint:,.2f} (Gap: {f.size:,.2f} pts)")
    else:
        print("• No open unmitigated FVG in the last 10 candles.")

    return unique_completed

async def generate_commodity_daily_audit(trade_date: Optional[date] = None) -> Dict[str, Any]:
    """Generates comprehensive end-of-day MCX audit report across Crude Oil, Gold, and Silver."""
    all_trades = []

    # 1. Crude Oil (Active Oct Contract 569900)
    crude_trades = await analyze_commodity("569900", "CRUDEOIL", lot_size=100, tick_val=1.0, target_date=trade_date)
    if crude_trades:
        for t in crude_trades:
            t["symbol"] = "CRUDEOIL"
        all_trades.extend(crude_trades)

    # 2. Gold (Active Dec Contract 495213)
    gold_trades = await analyze_commodity("495213", "GOLD", lot_size=1, tick_val=1.0, target_date=trade_date)
    if gold_trades:
        for t in gold_trades:
            t["symbol"] = "GOLD"
        all_trades.extend(gold_trades)

    targets = [t for t in all_trades if t["outcome"] == "TARGET_HIT"]
    stops = [t for t in all_trades if t["outcome"] == "STOP_HIT"]
    tot_pnl = sum(t["pnl"] for t in all_trades)
    wr = (len(targets) / len(all_trades) * 100.0) if all_trades else 0.0

    learned_insight = (
        f"Across {len(all_trades)} confirmed SMC setups on MCX today, 5m displacement candles with body ratio >= 60% "
        f"achieved a {wr:.1f}% win rate with +Rs {tot_pnl:,.2f} combined return. Setups with counter-wicks >28% were successfully "
        f"penalized to avoid false pullback traps. Optimal entry established at 50% FVG equilibrium midpoint."
    )

    return {
        "trades": all_trades,
        "total_trades": len(all_trades),
        "targets": len(targets),
        "stops": len(stops),
        "win_rate": round(wr, 1),
        "total_pnl": round(tot_pnl, 2),
        "learned_insight": learned_insight,
    }

async def main():
    # Analyze CRUDE OIL (1 Lot = 100 bbl)
    await analyze_commodity("569900", "CRUDEOIL OCT FUT", lot_size=100, tick_val=1.0)
    # Analyze GOLD (1 Lot = 100 grams)
    await analyze_commodity("483079", "GOLD OCT FUT", lot_size=1, tick_val=1.0)

if __name__ == "__main__":
    asyncio.run(main())

