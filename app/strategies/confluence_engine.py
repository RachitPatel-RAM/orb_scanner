"""
Institutional Multi-Confluence Engine:
Integrates Traditional Pivot Points and Dhan Option Chain Open Interest (OI) Profile
to filter out support/resistance traps (e.g., selling into S1 Support or buying into R1 Resistance).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple
import httpx

from app.config import logger, settings
from app.dhan.auth import auth
from app.storage.database import db
from app.storage.models import Direction


@dataclass
class PivotLevels:
    pivot: float
    r1: float
    r2: float
    r3: float
    s1: float
    s2: float
    s3: float


@dataclass
class OIProfile:
    support_strike: float       # Max Put OI Strike (Institutional Put Wall / Floor)
    resistance_strike: float    # Max Call OI Strike (Institutional Call Wall / Ceiling)
    max_pain: float
    total_put_oi: int
    total_call_oi: int
    pcr: float                  # Put-Call Ratio


@dataclass
class ConfluenceResult:
    is_valid: bool
    rejection_reason: Optional[str]
    pivot_levels: Optional[PivotLevels]
    oi_profile: Optional[OIProfile]
    distance_to_trap_pct: float
    confluence_score: int       # 0 - 100
    summary_text: str


class ConfluenceEngine:
    """Evaluates Pivot Points and Open Interest Support/Resistance before executing orders."""

    def __init__(self):
        self._pivot_cache: Dict[str, Tuple[date, PivotLevels]] = {}
        self._oi_cache: Dict[str, Tuple[datetime, OIProfile]] = {}

    def calculate_traditional_pivots(self, high: float, low: float, close: float) -> PivotLevels:
        """
        Calculates Standard Floor / Traditional Pivots:
        P = (H + L + C) / 3
        R1 = 2P - L, S1 = 2P - H
        R2 = P + (H - L), S2 = P - (H - L)
        R3 = H + 2(P - L), S3 = L - 2(H - P)
        """
        p = round((high + low + close) / 3.0, 2)
        r1 = round(2.0 * p - low, 2)
        s1 = round(2.0 * p - high, 2)
        r2 = round(p + (high - low), 2)
        s2 = round(p - (high - low), 2)
        r3 = round(high + 2.0 * (p - low), 2)
        s3 = round(low - 2.0 * (high - p), 2)
        return PivotLevels(pivot=p, r1=r1, r2=r2, r3=r3, s1=s1, s2=s2, s3=s3)

    async def get_daily_pivots(self, symbol: str) -> Optional[PivotLevels]:
        """Fetches previous day bar and returns cached Traditional Pivot levels."""
        today = date.today()
        if symbol in self._pivot_cache:
            c_date, p = self._pivot_cache[symbol]
            if c_date == today:
                return p

        # Fetch daily bar from Yahoo Finance / exchange feed
        try:
            clean_sym = symbol.replace("&", "%26")
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{clean_sym}.NS?interval=1d&range=5d"
            headers = {"User-Agent": "Mozilla/5.0"}
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 200:
                    d = resp.json().get("chart", {}).get("result", [{}])[0]
                    quote = d.get("indicators", {}).get("quote", [{}])[0]
                    highs = quote.get("high", [])
                    lows = quote.get("low", [])
                    closes = quote.get("close", [])
                    # Pick previous session (index -2)
                    if len(highs) >= 2 and highs[-2] is not None:
                        pivots = self.calculate_traditional_pivots(
                            float(highs[-2]), float(lows[-2]), float(closes[-2])
                        )
                        self._pivot_cache[symbol] = (today, pivots)
                        return pivots
        except Exception as e:
            logger.debug(f"Pivots fetch note for {symbol}: {e}")

        return None

    async def get_dhan_oi_profile(self, security_id: str, symbol: str) -> Optional[OIProfile]:
        """Queries Dhan Option Chain API to determine Support Strike & Resistance Strike."""
        now = datetime.now()
        if symbol in self._oi_cache:
            ts, profile = self._oi_cache[symbol]
            if (now - ts).total_seconds() < 900:  # 15m cache
                return profile

        if not auth.has_credentials:
            return None

        try:
            url = "https://api.dhan.co/v2/optionchain"
            headers = auth.get_headers()
            payload = {
                "UnderlyingScrip": int(security_id),
                "UnderlyingSeg": "NSE_EQ",
            }
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.post(url, headers=headers, json=payload)
                if resp.status_code == 200:
                    data = resp.json().get("data", {})
                    oc = data.get("oc", {})
                    if oc:
                        put_oi_by_strike = {}
                        call_oi_by_strike = {}
                        tot_put_oi = 0
                        tot_call_oi = 0

                        for strike_str, details in oc.items():
                            try:
                                strike = float(strike_str)
                                ce = details.get("ce", {})
                                pe = details.get("pe", {})
                                c_oi = int(ce.get("oi", 0))
                                p_oi = int(pe.get("oi", 0))

                                call_oi_by_strike[strike] = c_oi
                                put_oi_by_strike[strike] = p_oi
                                tot_call_oi += c_oi
                                tot_put_oi += p_oi
                            except ValueError:
                                continue

                        if put_oi_by_strike and call_oi_by_strike:
                            support_strike = max(put_oi_by_strike, key=put_oi_by_strike.get)
                            resistance_strike = max(call_oi_by_strike, key=call_oi_by_strike.get)
                            pcr = round(tot_put_oi / tot_call_oi, 2) if tot_call_oi > 0 else 1.0

                            profile = OIProfile(
                                support_strike=support_strike,
                                resistance_strike=resistance_strike,
                                max_pain=round((support_strike + resistance_strike) / 2.0, 2),
                                total_put_oi=tot_put_oi,
                                total_call_oi=tot_call_oi,
                                pcr=pcr,
                            )
                            self._oi_cache[symbol] = (now, profile)
                            return profile
        except Exception as e:
            logger.debug(f"OI Profile fetch note for {symbol}: {e}")

        return None

    async def evaluate_confluence(
        self,
        security_id: str,
        symbol: str,
        direction: Direction,
        entry_price: float,
        stop_loss: float,
        target: float,
    ) -> ConfluenceResult:
        """
        Validates breakout against Pivots and Open Interest:
        1. SHORT: Rejects if price is right into S1/S2 support or OI Put Wall Support Strike.
        2. LONG: Rejects if price is right into R1/R2 resistance or OI Call Wall Resistance Strike.
        """
        pivots = await self.get_daily_pivots(symbol)
        oi = await self.get_dhan_oi_profile(security_id, symbol)

        rejection_reason = None
        confluence_score = 80
        dist_to_trap_pct = 999.0
        summary_lines = []

        # 1. Evaluate Pivot Levels
        if pivots:
            summary_lines.append(f"Pivot: ₹{pivots.pivot:.1f} | S1: ₹{pivots.s1:.1f} | R1: ₹{pivots.r1:.1f}")
            if direction == Direction.SHORT:
                # If shorting, check distance to S1 support
                if entry_price > pivots.s1:
                    dist_to_s1 = ((entry_price - pivots.s1) / entry_price) * 100.0
                    dist_to_trap_pct = min(dist_to_trap_pct, dist_to_s1)
                    # TRAP WARNING: Selling within 0.45% of S1 Support without breaking it first
                    if dist_to_s1 <= 0.45:
                        rejection_reason = (
                            f"Pivot S1 Support Trap: Entry ₹{entry_price:.2f} is only {dist_to_s1:.2f}% "
                            f"above S1 Support (₹{pivots.s1:.2f}). High bounce risk!"
                        )
                elif entry_price <= pivots.s1:
                    # Broken below S1 - clean institutional breakdown
                    confluence_score += 10
                    summary_lines.append("Confirmed break below S1 Support floor")
            else:
                # If buying long, check distance to R1 resistance
                if entry_price < pivots.r1:
                    dist_to_r1 = ((pivots.r1 - entry_price) / entry_price) * 100.0
                    dist_to_trap_pct = min(dist_to_trap_pct, dist_to_r1)
                    # TRAP WARNING: Buying within 0.45% of R1 Resistance without breaking it first
                    if dist_to_r1 <= 0.45:
                        rejection_reason = (
                            f"Pivot R1 Resistance Trap: Entry ₹{entry_price:.2f} is only {dist_to_r1:.2f}% "
                            f"below R1 Resistance (₹{pivots.r1:.2f}). High rejection risk!"
                        )
                elif entry_price >= pivots.r1:
                    # Broken above R1 - clean institutional breakout
                    confluence_score += 10
                    summary_lines.append("Confirmed break above R1 Resistance ceiling")

        # 2. Evaluate Open Interest (OI Profile)
        if oi:
            summary_lines.append(f"OI Support: ₹{oi.support_strike:.0f} | OI Resistance: ₹{oi.resistance_strike:.0f} (PCR: {oi.pcr:.2f})")
            if direction == Direction.SHORT:
                # Rejection if shorting right on top of Max Put OI Support Wall
                if entry_price >= oi.support_strike:
                    dist_to_oi_sup = ((entry_price - oi.support_strike) / entry_price) * 100.0
                    dist_to_trap_pct = min(dist_to_trap_pct, dist_to_oi_sup)
                    if dist_to_oi_sup <= 0.50:
                        rejection_reason = (
                            f"Open Interest Put Wall Trap: Major Put OI Support sits at ₹{oi.support_strike:.0f} "
                            f"({dist_to_oi_sup:.2f}% away). Institutional writers defending strike!"
                        )
            else:
                # Rejection if buying right beneath Max Call OI Resistance Wall
                if entry_price <= oi.resistance_strike:
                    dist_to_oi_res = ((oi.resistance_strike - entry_price) / entry_price) * 100.0
                    dist_to_trap_pct = min(dist_to_trap_pct, dist_to_oi_res)
                    if dist_to_oi_res <= 0.50:
                        rejection_reason = (
                            f"Open Interest Call Wall Trap: Major Call OI Resistance sits at ₹{oi.resistance_strike:.0f} "
                            f"({dist_to_oi_res:.2f}% away). Institutional call writers capping upside!"
                        )

        is_valid = rejection_reason is None

        return ConfluenceResult(
            is_valid=is_valid,
            rejection_reason=rejection_reason,
            pivot_levels=pivots,
            oi_profile=oi,
            distance_to_trap_pct=dist_to_trap_pct,
            confluence_score=confluence_score if is_valid else 30,
            summary_text=" | ".join(summary_lines) if summary_lines else "Standard Multi-Confluence Checked",
        )


confluence_engine = ConfluenceEngine()
