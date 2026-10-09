"""
Volume Profile Analyzer (Section 7).

Deterministic, auditable approximate bar volume profile:
- Spans previous completed regular session's high-low range.
- 64 equal-width price bins rounded to instrument tick size.
- Uniform-within-range allocation of 1m/5m bar volume across covered bins.
- Identifies Point of Control (POC), Value Area High (VAH), Value Area Low (VAL).
- Explicitly labeled APPROXIMATE_BAR_VOLUME_PROFILE.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple
import math

from app.storage.models import Candle


@dataclass(frozen=True)
class VolumeProfileBin:
    bin_index: int
    price_low: float
    price_high: float
    center_price: float
    volume: float


@dataclass
class VolumeProfileResult:
    """Represents a computed session volume profile."""
    session_date: str
    symbol: str
    profile_type: str = "APPROXIMATE_BAR_VOLUME_PROFILE"
    total_volume: float = 0.0
    range_high: float = 0.0
    range_low: float = 0.0
    tick_size: float = 0.05
    num_bins: int = 64
    bin_width: float = 0.0
    poc_price: float = 0.0
    poc_volume: float = 0.0
    vah_price: float = 0.0
    val_price: float = 0.0
    value_area_volume: float = 0.0
    value_area_pct: float = 0.70
    bins: List[VolumeProfileBin] = field(default_factory=list)
    methodology_note: str = (
        "Calculated using 64 equal-width bins with uniform distribution assumption across bar high-low. "
        "Does not represent true tick-level order book liquidity or aggressive market orders."
    )

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # Bins can be large, store lightweight summary or full bins
        return d

    def get_summary(self) -> Dict[str, Any]:
        return {
            "profile_type": self.profile_type,
            "session_date": self.session_date,
            "symbol": self.symbol,
            "poc_price": round(self.poc_price, 2),
            "vah_price": round(self.vah_price, 2),
            "val_price": round(self.val_price, 2),
            "total_volume": round(self.total_volume, 2),
            "num_bins": self.num_bins,
        }


class VolumeProfileEngine:
    """Computes reproducible, deterministic session volume profile from bar OHLCV."""

    @staticmethod
    def compute_profile(
        candles: List[Candle],
        session_date: str,
        symbol: str,
        tick_size: float = 0.05,
        target_bins: int = 64,
        value_area_pct: float = 0.70,
    ) -> Optional[VolumeProfileResult]:
        """
        Builds a 64-bin approximate bar volume profile from completed session candles.
        """
        if not candles:
            return None

        range_high = max(c.high for c in candles)
        range_low = min(c.low for c in candles)
        total_range = range_high - range_low

        if total_range <= 0:
            return None

        # Check total ticks in range
        ticks_in_range = int(round(total_range / tick_size))
        actual_bins = min(target_bins, max(1, ticks_in_range))
        bin_width = total_range / actual_bins

        # Initialize bins
        bin_volumes = [0.0] * actual_bins
        bin_boundaries: List[Tuple[float, float, float]] = [] # low, high, center

        for i in range(actual_bins):
            b_low = range_low + i * bin_width
            b_high = range_low + (i + 1) * bin_width
            b_center = round((b_low + b_high) / 2.0 / tick_size) * tick_size
            bin_boundaries.append((b_low, b_high, b_center))

        total_volume = 0.0

        # Allocate bar volume across bins
        for c in candles:
            vol = max(0.0, float(c.volume))
            if vol <= 0:
                continue
            total_volume += vol
            c_high = min(c.high, range_high)
            c_low = max(c.low, range_low)
            bar_range = c_high - c_low

            if bar_range <= 0:
                # Zero-range bar: place entirely in containing bin
                idx = min(actual_bins - 1, max(0, int((c.close - range_low) / bin_width)))
                bin_volumes[idx] += vol
                continue

            # Allocate proportionally by overlap
            for i, (b_low, b_high, _) in enumerate(bin_boundaries):
                overlap_low = max(c_low, b_low)
                overlap_high = min(c_high, b_high)
                overlap = max(0.0, overlap_high - overlap_low)
                if overlap > 0:
                    fraction = overlap / bar_range
                    bin_volumes[i] += vol * fraction

        if total_volume <= 0:
            return None

        # 1. POC = Bin with highest volume. Ties broken to lower price.
        max_vol = -1.0
        poc_idx = 0
        for i, vol in enumerate(bin_volumes):
            if vol > max_vol:
                max_vol = vol
                poc_idx = i

        poc_price = bin_boundaries[poc_idx][2]
        poc_volume = bin_volumes[poc_idx]

        # 2. Value Area: 70% of total volume expanding outward from POC
        target_va_vol = total_volume * value_area_pct
        current_va_vol = poc_volume
        va_min_idx = poc_idx
        va_max_idx = poc_idx

        while current_va_vol < target_va_vol and (va_min_idx > 0 or va_max_idx < actual_bins - 1):
            vol_below = bin_volumes[va_min_idx - 1] if va_min_idx > 0 else -1.0
            vol_above = bin_volumes[va_max_idx + 1] if va_max_idx < actual_bins - 1 else -1.0

            # Tie goes to lower price side
            if vol_below >= vol_above and vol_below >= 0:
                va_min_idx -= 1
                current_va_vol += vol_below
            elif vol_above >= 0:
                va_max_idx += 1
                current_va_vol += vol_above
            else:
                break

        val_price = bin_boundaries[va_min_idx][0] # lower edge of VAL bin
        vah_price = bin_boundaries[va_max_idx][1] # upper edge of VAH bin

        profile_bins: List[VolumeProfileBin] = []
        for i, (b_low, b_high, b_center) in enumerate(bin_boundaries):
            profile_bins.append(
                VolumeProfileBin(
                    bin_index=i,
                    price_low=round(b_low, 2),
                    price_high=round(b_high, 2),
                    center_price=round(b_center, 2),
                    volume=round(bin_volumes[i], 2),
                )
            )

        return VolumeProfileResult(
            session_date=session_date,
            symbol=symbol,
            total_volume=round(total_volume, 2),
            range_high=round(range_high, 2),
            range_low=round(range_low, 2),
            tick_size=tick_size,
            num_bins=actual_bins,
            bin_width=round(bin_width, 4),
            poc_price=round(poc_price, 2),
            poc_volume=round(poc_volume, 2),
            vah_price=round(vah_price, 2),
            val_price=round(val_price, 2),
            value_area_volume=round(current_va_vol, 2),
            value_area_pct=value_area_pct,
            bins=profile_bins,
        )


volume_profile_engine = VolumeProfileEngine()
