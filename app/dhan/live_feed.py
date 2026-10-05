"""
DhanHQ Live Market Feed WebSocket Client.

Maintains a resilient real-time connection to DhanHQ v2 WebSocket feed:
- Binary packet unpacking (Ticker & Quote packets)
- Batch subscription partitioning (100 instruments/batch)
- Auto-reconnect with exponential backoff
- Stale feed detection & heartbeat monitoring
- Clean async shutdown
"""

from __future__ import annotations

import asyncio
from datetime import datetime, time
import json
import struct
from typing import Callable, Dict, List, Optional, Set
import websockets
import zoneinfo

from app.config import logger, settings
from app.dhan.auth import auth
from app.dhan.instruments import InstrumentInfo, instrument_manager
from app.market.candle_builder import TickData
from app.market.session import default_session, IST_TZ
from app.notifications.telegram import notifier


class LiveMarketFeed:
    """High-performance WebSocket client for DhanHQ v2 Live Market Feed."""

    FEED_URL = "wss://api-feed.dhan.co"
    BATCH_SIZE = 100

    def __init__(
        self,
        on_tick: Optional[Callable[[TickData], None]] = None,
        stale_threshold_seconds: int = 120,
    ):
        self.on_tick = on_tick
        self.stale_threshold_seconds = stale_threshold_seconds

        self._running = False
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._subscribed_instruments: Dict[str, InstrumentInfo] = {}  # sec_id -> InstrumentInfo
        self._last_tick_time: Optional[datetime] = None
        self._stale_alert_sent = False
        self._stats = {
            "ticks_received": 0,
            "reconnect_count": 0,
            "connected_at": None,
        }

    def _is_open(self) -> bool:
        """Compatibility helper to check if websocket is currently open."""
        if self._ws is None:
            return False
        state = getattr(self._ws, "state", None)
        if state is not None:
            return getattr(state, "name", "") == "OPEN"
        return bool(getattr(self._ws, "open", False))

    @property
    def is_connected(self) -> bool:
        return self._is_open()

    @property
    def last_tick_time(self) -> Optional[datetime]:
        return self._last_tick_time

    @property
    def subscribed_count(self) -> int:
        return len(self._subscribed_instruments)

    def subscribe_instruments(self, instruments: List[InstrumentInfo]) -> None:
        """Registers instruments for subscription."""
        for inst in instruments:
            self._subscribed_instruments[inst.security_id] = inst

    async def _send_subscription_batches(self) -> None:
        """Splits subscribed instruments into allowed 100-scrip chunks and dispatches JSON."""
        if not self._ws or not self._is_open():
            return

        all_items = list(self._subscribed_instruments.values())
        if not all_items:
            logger.info("No instruments to subscribe.")
            return

        # Use RequestCode 17 (Quote) if volume filter enabled, else 15 (Ticker)
        req_code = 17 if settings.strategy.filters.volume.enabled else 15

        for i in range(0, len(all_items), self.BATCH_SIZE):
            batch = all_items[i : i + self.BATCH_SIZE]
            inst_list = [
                {
                    "ExchangeSegment": inst.exchange_segment,
                    "SecurityId": str(inst.security_id),
                }
                for inst in batch
            ]

            payload = {
                "RequestCode": req_code,
                "InstrumentCount": len(batch),
                "InstrumentList": inst_list,
            }

            try:
                await self._ws.send(json.dumps(payload))
                logger.debug(f"Subscribed batch {i // self.BATCH_SIZE + 1} ({len(batch)} scrips).")
                await asyncio.sleep(0.05)  # small throttle between batches
            except Exception as e:
                logger.error(f"Failed to send subscription batch: {e}")

        logger.info(
            f"Successfully dispatched subscriptions for {len(all_items)} instruments (Code {req_code})."
        )

    def _parse_binary_packet(self, data: bytes) -> Optional[TickData]:
        """
        Parses binary response packet according to DhanHQ v2 specification.
        Header:
        - 1 byte: response_code (int8)
        - 2 bytes: message_length (int16, little endian '<h')
        - 1 byte: exchange_segment (int8)
        - 4 bytes: security_id (int32, little endian '<i')
        Total Header: 8 bytes
        """
        if len(data) < 8:
            return None

        try:
            resp_code, msg_len, exch_seg, sec_id_int = struct.unpack("<bhbi", data[:8])
        except struct.error:
            return None

        sec_id = str(sec_id_int)
        symbol = instrument_manager.get_symbol(sec_id) or f"SEC_{sec_id}"
        payload = data[8:]

        ltp: float = 0.0
        ltt_epoch: int = 0
        volume: float = 0.0

        if resp_code == 2:  # Ticker Packet
            if len(payload) >= 8:
                try:
                    ltp, ltt_epoch = struct.unpack("<fI", payload[:8])
                except struct.error:
                    return None
        elif resp_code == 4:  # Quote Packet
            if len(payload) >= 16:
                try:
                    # LTP (float), LTQ (int32), LTT (uint32), AvgPrice (float)
                    ltp, ltq, ltt_epoch, avg_p = struct.unpack("<fiIf", payload[:16])
                    if len(payload) >= 20:
                        volume = float(struct.unpack("<I", payload[16:20])[0])
                except struct.error:
                    return None
        elif resp_code == 8:  # Full Packet
            if len(payload) >= 8:
                try:
                    ltp, ltt_epoch = struct.unpack("<fI", payload[:8])
                except struct.error:
                    return None
        else:
            return None

        if ltp <= 0:
            return None

        # Convert ltt to datetime
        now = default_session.now()
        if ltt_epoch > 0:
            try:
                # Dhan LTT is unix timestamp
                tick_time = datetime.fromtimestamp(ltt_epoch, tz=IST_TZ)
            except Exception:
                tick_time = now
        else:
            tick_time = now

        return TickData(
            security_id=sec_id,
            symbol=symbol,
            ltp=round(ltp, 2),
            timestamp=tick_time,
            volume=volume,
        )

    async def _message_loop(self) -> None:
        """Processes incoming binary packets from WebSocket."""
        try:
            async for msg in self._ws:
                if not self._running:
                    break
                if isinstance(msg, bytes):
                    tick = self._parse_binary_packet(msg)
                    if tick:
                        self._last_tick_time = default_session.now()
                        self._stats["ticks_received"] += 1
                        self._stale_alert_sent = False
                        if self.on_tick:
                            self.on_tick(tick)
                elif isinstance(msg, str):
                    logger.debug(f"Received text message from Dhan feed: {msg}")
        except websockets.ConnectionClosed:
            logger.warning("Dhan WebSocket connection closed by remote server.")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error processing market feed message: {e}")

    async def _stale_feed_monitor(self) -> None:
        """Monitors for data stalls during active market hours."""
        while self._running:
            await asyncio.sleep(10)
            if not self._running:
                break

            now = default_session.now()
            # Only monitor during active trading entry hours (09:15 to 15:15 IST).
            # From 15:15 to 15:30, intraday trades are squared off and market winds down.
            if default_session.is_market_open(now) and now.time() < time(15, 15):
                if self._last_tick_time:
                    stale_dur = (now - self._last_tick_time).total_seconds()
                    if stale_dur > self.stale_threshold_seconds:
                        logger.warning(
                            f"Feed appears STALE: No ticks received for {stale_dur:.0f} seconds."
                        )
                        if not self._stale_alert_sent:
                            self._stale_alert_sent = True
                            await notifier.send_error(
                                f"Market feed stale: No live ticks received for {stale_dur:.0f}s. Triggering reconnect..."
                            )
                        # Close WebSocket to trigger auto-reconnect
                        if self._ws:
                            await self._ws.close()
                elif self.is_connected and (now - self._stats.get("connected_at", now)).total_seconds() > 60:
                    logger.warning("Feed connected but no ticks received after 60s.")

    async def reconnect(self) -> None:
        """Forces WebSocket to reconnect using current credentials."""
        logger.info("Triggering Live Market Feed reconnect with latest credentials...")
        if self._ws:
            try:
                await self._ws.close()
            except Exception as e:
                logger.debug(f"Error closing socket during reconnect: {e}")

    async def start(self) -> None:
        """Connects to Dhan WebSocket with auto-reconnect and heartbeat loop."""
        self._running = True

        # Launch watchdog monitor
        monitor_task = asyncio.create_task(self._stale_feed_monitor())

        backoff = 1.0
        max_backoff = 60.0

        while self._running:
            client_id = auth.client_id
            token = auth.access_token

            if not client_id or not token:
                logger.warning("Dhan client_id or access_token missing. Waiting for token...")
                await asyncio.sleep(5)
                continue

            ws_url = f"{self.FEED_URL}?version=2&token={token}&clientId={client_id}&authType=2"
            try:
                logger.info(f"Connecting to Dhan Live Market Feed ({self.FEED_URL})...")
                async with websockets.connect(
                    ws_url,
                    ping_interval=20,
                    ping_timeout=15,
                    max_size=10 * 1024 * 1024,
                ) as ws:
                    self._ws = ws
                    self._stats["connected_at"] = default_session.now()
                    backoff = 1.0  # reset backoff on successful connect
                    logger.info("Connected to DhanHQ v2 Live Market Feed.")

                    # Resubscribe instruments
                    await self._send_subscription_batches()

                    # Start message receiving loop
                    await self._message_loop()

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._stats["reconnect_count"] += 1
                logger.error(
                    f"WebSocket error: {e}. Reconnecting in {backoff:.1f}s (Attempt #{self._stats['reconnect_count']})..."
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, max_backoff)

        monitor_task.cancel()
        logger.info("Live market feed stopped cleanly.")

    async def stop(self) -> None:
        """Stops the live feed client cleanly."""
        self._running = False
        if self._ws:
            await self._ws.close()


live_feed = LiveMarketFeed()
