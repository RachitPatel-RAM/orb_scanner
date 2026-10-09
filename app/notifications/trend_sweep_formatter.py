"""
Auditable Telegram Alert Formatter for TREND_SWEEP_FVG_V1 (Section 10).

Generates pure, auditable alert messages populated strictly from stored strategy features.
Zero hallucinated confidence, zero video claims, zero unverified promises.
Distinguishes between underlying research alerts and executable tradable instruments.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from app.strategies.trend_sweep_fvg import TrendSweepSetup


class TrendSweepAlertFormatter:
    """Formats auditable Section 10 Telegram notification templates."""

    @staticmethod
    def format_alert(
        setup: TrendSweepSetup,
        mode_label: str = "PAPER RESEARCH",
        observed_win_rate: str = "NOT ESTABLISHED (RESEARCH V1)",
    ) -> str:
        """
        Builds the complete Section 10 auditable alert message.
        """
        symbol = setup.symbol
        strat_ver = setup.strategy_version
        dir_label = "LONG (UNDERLYING BULLISH)" if setup.direction.value == "LONG" else "SHORT (UNDERLYING BEARISH)"

        # Morning bias
        bias_str = f"{setup.daily_bias}"
        if setup.daily_bias_snapshot_id:
            bias_str += f" (ID: {setup.daily_bias_snapshot_id[:12]})"

        # Trend facts
        h_details = setup.hourly_details
        ema20 = h_details.get("ema20", 0.0)
        ema50 = h_details.get("ema50", 0.0)
        h_code = h_details.get("reason_code", "UNKNOWN")
        trend_str = f"60m {setup.hourly_regime.value} | EMA20: {ema20:.2f}, EMA50: {ema50:.2f} ({h_code})"

        # Location facts
        if setup.impulse_leg:
            leg = setup.impulse_leg
            poi_str = setup.selected_15m_poi.zone_id if setup.selected_15m_poi else "NONE"
            loc_str = (
                f"15m Impulse [{leg.low_pivot.price:.2f} to {leg.high_pivot.price:.2f}] | "
                f"Discount Band: [{leg.discount_band[0]:.2f}, {leg.discount_band[1]:.2f}] | POI: {poi_str}"
            )
        else:
            loc_str = "Impulse leg unavailable"

        # Confirmation facts
        conf_str = (
            f"Swept {setup.swept_level_type} ({setup.swept_level:.2f}) at {setup.sweep_bar_time[-8:]} | "
            f"Reclaimed at {setup.reclaim_bar_time[-8:]} | Disp Ref: {setup.frozen_microstructure_ref:.2f}"
        )
        if setup.entry_fvg:
            conf_str += f" | 5m FVG: [{setup.entry_fvg.bottom:.2f}, {setup.entry_fvg.top:.2f}]"

        # Planned entry
        entry_str = f"₹{setup.entry_price:.2f} on future 5m midpoint retest (Touch inside FVG)"

        # Initial stop
        stop_str = f"₹{setup.initial_stop:.2f} (Structural Extreme - 5m Buffer)"

        # Target and exit
        target_str = f"₹{setup.target_price:.2f} (Target 2R) | Policy: {setup.exit_policy.value}"
        if setup.sizing and setup.sizing.net_reward_risk_ratio > 0:
            target_str += f" | Estimated Net RR: {setup.sizing.net_reward_risk_ratio:.2f}"

        # Sizing and risk
        if setup.sizing and setup.sizing.allowed:
            sizing_str = f"{setup.sizing.quantity} shares ({setup.sizing.lots} lot(s)) | Planned Risk: ₹{setup.sizing.nominal_gross_risk:.2f}"
        else:
            sizing_str = f"OPTIONS_EXECUTION_UNAVAILABLE | Underlying points only (Risk: {setup.initial_r_pts:.2f} pts)"

        # Optional evidence
        evidence_parts = []
        if setup.rvol and setup.rvol.status == "CALCULATED":
            evidence_parts.append(f"5m RVOL: {setup.rvol.rvol:.2f} (Median: {setup.rvol.median_volume_20:.0f})")
        else:
            evidence_parts.append("RVOL: SHADOW/UNAVAILABLE")

        if setup.volume_profile:
            vp = setup.volume_profile
            evidence_parts.append(f"VP POC: ₹{vp.get('poc_price', 0):.2f} (VAH: ₹{vp.get('vah_price', 0):.2f}, VAL: ₹{vp.get('val_price', 0):.2f})")
        else:
            evidence_parts.append("Volume Profile: SHADOW")

        evidence_parts.append("SMT: DISABLED")
        evidence_parts.append("CBDR: DISABLED")
        opt_str = " | ".join(evidence_parts)

        # No chase conditions
        no_chase = "Cancel if price moves through stop before fill, hits target before fill, or retest exceeds 15m window"

        lines = [
            f"<b>BORNBULL | {mode_label}</b>",
            f"<b>Instrument:</b> {symbol} (NSE Spot/Futures Proxy)",
            f"<b>Strategy/version:</b> {strat_ver}",
            f"<b>Direction:</b> {dir_label}",
            f"<b>Status:</b> ARMED (Waiting for future retest; not a confirmed fill)",
            f"<b>Morning bias:</b> {bias_str}",
            f"<b>Trend:</b> {trend_str}",
            f"<b>Location:</b> {loc_str}",
            f"<b>Confirmation:</b> {conf_str}",
            f"<b>Planned entry:</b> {entry_str}",
            f"<b>Initial stop/invalidation:</b> {stop_str}",
            f"<b>Target and exit version:</b> {target_str}",
            f"<b>Valid until:</b> {setup.valid_until} IST",
            f"<b>No-chase/invalidation conditions:</b> {no_chase}",
            f"<b>Quantity and planned rupee risk:</b> {sizing_str}",
            f"<b>Volume/profile/SMT/CBDR:</b> {opt_str}",
            f"<b>Data as of:</b> {setup.created_at} | <b>Setup known at:</b> {setup.created_at}",
            f"<b>Observed win rate:</b> {observed_win_rate}",
            f"<b>Heuristic Setup Score:</b> {setup.heuristic_score}/10 (WIN_PROBABILITY=UNAVAILABLE)",
            f"<b>Signal ID:</b> <code>{setup.setup_id}</code>",
        ]

        return "\n".join(lines)


trend_sweep_alert_formatter = TrendSweepAlertFormatter()


def format_trend_sweep_alert(
    setup: TrendSweepSetup,
    mode_label: str = "PAPER RESEARCH",
    observed_win_rate: str = "NOT ESTABLISHED (RESEARCH V1)",
) -> str:
    """Convenience functional wrapper for TrendSweepAlertFormatter.format_alert."""
    return TrendSweepAlertFormatter.format_alert(
        setup=setup,
        mode_label=mode_label,
        observed_win_rate=observed_win_rate,
    )
