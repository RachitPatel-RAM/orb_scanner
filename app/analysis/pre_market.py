"""
Pre-Market 09:08 AM Discovery & CPR Regime Engine.

Automates:
1. 09:08 AM Pre-Open Auction Discovery (Discovered Price, Gap Points, Gap %)
2. Daily Traditional Pivots & CPR (TC, Pivot, BC, CPR Width %)
3. Market Regime Classification:
   - NARROW CPR (< 0.20%): Trending Expansion -> Activate 5M Solid ORB Breakout Engine
   - AVERAGE CPR (0.20% - 0.35%): Normal Day -> Trend-Aligned ORB
   - WIDE CPR (> 0.35%): Choppy Rangebound -> Activate SMC Liquidity Sweep & Reversal Engine
4. Pre-Market 09:10 AM Digest Telegram Broadcast
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any, Dict, List, Optional, Tuple
import httpx

from app.analysis.pivot_points import calculate_traditional_pivots, TraditionalPivotPoints
from app.config import logger, settings
from app.dhan.auth import auth
from app.market.session import default_session, IST_TZ
from app.notifications.telegram import notifier
from app.storage.database import db


@dataclass
class PreMarketSnapshot:
    trade_date: date
    symbol: str
    security_id: str
    prev_close: float
    prev_high: float
    prev_low: float
    pre_open_price: float
    gap_points: float
    gap_pct: float
    gap_type: str        # GAP_UP, GAP_DOWN, FLAT
    pivot: float
    bc: float
    tc: float
    cpr_width_pct: float
    regime: str          # NARROW_TREND, AVERAGE_NORMAL, WIDE_CHOP
    recommended_strategy: str  # ORB_BREAKOUT, SMC_SWEEP_REVERSAL


class PreMarketManager:
    """Manages pre-market 09:08 AM discovery, CPR calculation, and regime routing."""

    def __init__(self):
        self._daily_snapshots: Dict[Tuple[date, str], PreMarketSnapshot] = {}

    def get_snapshot(self, symbol: str, trade_date: Optional[date] = None) -> Optional[PreMarketSnapshot]:
        d = trade_date or default_session.now().date()
        return self._daily_snapshots.get((d, symbol.upper()))

    async def compute_snapshot(
        self,
        security_id: str,
        symbol: str,
        trade_date: Optional[date] = None,
    ) -> Optional[PreMarketSnapshot]:
        """Calculates CPR, discovers Pre-Open Price, and determines today's market regime."""
        d = trade_date or default_session.now().date()
        key = (d, symbol.upper())
        if key in self._daily_snapshots:
            return self._daily_snapshots[key]

        prev_days = default_session.get_previous_trading_days(reference_date=d, count=1)
        if not prev_days:
            return None
        prev_date = prev_days[0]

        # 1. Fetch prior day HLC via Dhan historical or Yahoo Finance
        prev_high, prev_low, prev_close = 0.0, 0.0, 0.0
        try:
            from app.dhan.historical import historical_manager
            c = await historical_manager.get_daily_candle(security_id, symbol, prev_date)
            if c and c.high > 0 and c.low > 0 and c.close > 0:
                prev_high, prev_low, prev_close = c.high, c.low, c.close
        except Exception as e:
            logger.debug(f"Pre-market Dhan candle fetch note for {symbol}: {e}")

        if prev_close <= 0:
            ticker = f"^NSEI" if symbol == "NIFTY" else ("^NSEBANK" if symbol == "BANKNIFTY" else "^BSESN")
            try:
                url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=5d"
                headers = {"User-Agent": "Mozilla/5.0"}
                async with httpx.AsyncClient(timeout=6.0) as client:
                    resp = await client.get(url, headers=headers)
                if resp.status_code == 200:
                    res = resp.json().get("chart", {}).get("result", [{}])[0]
                    q = res.get("indicators", {}).get("quote", [{}])[0]
                    h_list = q.get("high", [])
                    l_list = q.get("low", [])
                    c_list = q.get("close", [])
                    if len(h_list) >= 2 and h_list[-2] is not None:
                        prev_high = float(h_list[-2])
                        prev_low = float(l_list[-2])
                        prev_close = float(c_list[-2])
            except Exception as e:
                logger.debug(f"Pre-market Yahoo candle fetch note for {symbol}: {e}")

        if prev_close <= 0:
            logger.warning(f"Could not retrieve prior day HLC for {symbol}. Pre-market skipped.")
            return None

        # 2. Compute CPR (Central Pivot Range)
        pivot = round((prev_high + prev_low + prev_close) / 3.0, 2)
        bc = round((prev_high + prev_low) / 2.0, 2)
        tc = round(2.0 * pivot - bc, 2)
        cpr_width = abs(tc - bc)
        cpr_width_pct = round((cpr_width / pivot) * 100.0, 3)

        # 3. Discover Pre-Open Auction Price (09:08 AM onwards)
        pre_open_price = prev_close
        try:
            headers = auth.get_headers()
            async with httpx.AsyncClient(timeout=5.0) as client:
                r_ltp = await client.post(
                    "https://api.dhan.co/v2/marketfeed/ltp",
                    headers=headers,
                    json={"IDX_I": [int(security_id)]},
                )
            if r_ltp.status_code == 200:
                d_ltp = r_ltp.json().get("data", {}).get("IDX_I", {})
                last_p = float(d_ltp.get(str(security_id), {}).get("last_price", 0.0))
                if last_p > 0:
                    pre_open_price = last_p
        except Exception as e:
            logger.debug(f"Pre-open LTP lookup note for {symbol}: {e}")

        # Gap calculation
        gap_points = round(pre_open_price - prev_close, 2)
        gap_pct = round((gap_points / prev_close) * 100.0, 2)

        if gap_pct >= 0.25:
            gap_type = "GAP_UP"
        elif gap_pct <= -0.25:
            gap_type = "GAP_DOWN"
        else:
            gap_type = "FLAT"

        # Regime & Strategy Selection
        if cpr_width_pct < 0.20:
            regime = "NARROW_TREND"
            recommended_strategy = "ORB_BREAKOUT"
        elif cpr_width_pct <= 0.35:
            regime = "AVERAGE_NORMAL"
            recommended_strategy = "ORB_BREAKOUT"
        else:
            regime = "WIDE_CHOP"
            recommended_strategy = "SMC_SWEEP_REVERSAL"

        snapshot = PreMarketSnapshot(
            trade_date=d,
            symbol=symbol,
            security_id=security_id,
            prev_close=prev_close,
            prev_high=prev_high,
            prev_low=prev_low,
            pre_open_price=pre_open_price,
            gap_points=gap_points,
            gap_pct=gap_pct,
            gap_type=gap_type,
            pivot=pivot,
            bc=bc,
            tc=tc,
            cpr_width_pct=cpr_width_pct,
            regime=regime,
            recommended_strategy=recommended_strategy,
        )

        self._daily_snapshots[key] = snapshot
        logger.info(
            f"[Pre-Market 09:08] {symbol}: PreOpen=₹{pre_open_price:,.2f} ({gap_points:+,.1f} pts, {gap_pct:+.2f}%) | "
            f"CPR Width={cpr_width_pct:.2f}% ({regime}) -> Active Strategy: {recommended_strategy}"
        )
        return snapshot

    async def generate_and_broadcast_digest(self, trade_date: Optional[date] = None) -> Optional[str]:
        """
        Generates and dispatches the 09:10 AM Pre-Market CPR & Sentiment Digest
        to Telegram group and channels.
        """
        d = trade_date or default_session.now().date()
        indices = [
            ("13", "NIFTY"),
            ("25", "BANKNIFTY"),
            ("51", "SENSEX"),
        ]

        snapshots: List[PreMarketSnapshot] = []
        for sid, sym in indices:
            snap = await self.compute_snapshot(sid, sym, d)
            if snap:
                snapshots.append(snap)

        if not snapshots:
            return None

        # Build elegant Telegram Message
        lines = [
            "🌅 <b>PRE-MARKET DAILY BIAS & CPR DIGEST</b> 📊",
            f"📅 <b>Date:</b> {d.strftime('%d-%b-%Y')} | ⏰ <b>Discovery:</b> 09:10 AM IST",
            "━━━━━━━━━━━━━━━━━━━━━",
        ]

        for s in snapshots:
            gap_icon = "🟢" if s.gap_points > 0 else ("🔴" if s.gap_points < 0 else "⚪")
            regime_icon = "🔥" if s.regime == "NARROW_TREND" else ("⚡" if s.regime == "AVERAGE_NORMAL" else "🛡️")
            strat_label = "5M Solid ORB Breakout" if s.recommended_strategy == "ORB_BREAKOUT" else "SMC Sweep & Reclaim Reversals"

            lines.append(f"🎯 <b>{s.symbol}</b> ({gap_icon} {s.gap_type.replace('_', ' ')})")
            lines.append(f"• Pre-Open: ₹{s.pre_open_price:,.2f} ({s.gap_points:+,.1f} pts | {s.gap_pct:+.2f}%)")
            lines.append(f"• Central Pivot (P): ₹{s.pivot:,.2f} | CPR Width: {s.cpr_width_pct:.2f}%")
            lines.append(f"• Market Regime: {regime_icon} <b>{s.regime.replace('_', ' ')}</b>")
            lines.append(f"• Active Strategy: <b>{strat_label}</b>")
            lines.append("")

        lines.append("━━━━━━━━━━━━━━━━━━━━━")
        lines.append("💡 <i>Strategy Execution Policy:</i>")
        lines.append("• <b>Narrow CPR (<0.20%):</b> High momentum trend expected. ORB Breakouts active.")
        lines.append("• <b>Wide CPR (>0.35%):</b> Choppy consolidation expected. Breakouts suppressed; Mean Reversion sweeps active.")
        lines.append("💎 Live alerts will trigger automatically with 1-click execution!")

        msg_text = "\n".join(lines)
        idemp = f"PREMARKET_{d.isoformat()}"
        await notifier.send_message(msg_text, idempotency_key=idemp)
        logger.info(f"Broadcasted Pre-Market 09:10 AM Digest to Telegram.")
        return msg_text


pre_market_manager = PreMarketManager()
