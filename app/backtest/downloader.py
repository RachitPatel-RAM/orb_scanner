"""
Historical Data Batch Downloader for Backtesting.

Downloads and caches historical 1m/5m OHLCV bars across a date range and stock universe,
adhering to Dhan rate limits and skipping already cached days.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, date, timedelta
from typing import List, Optional
import pandas as pd

from app.config import logger
from app.dhan.historical import historical_manager
from app.dhan.instruments import InstrumentInfo
from app.market.session import default_session
from app.storage.models import Candle


class HistoricalBatchDownloader:
    """Orchestrates bulk download and local caching of historical stock data."""

    def __init__(self, historical_client=None):
        self.historical = historical_client or historical_manager

    def get_trading_days(self, start_date: date, end_date: date) -> List[date]:
        """Returns all weekday trading dates between start_date and end_date inclusive."""
        cur = start_date
        days = []
        while cur <= end_date:
            if cur.weekday() < 5:  # Monday to Friday
                days.append(cur)
            cur += timedelta(days=1)
        return days

    async def download_universe_data(
        self,
        instruments: List[InstrumentInfo],
        start_date: date,
        end_date: date,
        interval: int = 1,
        force: bool = False,
    ) -> int:
        """
        Downloads data for all instruments across trading dates.
        Returns total number of candles acquired.
        """
        trading_days = self.get_trading_days(start_date, end_date)
        logger.info(
            f"Starting batch historical download: {len(instruments)} instruments across "
            f"{len(trading_days)} trading days ({start_date} to {end_date})."
        )

        total_candles = 0
        total_tasks = len(instruments) * len(trading_days)
        completed = 0

        for inst in instruments:
            for day in trading_days:
                candles = await self.historical.fetch_intraday_candles(
                    security_id=inst.security_id,
                    symbol=inst.symbol,
                    trade_date=day,
                    interval=interval,
                    force_download=force,
                )
                total_candles += len(candles)
                completed += 1

                if completed % 25 == 0 or completed == total_tasks:
                    pct = (completed / total_tasks * 100.0) if total_tasks > 0 else 100.0
                    logger.info(f"Historical download progress: {completed}/{total_tasks} ({pct:.1f}%)")

        logger.info(f"Historical batch download complete. Total candles: {total_candles}.")
        return total_candles


batch_downloader = HistoricalBatchDownloader()
