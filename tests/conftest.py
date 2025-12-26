"""
Pytest configuration and shared fixtures.
"""

import pytest
from unittest.mock import AsyncMock, patch


@pytest.fixture
def mock_seedr_client():
    """Mock the Seedr client for testing."""
    with patch("seedr_sonarr.server.seedr_client") as mock:
        mock.get_torrents = AsyncMock(return_value=[])
        mock.add_torrent = AsyncMock(return_value="ABCD1234567890")
        mock.delete_torrent = AsyncMock(return_value=True)
        mock.get_torrent = AsyncMock(return_value=None)
        mock.get_torrent_files = AsyncMock(return_value=[])
        mock.test_connection = AsyncMock(return_value=(True, "Connected"))
        mock._category_mapping = {}
        yield mock


@pytest.fixture
def auth_cookies(client):
    """Get authenticated session cookies."""
    response = client.post(
        "/api/v2/auth/login",
        data={"username": "admin", "password": "adminadmin"}
    )
    return response.cookies
