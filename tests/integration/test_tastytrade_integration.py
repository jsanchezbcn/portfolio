from __future__ import annotations

import os

import pytest


@pytest.mark.integration
def test_tastytrade_credentials_available_for_integration() -> None:
    username = os.getenv("TASTYTRADE_USERNAME") or os.getenv("TASTYWORKS_USER")
    password = os.getenv("TASTYTRADE_PASSWORD") or os.getenv("TASTYWORKS_PASS")

    if not username or not password:
        pytest.skip("Tastytrade credentials not configured for integration test")

    assert isinstance(username, str) and len(username) > 0
    assert isinstance(password, str) and len(password) > 0
