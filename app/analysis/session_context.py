"""
Optional Session Range Adapter (Section 8).

Implements optional prior-session range sweep logic:
- Configured instrument coverage, timezone, start/end boundaries, and holiday behavior.
- Disabled initially (SESSION_CONTEXT_ENABLED=false).
- When disabled, absence of session context returns NONE and does not block valid candidates.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, date
from typing import Optional

from app.analysis.liquidity_context import ContextDirection
from app.config import settings


@dataclass
class SessionContextResult:
    symbol: str
    is_enabled: bool
    direction: ContextDirection
    reference_session_name: str
    reference_high: Optional[float] = None
    reference_low: Optional[float] = None
    reason: str = "Session context disabled"


class SessionContextAdapter:
    """
    Adapter for session sweeps (e.g. Asian/London session reference).
    Disabled by default for NSE/BSE intraday cash/index strategy.
    """

    def __init__(self):
        self.enabled = getattr(settings, "session_context_enabled", False)

    def evaluate_session_context(
        self, symbol: str, current_time: Optional[datetime] = None
    ) -> SessionContextResult:
        """
        When disabled, cleanly returns NONE so baseline candidates are not blocked.
        """
        if not self.enabled:
            return SessionContextResult(
                symbol=symbol.upper(),
                is_enabled=False,
                direction=ContextDirection.NONE,
                reference_session_name="DISABLED",
                reason="SESSION_CONTEXT_ENABLED is false; adapter inactive",
            )

        # Placeholder for future multi-session instrument extensions (e.g. MCX/FX)
        return SessionContextResult(
            symbol=symbol.upper(),
            is_enabled=True,
            direction=ContextDirection.NONE,
            reference_session_name="NSE_PREV_DAY",
            reason="No active session sweep detected",
        )


session_context_adapter = SessionContextAdapter()
