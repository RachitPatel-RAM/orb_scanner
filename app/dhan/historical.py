"""
DhanHQ Historical Intraday Data Fetcher and Local Cache.

Fetches 1-minute and 5-minute historical OHLCV data with:
- Strict rate limiting (max requests/second)
- Exponential backoff on transient errors / 429
- Local file & SQLite caching to eliminate duplicate API requests
- Seamless intraday recovery for late startups
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, date, time, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
import httpx
import zoneinfo

from app.config import DATA_DIR, logger, settings
from app.dhan.auth import auth
from app.market.session import default_session, IST_TZ
from app.storage.database import db
from app.storage.models import Candle

HISTORICAL_CACHE_DIR = DATA_DIR / "historical"
HISTORICAL_CACHE_DIR.mkdir(parents=True, exist_ok=True)


class HistoricalDataManager:
    """Manages downloading, caching, and parsing of Dhan historical intraday data."""

    BASE_URL = "https://api.dhan.co/v2"

    def __init__(self, max_concurrent: int = 4, requests_per_second: float = 4.0):
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.min_interval = 1.0 / requests_per_second
        self._last_req_time: float = 0.0

    async def _rate_limit_wait(self) -> None:
        """Enforces rate limiting between consecutive outbound HTTP requests."""
        now = asyncio.get_event_loop().time()
        elapsed = now - self._last_req_time
        if elapsed < self.min_interval:
            await asyncio.sleep(self.min_interval - elapsed)
        self._last_req_time = asyncio.get_event_loop().time()

    def _get_cache_path(self, security_id: str, trade_date: date, interval: int = 1) -> Path:
        """Generates deterministic cache filepath: data/historical/{sec_id}_{date}_{interval}m.json"""
        return HISTORICAL_CACHE_DIR / f"{security_id}_{trade_date.isoformat()}_{interval}m.json"

    async def fetch_intraday_candles(
        self,
        security_id: str,
        symbol: str,
        trade_date: date,
        interval: int = 1,
        force_download: bool = False,
    ) -> List[Candle]:
        """
        Fetches intraday candles for a specific stock and date.
        Checks local disk cache first before issuing network requests.
        """
        cache_file = self._get_cache_path(security_id, trade_date, interval)

        # 1. Read from cache if present and not today's ongoing session
        is_today = trade_date == default_session.now().date()
        if not force_download and not is_today and cache_file.exists():
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    cached_data = json.load(f)
                return self._parse_dhan_response(security_id, symbol, cached_data)
            except Exception as e:
                logger.warning(f"Failed to read cache {cache_file}: {e}")

        # 2. Fetch from Dhan REST API
        if not auth.has_credentials:
            logger.warning(f"No Dhan credentials to fetch historical data for {symbol}.")
            return []

        from_str = f"{trade_date.isoformat()} 09:15:00"
        to_str = f"{trade_date.isoformat()} 15:30:00"

        endpoint = f"{self.BASE_URL}/charts/intraday"
        payload = {
            "securityId": str(security_id),
            "exchangeSegment": "NSE_EQ",
            "instrument": "EQUITY",
            "fromDate": from_str,
            "toDate": to_str,
            "interval": str(interval),
        }

        headers = auth.get_headers()
        max_retries = 3

        async with self.semaphore:
            for attempt in range(1, max_retries + 1):
                await self._rate_limit_wait()
                try:
                    async with httpx.AsyncClient(timeout=15.0) as client:
                        resp = await client.post(endpoint, json=payload, headers=headers)

                    if resp.status_code == 200:
                        data = resp.json()
                        # Cache historical (past) days to disk
                        if not is_today and data:
                            with open(cache_file, "w", encoding="utf-8") as f:
                                json.dump(data, f)
                        return self._parse_dhan_response(security_id, symbol, data)

                    elif resp.status_code == 429:
                        backoff = attempt * 2.0
                        logger.warning(f"Dhan Rate Limit 429 for {symbol}. Backing off {backoff}s...")
                        await asyncio.sleep(backoff)
                    elif resp.status_code in (401, 403):
                        logger.error(f"Dhan Auth Error {resp.status_code}: Token invalid or expired.")
                        break
                    else:
                        logger.warning(f"Dhan API HTTP {resp.status_code} for {symbol}: {resp.text}")
                except Exception as e:
                    logger.error(f"Error fetching historical data for {symbol} (attempt {attempt}): {e}")
                    if attempt < max_retries:
                        await asyncio.sleep(attempt * 1.5)

        return []

    def _parse_dhan_response(
        self, security_id: str, symbol: str, data: Dict[str, Any]
    ) -> List[Candle]:
        """
        Parses Dhan intraday JSON payload into Candle objects.
        Expected Dhan format:
        {
          "status": "success",
          "data": {
            "start_Time": [1694403900, ...], // or timestamps
            "open": [2500.0, ...],
            "high": [2510.0, ...],
            "low": [2495.0, ...],
            "close": [2505.0, ...],
            "volume": [1000, ...]
          }
        }
        """
        candles: List[Candle] = []
        if not isinstance(data, dict):
            return candles

        payload_data = data.get("data") if ("data" in data and isinstance(data.get("data"), dict)) else data
        if not payload_data:
            return candles

        times = payload_data.get("timestamp") or payload_data.get("start_Time") or []
        opens = payload_data.get("open", [])
        highs = payload_data.get("high", [])
        lows = payload_data.get("low", [])
        closes = payload_data.get("close", [])
        volumes = payload_data.get("volume", [])

        length = min(len(times), len(opens), len(highs), len(lows), len(closes))

        for i in range(length):
            t_val = times[i]
            # Convert epoch or ISO
            if isinstance(t_val, (int, float)):
                # Dhan timestamps can be seconds or milliseconds
                if t_val > 1e11:
                    t_val = t_val / 1000.0
                dt = datetime.fromtimestamp(t_val, tz=IST_TZ)
            elif isinstance(t_val, str):
                try:
                    dt = datetime.fromisoformat(t_val).replace(tzinfo=IST_TZ)
                except ValueError:
                    dt = datetime.strptime(t_val, "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST_TZ)
            else:
                continue

            vol = float(volumes[i]) if i < len(volumes) else 0.0

            candle = Candle(
                security_id=security_id,
                symbol=symbol,
                timestamp=dt,
                open=float(opens[i]),
                high=float(highs[i]),
                low=float(lows[i]),
                close=float(closes[i]),
                volume=vol,
                is_closed=True,
            )
            candles.append(candle)

        # Ensure chronological order
        candles.sort(key=lambda c: c.timestamp)
        return candles

    async def recover_today_intraday(
        self,
        security_id: str,
        symbol: str,
    ) -> List[Candle]:
        """
        Recovery for crash / late start:
        Fetches all today's 1m candles from 09:15 up to current time.
        """
        today = default_session.now().date()
        logger.info(f"Recovering today's intraday data for {symbol} ({today})...")
        candles = await self.fetch_intraday_candles(
            security_id=security_id,
            symbol=symbol,
            trade_date=today,
            interval=1,
            force_download=True,
        )

        # Filter candles up to current time
        now = default_session.now()
        recovered = [c for c in candles if c.timestamp <= now]

        # Persist to database
        for c in recovered:
            db.save_candle_1m(
                security_id=c.security_id,
                symbol=c.symbol,
                timestamp=c.iso_timestamp,
                open_=c.open,
                high=c.high,
                low=c.low,
                close=c.close,
                volume=c.volume,
                is_closed=True,
            )

        logger.info(f"Recovered {len(recovered)} candles for {symbol}.")
        return recovered


historical_manager = HistoricalDataManager()
