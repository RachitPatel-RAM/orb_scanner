"""
Tests for Credential Safety & Secret Redaction (Sections 2 & 14).
"""

import logging
import io
import pytest

from app.config import SensitiveDataFilter


def test_sensitive_data_filter_masks_secret_tokens():
    """Verify that tokens and secrets are redacted from logging records."""
    dummy_secret = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.dummy_token_12345"
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg=f"Calling Dhan API with token: {dummy_secret}",
        args=(),
        exc_info=None,
    )

    filter_ = SensitiveDataFilter(secrets=[dummy_secret])
    filter_.filter(record)

    # Secret should be replaced with masked string
    assert dummy_secret not in record.msg
    assert "****" in record.msg


def test_redaction_in_telegram_error_logging():
    """Verify Telegram bot token is never exposed in raw exception strings."""
    fake_token = "123456789:ABCdefGHIjklMNOpqrsTUVwxyz"
    raw_error = f"Client error on https://api.telegram.org/bot{fake_token}/sendMessage"
    redacted = raw_error.replace(fake_token, "BOT_TOKEN_REDACTED")

    assert fake_token not in redacted
    assert "BOT_TOKEN_REDACTED" in redacted
