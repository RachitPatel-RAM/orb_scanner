"""
Firebase Realtime Database Cloud Sync Engine for ORB Scanner.

Syncs ML model parameters, candlestick learning weights, stock rankings,
and daily backtest reports to Firebase (orbscanner-cb055).
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional
import httpx

from app.config import logger, settings


class FirebaseSyncManager:
    """Manages cloud persistence of ML weights, reports, and stock recommendations."""

    def __init__(self):
        self.project_id = settings.firebase_project_id or "orbscanner-cb055"
        self.api_key = settings.firebase_api_key
        self.base_url = f"https://{self.project_id}-default-rtdb.firebaseio.com"

    async def save_model_weights(self, weights: Dict[str, Any]) -> bool:
        """Saves learned ML candlestick weights to Firebase."""
        url = f"{self.base_url}/ml_model/weights.json"
        data = {
            "updated_at": datetime.now().isoformat(),
            "weights": weights,
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.put(url, json=data)
                if resp.status_code == 200:
                    logger.info("Successfully synced ML model weights to Firebase.")
                    return True
        except Exception as e:
            logger.warning(f"Error syncing model weights to Firebase: {e}")
        return False

    async def get_model_weights(self) -> Optional[Dict[str, Any]]:
        """Retrieves learned ML weights from Firebase."""
        url = f"{self.base_url}/ml_model/weights.json"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url)
                if resp.status_code == 200 and resp.json():
                    return resp.json().get("weights")
        except Exception as e:
            logger.debug(f"Error fetching model weights from Firebase: {e}")
        return None

    async def save_daily_report(self, report_key: str, report_data: Dict[str, Any]) -> bool:
        """Saves a daily backtest / performance report to Firebase."""
        url = f"{self.base_url}/daily_reports/{report_key}.json"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.put(url, json=report_data)
                return resp.status_code == 200
        except Exception as e:
            logger.warning(f"Error saving report to Firebase: {e}")
            return False

    async def save_stock_rankings(self, rankings: List[Dict[str, Any]]) -> bool:
        """Saves top recommended stocks and quality scores to Firebase."""
        url = f"{self.base_url}/recommendations/latest.json"
        payload = {
            "updated_at": datetime.now().isoformat(),
            "stocks": rankings,
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.put(url, json=payload)
                return resp.status_code == 200
        except Exception as e:
            logger.warning(f"Error saving stock rankings to Firebase: {e}")
            return False


firebase_sync = FirebaseSyncManager()
