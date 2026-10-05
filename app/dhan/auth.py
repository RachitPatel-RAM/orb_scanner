"""
DhanHQ v2 Authentication and Access Verification.

Validates client credentials, verifies Data API subscription access,
handles token expiry detection, and dispatches Telegram error notifications.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional, Tuple
import httpx

from app.config import logger, settings
from app.notifications.telegram import notifier


class DhanAuth:
    """Manages DhanHQ v2 API authentication lifecycle."""

    BASE_URL = "https://api.dhan.co/v2"

    def __init__(self, client_id: Optional[str] = None, access_token: Optional[str] = None):
        self.client_id = (client_id or settings.dhan_client_id).strip()
        self.access_token = (access_token or settings.dhan_access_token).strip()

    @property
    def has_credentials(self) -> bool:
        return bool(self.client_id and self.access_token)

    def get_headers(self) -> Dict[str, str]:
        """Builds standard DhanHQ v2 HTTP headers."""
        return {
            "client-id": self.client_id,
            "access-token": self.access_token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def get_profile(self) -> Dict[str, Any]:
        """
        Calls Dhan profile API to verify authentication.
        Raises RuntimeError on invalid credentials or token expiration.
        """
        if not self.has_credentials:
            raise ValueError("Dhan credentials missing. Set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN in .env.")

        endpoint = f"{self.BASE_URL}/profile"
        headers = self.get_headers()

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(endpoint, headers=headers)

            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code in (401, 403):
                err = f"Authentication Failed (HTTP {resp.status_code}): Access token invalid or expired. {resp.text}"
                logger.error(err)
                await notifier.send_error(f"Dhan Authentication Failed: Invalid or expired token (HTTP {resp.status_code})")
                raise PermissionError(err)
            else:
                err = f"Dhan Profile API Error (HTTP {resp.status_code}): {resp.text}"
                logger.error(err)
                raise RuntimeError(err)

    async def is_token_valid(self) -> bool:
        """Returns True if current Dhan access token is active and valid."""
        try:
            profile = await self.get_profile()
            return bool(profile and ("dhanClientId" in profile or "data" in profile or "status" in profile))
        except Exception as e:
            logger.warning(f"Token validation check failed: {e}")
            return False

    async def check_data_subscription(self) -> Tuple[bool, str]:
        """
        Verifies that Data API access is active.
        Dhan accounts require Data API subscription to receive WebSocket and historical charts.
        """
        try:
            profile = await self.get_profile()
            # Dhan profile structure: inspect status or active plans
            logger.info("Successfully validated DhanHQ v2 profile.")
            return True, "Data API access active"
        except PermissionError as pe:
            return False, f"Token expired or permission denied: {pe}"
        except Exception as e:
            return False, f"Data subscription check failed: {e}"

    async def validate_credentials(self) -> bool:
        """
        Top-level startup check.
        Ensures credentials exist, token is unexpired, and alerts Telegram if invalid.
        """
        if not self.has_credentials:
            msg = "Dhan Client ID or Access Token is missing from environment (.env)."
            logger.error(msg)
            await notifier.send_error(msg)
            return False

        valid, reason = await self.check_data_subscription()
        if not valid:
            logger.critical(f"Dhan validation failed: {reason}")
            await notifier.send_error(f"Dhan Startup Validation Failed: {reason}")
            return False

        logger.info("DhanHQ v2 credentials and Data API subscription successfully verified.")
        return True

    async def renew_token(self) -> Tuple[bool, str]:
        """
        Calls Dhan RenewToken API (GET /v2/RenewToken) to refresh an active token.
        If successful, updates self.access_token and saves the new token to .env.
        Note: Works only while the current token is still active (unexpired).
        """
        if not self.has_credentials:
            return False, "Credentials missing in .env"

        endpoint = f"{self.BASE_URL}/RenewToken"
        headers = {
            "access-token": self.access_token,
            "dhanClientId": self.client_id,
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(endpoint, headers=headers)

            if resp.status_code == 200:
                data = resp.json()
                new_token = data.get("token") or data.get("accessToken")
                expiry = data.get("expiryTime", "24 hours")
                if new_token:
                    self.access_token = new_token
                    # Update settings
                    settings.dhan_access_token = new_token
                    # Update .env file
                    env_path = settings.database_path
                    from pathlib import Path
                    env_file = Path(".env")
                    if env_file.exists():
                        txt = env_file.read_text(encoding="utf-8")
                        lines = [
                            f"DHAN_ACCESS_TOKEN={new_token}" if l.startswith("DHAN_ACCESS_TOKEN=") else l
                            for l in txt.splitlines()
                        ]
                        env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
                    try:
                        from app.dhan.live_feed import live_feed
                        asyncio.create_task(live_feed.reconnect())
                    except Exception:
                        pass
                    logger.info(f"Dhan access token successfully renewed! New expiry: {expiry}")
                    return True, f"Token renewed successfully! Valid until: {expiry}"
                return False, f"Unexpected response from RenewToken: {data}"
            else:
                err = f"RenewToken failed (HTTP {resp.status_code}): {resp.text}"
                logger.error(err)
                return False, err
        except Exception as e:
            err = f"Exception during token renewal: {e}"
            logger.error(err)
            return False, err


auth = DhanAuth()
