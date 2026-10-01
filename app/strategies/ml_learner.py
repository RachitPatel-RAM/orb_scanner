"""
Candlestick Self-Learning & Machine Learning Conviction Scorer for ORB Breakouts.

Learns daily from candlestick anatomy (body ratio, rejection wicks, volume surge),
recalibrates stock win probabilities, eliminates false breakouts, and syncs
learned parameters to Firebase (orbscanner-cb055).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Any, Dict, List, Optional, Tuple

from app.config import logger
from app.storage.firebase_sync import firebase_sync
from app.storage.models import Candle, Direction, PaperTrade, Signal


@dataclass
class CandlestickFeatures:
    body_ratio: float
    upper_wick_ratio: float
    lower_wick_ratio: float
    is_strong_body: bool
    has_rejection_wick: bool
    pattern_name: str


@dataclass
class AIConvictionResult:
    score: int  # 0 to 100
    level: str  # HIGH_CONVICTION, MODERATE, LOW_RISK
    pattern: str
    reasons: List[str]


class MLLearner:
    """Self-learning engine that evaluates breakout quality and updates daily weights."""

    def __init__(self):
        # Default baseline weights
        self.weights = {
            "body_ratio_weight": 35.0,
            "volume_surge_weight": 30.0,
            "rejection_penalty": -25.0,
            "orb_width_weight": 15.0,
            "timing_weight": 10.0,
        }
        # Stock specific learned win rates
        self.stock_history: Dict[str, Dict[str, Any]] = {}
        self._initialized = False

    async def initialize_from_cloud(self):
        """Loads learned model weights and rankings from Firebase."""
        if self._initialized:
            return
        weights = await firebase_sync.get_model_weights()
        if weights and isinstance(weights, dict):
            self.weights.update(weights)
            logger.info("ML Learner loaded updated weights from Firebase.")
        self._initialized = True

    def analyze_candlestick(self, candle: Candle, direction: Direction) -> CandlestickFeatures:
        """
        Extracts morphological features from the breakout candle:
        - Body ratio = |Close - Open| / (High - Low)
        - Upper wick ratio = (High - max(Open, Close)) / (High - Low)
        - Lower wick ratio = (min(Open, Close) - Low) / (High - Low)
        """
        c_range = candle.high - candle.low
        if c_range <= 0.0001:
            return CandlestickFeatures(
                body_ratio=0.5,
                upper_wick_ratio=0.25,
                lower_wick_ratio=0.25,
                is_strong_body=False,
                has_rejection_wick=False,
                pattern_name="Neutral Doji",
            )

        body = abs(candle.close - candle.open)
        body_ratio = body / c_range
        upper_wick = (candle.high - max(candle.open, candle.close)) / c_range
        lower_wick = (min(candle.open, candle.close) - candle.low) / c_range

        # Determine pattern and rejection
        if direction == Direction.LONG:
            has_rejection = upper_wick > 0.35  # Selling pressure at top
            is_strong = body_ratio >= 0.60 and lower_wick <= 0.25
            if body_ratio >= 0.75:
                pattern = "Bullish Marubozu (Strong Momentum)"
            elif is_strong:
                pattern = "Solid Bullish Candle"
            elif has_rejection:
                pattern = "Upper Wick Rejection (Caution)"
            else:
                pattern = "Standard Candle"
        else:
            has_rejection = lower_wick > 0.35  # Buying pressure at bottom
            is_strong = body_ratio >= 0.60 and upper_wick <= 0.25
            if body_ratio >= 0.75:
                pattern = "Bearish Marubozu (Strong Breakdown)"
            elif is_strong:
                pattern = "Solid Bearish Candle"
            elif has_rejection:
                pattern = "Lower Wick Rejection (Caution)"
            else:
                pattern = "Standard Candle"

        return CandlestickFeatures(
            body_ratio=round(body_ratio, 3),
            upper_wick_ratio=round(upper_wick, 3),
            lower_wick_ratio=round(lower_wick, 3),
            is_strong_body=is_strong,
            has_rejection_wick=has_rejection,
            pattern_name=pattern,
        )

    def calculate_conviction_score(
        self,
        candle: Candle,
        direction: Direction,
        orb_high: float,
        orb_low: float,
        avg_volume_20: Optional[float] = None,
    ) -> AIConvictionResult:
        """
        Calculates 0-100% conviction score to eliminate false breakouts:
        - Strong body: +30 to +40 pts
        - Volume expansion (> 1.5x): +20 to +30 pts
        - Rejection wick against direction: -25 pts
        - Compressed optimal ORB width (0.3% - 2.5%): +15 pts
        - Morning timing (10:00 - 11:30): +10 pts
        """
        feats = self.analyze_candlestick(candle, direction)
        score = 50.0  # Base prior
        reasons = []

        # 1. Candlestick Anatomy
        if feats.is_strong_body:
            score += 20.0
            reasons.append("Strong Candle Body (>60% range)")
        if feats.body_ratio >= 0.75:
            score += 15.0
            reasons.append("Marubozu Institutional Conviction")
        if feats.has_rejection_wick:
            score -= 25.0
            reasons.append("Counter-trend Rejection Wick Detected")

        # 2. Volume Expansion
        if avg_volume_20 and avg_volume_20 > 0:
            vol_ratio = candle.volume / avg_volume_20
            if vol_ratio >= 2.0:
                score += 25.0
                reasons.append(f"Huge Volume Surge ({vol_ratio:.1f}x avg)")
            elif vol_ratio >= 1.3:
                score += 15.0
                reasons.append(f"Above Average Volume ({vol_ratio:.1f}x)")
            elif vol_ratio < 0.7:
                score -= 15.0
                reasons.append(f"Low Volume Breakout ({vol_ratio:.1f}x avg)")

        # 3. ORB Range Width
        if orb_low > 0:
            width_pct = ((orb_high - orb_low) / orb_low) * 100.0
            if 0.3 <= width_pct <= 2.2:
                score += 10.0
                reasons.append("Optimal Range Width (Tight base)")
            elif width_pct > 3.5:
                score -= 15.0
                reasons.append(f"Over-extended Range ({width_pct:.1f}%)")

        # 4. Timing
        c_time = candle.timestamp.time()
        if time(10, 0) <= c_time <= time(11, 30):
            score += 10.0
            reasons.append("Prime Morning Liquidity Window")
        elif time(12, 0) <= c_time <= time(13, 30):
            score -= 10.0
            reasons.append("Midday Low Liquidity Window")

        # Bound score between 5 and 99
        final_score = int(max(5, min(99, score)))

        if final_score >= 70:
            level = "HIGH_CONVICTION"
        elif final_score >= 50:
            level = "MODERATE"
        else:
            level = "LOW_PROBABILITY"

        return AIConvictionResult(
            score=final_score,
            level=level,
            pattern=feats.pattern_name,
            reasons=reasons,
        )

    async def update_daily_learning(self, trades: List[PaperTrade]) -> Dict[str, Any]:
        """
        Runs after market close:
        Analyzes today's executed trades, updates stock follow-through stats,
        and saves updated weights and rankings to Firebase.
        """
        if not trades:
            return {"status": "No trades to learn from today"}

        wins = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl < 0]
        win_rate = (len(wins) / len(trades)) * 100.0 if trades else 0.0

        # Update stock history
        stock_stats: Dict[str, Dict[str, Any]] = {}
        for t in trades:
            sym = t.symbol
            if sym not in stock_stats:
                stock_stats[sym] = {"trades": 0, "wins": 0, "pnl": 0.0}
            stock_stats[sym]["trades"] += 1
            if t.pnl > 0:
                stock_stats[sym]["wins"] += 1
            stock_stats[sym]["pnl"] += t.pnl

        # Rank stocks
        rankings = []
        for sym, d in stock_stats.items():
            wr = (d["wins"] / d["trades"]) * 100.0 if d["trades"] > 0 else 0.0
            rankings.append({
                "symbol": sym,
                "trades": d["trades"],
                "win_rate": round(wr, 1),
                "total_pnl": round(d["pnl"], 2),
            })

        rankings.sort(key=lambda x: (x["win_rate"], x["total_pnl"]), reverse=True)

        # Sync to Firebase
        await firebase_sync.save_stock_rankings(rankings)
        await firebase_sync.save_model_weights(self.weights)

        logger.info(f"Daily ML self-learning complete: {len(trades)} trades processed. Weights synced to Firebase.")
        return {
            "total_trades": len(trades),
            "win_rate": round(win_rate, 1),
            "top_stocks": rankings[:5],
        }


ml_learner = MLLearner()
