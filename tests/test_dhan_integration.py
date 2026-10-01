"""
Integration and Mocked Tests for DhanHQ API v2 and Telegram Engine.
"""

import asyncio
from datetime import datetime, date
import struct
import pytest
from unittest.mock import AsyncMock, patch

from app.dhan.auth import DhanAuth
from app.dhan.historical import HistoricalDataManager
from app.dhan.instruments import InstrumentManager
from app.dhan.live_feed import LiveMarketFeed
from app.notifications.telegram import TelegramNotifier
from app.storage.models import Direction, Signal


def test_dhan_binary_packet_unpacking():
    feed = LiveMarketFeed()

    # Pack a mock Dhan Ticker binary packet:
    # Header (8 bytes):
    # - resp_code = 2 (int8)
    # - msg_len = 16 (int16)
    # - exch_seg = 1 (int8)
    # - sec_id = 2885 (int32)
    # Payload:
    # - ltp = 2500.50 (float32)
    # - ltt = 1727764200 (uint32)
    header = struct.pack("<bhbi", 2, 16, 1, 2885)
    payload = struct.pack("<fI", 2500.50, 1727764200)
    packet = header + payload

    tick = feed._parse_binary_packet(packet)
    assert tick is not None
    assert tick.security_id == "2885"
    assert tick.ltp == 2500.50


def test_scrip_master_parsing(tmp_path):
    csv_file = tmp_path / "mock_scrip.csv"
    csv_content = """SEM_EXM_EXCH_ID,SEM_SEGMENT,SEM_SMST_SECURITY_ID,SEM_INSTRUMENT_NAME,SEM_TRADING_SYMBOL,SEM_CUSTOM_SYMBOL,SEM_LOT_UNITS,SEM_TICK_SIZE
NSE,E,2885,EQUITY,RELIANCE-EQ,Reliance Industries,1,0.05
NSE,E,11536,EQUITY,TCS-EQ,Tata Consultancy,1,0.05
BSE,E,500325,EQUITY,RELIANCE,Reliance Industries,1,0.05
"""
    csv_file.write_text(csv_content, encoding="utf-8")

    im = InstrumentManager()
    im.cache_path = csv_file
    im.load_and_parse()

    assert im.get_security_id("RELIANCE") == "2885"
    assert im.get_security_id("TCS") == "11536"
    assert im.get_symbol("2885") == "RELIANCE"
    # BSE scrip excluded because filter targets NSE Equity
    assert im.get_symbol("500325") is None


@pytest.mark.asyncio
async def test_dhan_auth_validation_success():
    from unittest.mock import MagicMock
    auth = DhanAuth(client_id="1000000001", access_token="mock_valid_token")

    with patch("httpx.AsyncClient.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"dhanClientId": "1000000001", "status": "Active"}
        mock_get.return_value = mock_resp

        profile = await auth.get_profile()
        assert profile["dhanClientId"] == "1000000001"
        is_valid = await auth.is_token_valid()
        assert is_valid is True


@pytest.mark.asyncio
async def test_dhan_auth_expired_token():
    from unittest.mock import AsyncMock, MagicMock
    auth = DhanAuth(client_id="1000000001", access_token="mock_expired_token")

    with patch("httpx.AsyncClient.get") as mock_get, patch("app.dhan.auth.notifier.send_error", new_callable=AsyncMock) as mock_send:
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = "Token Expired"
        mock_get.return_value = mock_resp

        with pytest.raises(PermissionError):
            await auth.get_profile()
        assert mock_send.called


@pytest.mark.asyncio
async def test_telegram_retry_on_rate_limit():
    from unittest.mock import MagicMock
    notifier = TelegramNotifier(bot_token="mock_token", chat_id="123456", max_retries=2)

    with patch("httpx.AsyncClient.post") as mock_post:
        # First call fails with 429, second call succeeds
        resp1 = MagicMock()
        resp1.status_code = 429
        resp1.headers = {"Retry-After": "0"}

        resp2 = MagicMock()
        resp2.status_code = 200

        mock_post.side_effect = [resp1, resp2]

        ok = await notifier.send_message("Test message")
        assert ok is True
        assert mock_post.call_count == 2
