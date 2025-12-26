"""
Tests for Seedr-Sonarr Proxy
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient


@pytest.fixture
def mock_seedr_client():
    """Mock the Seedr client."""
    with patch("seedr_sonarr.server.seedr_client") as mock:
        mock.get_torrents = AsyncMock(return_value=[])
        mock.test_connection = AsyncMock(return_value=(True, "Connected"))
        mock._category_mapping = {}
        yield mock


@pytest.fixture
def client(mock_seedr_client):
    """Create test client with mocked Seedr."""
    from seedr_sonarr.server import app
    return TestClient(app)


class TestAuthentication:
    """Test authentication endpoints."""

    def test_login_success(self, client):
        """Test successful login."""
        response = client.post(
            "/api/v2/auth/login",
            data={"username": "admin", "password": "adminadmin"}
        )
        assert response.status_code == 200
        assert response.text == "Ok."
        assert "SID" in response.cookies

    def test_login_failure(self, client):
        """Test failed login."""
        response = client.post(
            "/api/v2/auth/login",
            data={"username": "wrong", "password": "wrong"}
        )
        assert response.text == "Fails."


class TestAppEndpoints:
    """Test application endpoints."""

    def test_version(self, client):
        """Test version endpoint."""
        # First login
        login_response = client.post(
            "/api/v2/auth/login",
            data={"username": "admin", "password": "adminadmin"}
        )
        cookies = login_response.cookies

        # Then get version
        response = client.get("/api/v2/app/version", cookies=cookies)
        assert response.status_code == 200
        assert "v4" in response.text

    def test_webapi_version(self, client):
        """Test WebAPI version endpoint."""
        login_response = client.post(
            "/api/v2/auth/login",
            data={"username": "admin", "password": "adminadmin"}
        )
        cookies = login_response.cookies

        response = client.get("/api/v2/app/webapiVersion", cookies=cookies)
        assert response.status_code == 200


class TestTorrentEndpoints:
    """Test torrent management endpoints."""

    def test_torrents_info_empty(self, client, mock_seedr_client):
        """Test getting empty torrent list."""
        login_response = client.post(
            "/api/v2/auth/login",
            data={"username": "admin", "password": "adminadmin"}
        )
        cookies = login_response.cookies

        response = client.get("/api/v2/torrents/info", cookies=cookies)
        assert response.status_code == 200
        assert response.json() == []

    def test_sync_maindata(self, client, mock_seedr_client):
        """Test sync maindata endpoint."""
        login_response = client.post(
            "/api/v2/auth/login",
            data={"username": "admin", "password": "adminadmin"}
        )
        cookies = login_response.cookies

        response = client.get("/api/v2/sync/maindata", cookies=cookies)
        assert response.status_code == 200
        data = response.json()
        assert "torrents" in data
        assert "server_state" in data


class TestHealthCheck:
    """Test health check endpoint."""

    def test_health_check(self, client, mock_seedr_client):
        """Test health endpoint."""
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["seedr_connected"] is True
