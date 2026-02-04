"""
Tests for the QNAP Download Station client.
"""

import asyncio
import base64
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import aiohttp

from seedr_sonarr.qnap_client import (
    QnapDownloadStationClient,
    QnapTask,
    QnapTaskStatus,
)


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def qnap_config():
    """Create QNAP client configuration for tests."""
    return {
        "host": "192.168.1.100",
        "username": "admin",
        "password": "password123",
        "port": 8080,
        "use_https": False,
        "verify_ssl": True,
    }


@pytest.fixture
def qnap_client(qnap_config):
    """Create a QNAP client instance for testing."""
    return QnapDownloadStationClient(**qnap_config)


@pytest.fixture
def mock_response():
    """Create a factory for mock aiohttp responses."""
    def _create_response(json_data, status=200, reason="OK"):
        response = AsyncMock()
        response.status = status
        response.reason = reason
        response.json = AsyncMock(return_value=json_data)
        return response
    return _create_response


@pytest.fixture
def mock_session(mock_response):
    """Create a mock aiohttp session."""
    session = AsyncMock(spec=aiohttp.ClientSession)
    session.closed = False
    return session


# ============================================================================
# Initialization and Configuration Tests
# ============================================================================

class TestQnapClientInitialization:
    """Test QNAP client initialization and configuration."""

    def test_initialization_with_defaults(self):
        """Test client initialization with default values."""
        client = QnapDownloadStationClient(
            host="192.168.1.1",
            username="admin",
            password="password",
        )
        assert client.host == "192.168.1.1"
        assert client.port == 8080
        assert client.username == "admin"
        assert client.password == "password"
        assert client.use_https is False
        assert client.verify_ssl is True
        assert client._base_url == "http://192.168.1.1:8080/downloadstation/V4"
        assert client._sid is None
        assert client._session is None

    def test_initialization_with_https(self):
        """Test client initialization with HTTPS enabled."""
        client = QnapDownloadStationClient(
            host="nas.example.com",
            username="admin",
            password="secret",
            port=443,
            use_https=True,
            verify_ssl=False,
        )
        assert client.use_https is True
        assert client.verify_ssl is False
        assert client._base_url == "https://nas.example.com:443/downloadstation/V4"

    def test_initialization_with_custom_port(self):
        """Test client initialization with custom port."""
        client = QnapDownloadStationClient(
            host="192.168.1.100",
            username="admin",
            password="password",
            port=9443,
        )
        assert client.port == 9443
        assert "9443" in client._base_url


class TestQnapClientSession:
    """Test session management."""

    @pytest.mark.asyncio
    async def test_get_session_creates_new_session(self, qnap_client):
        """Test that _get_session creates a new session when none exists."""
        with patch("aiohttp.ClientSession") as mock_client_session:
            mock_session = AsyncMock()
            mock_session.closed = False
            mock_client_session.return_value = mock_session

            session = await qnap_client._get_session()

            mock_client_session.assert_called_once()
            assert qnap_client._session is mock_session

    @pytest.mark.asyncio
    async def test_get_session_reuses_existing_session(self, qnap_client):
        """Test that _get_session reuses an existing open session."""
        mock_session = AsyncMock()
        mock_session.closed = False
        qnap_client._session = mock_session

        with patch("aiohttp.ClientSession") as mock_client_session:
            session = await qnap_client._get_session()

            mock_client_session.assert_not_called()
            assert session is mock_session

    @pytest.mark.asyncio
    async def test_get_session_recreates_closed_session(self, qnap_client):
        """Test that _get_session recreates a closed session."""
        old_session = AsyncMock()
        old_session.closed = True
        qnap_client._session = old_session

        with patch("aiohttp.ClientSession") as mock_client_session:
            new_session = AsyncMock()
            new_session.closed = False
            mock_client_session.return_value = new_session

            session = await qnap_client._get_session()

            mock_client_session.assert_called_once()
            assert session is new_session


# ============================================================================
# Authentication Tests
# ============================================================================

class TestQnapClientLogin:
    """Test QNAP client login functionality."""

    @pytest.mark.asyncio
    async def test_login_success(self, qnap_client, mock_response):
        """Test successful login."""
        login_response = mock_response({"error": 0, "sid": "session123"})

        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.post = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=login_response)))

        with patch.object(qnap_client, "_get_session", return_value=mock_session):
            result = await qnap_client.login()

            assert result is True
            assert qnap_client._sid == "session123"

    @pytest.mark.asyncio
    async def test_login_password_encoding(self, qnap_client, mock_response):
        """Test that password is base64 encoded during login."""
        login_response = mock_response({"error": 0, "sid": "session123"})

        mock_session = AsyncMock()
        mock_session.closed = False
        post_context = AsyncMock(__aenter__=AsyncMock(return_value=login_response))
        mock_session.post = MagicMock(return_value=post_context)

        with patch.object(qnap_client, "_get_session", return_value=mock_session):
            await qnap_client.login()

            # Check the call arguments
            call_args = mock_session.post.call_args
            data = call_args.kwargs.get("data", {})

            expected_password = base64.b64encode(qnap_client.password.encode()).decode()
            assert data.get("pass") == expected_password

    @pytest.mark.asyncio
    async def test_login_no_sid_returned(self, qnap_client, mock_response):
        """Test login failure when no SID is returned."""
        login_response = mock_response({"error": 0})  # No sid field

        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.post = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=login_response)))

        with patch.object(qnap_client, "_get_session", return_value=mock_session):
            with pytest.raises(Exception) as exc_info:
                await qnap_client.login()

            assert "No session ID returned" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_login_already_authenticated(self, qnap_client):
        """Test that login returns immediately if already authenticated."""
        qnap_client._sid = "existing_session"

        with patch.object(qnap_client, "_request") as mock_request:
            result = await qnap_client.login()

            assert result is True
            mock_request.assert_not_called()

    @pytest.mark.asyncio
    async def test_login_api_error(self, qnap_client, mock_response):
        """Test login with API error response."""
        error_response = mock_response({"error": 1, "reason": "Invalid credentials"})

        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.post = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=error_response)))

        with patch.object(qnap_client, "_get_session", return_value=mock_session):
            with pytest.raises(Exception) as exc_info:
                await qnap_client.login()

            assert "Invalid credentials" in str(exc_info.value)


class TestQnapClientLogout:
    """Test QNAP client logout functionality."""

    @pytest.mark.asyncio
    async def test_logout_success(self, qnap_client):
        """Test successful logout."""
        qnap_client._sid = "session123"

        with patch.object(qnap_client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = {"error": 0}

            await qnap_client.logout()

            mock_request.assert_called_once_with("Misc", "Logout")
            assert qnap_client._sid is None

    @pytest.mark.asyncio
    async def test_logout_without_session(self, qnap_client):
        """Test logout when not logged in."""
        qnap_client._sid = None

        with patch.object(qnap_client, "_request", new_callable=AsyncMock) as mock_request:
            await qnap_client.logout()

            mock_request.assert_not_called()

    @pytest.mark.asyncio
    async def test_logout_with_error(self, qnap_client):
        """Test logout handles errors gracefully."""
        qnap_client._sid = "session123"

        with patch.object(qnap_client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.side_effect = Exception("Network error")

            # Should not raise, just log warning
            await qnap_client.logout()

            assert qnap_client._sid is None


class TestQnapClientClose:
    """Test QNAP client close functionality."""

    @pytest.mark.asyncio
    async def test_close_with_session(self, qnap_client):
        """Test closing client with active session."""
        mock_session = AsyncMock()
        mock_session.closed = False
        qnap_client._session = mock_session
        qnap_client._sid = "session123"

        with patch.object(qnap_client, "logout", new_callable=AsyncMock):
            await qnap_client.close()

            mock_session.close.assert_called_once()
            assert qnap_client._session is None

    @pytest.mark.asyncio
    async def test_close_without_session(self, qnap_client):
        """Test closing client without active session."""
        qnap_client._session = None
        qnap_client._sid = None

        # Should not raise
        await qnap_client.close()


# ============================================================================
# Request Method Tests
# ============================================================================

class TestQnapClientRequest:
    """Test the internal _request method."""

    @pytest.mark.asyncio
    async def test_request_adds_sid_when_authenticated(self, qnap_client, mock_response):
        """Test that _request adds SID to authenticated requests."""
        qnap_client._sid = "session123"

        response = mock_response({"error": 0, "data": "test"})

        mock_session = AsyncMock()
        mock_session.closed = False
        post_context = AsyncMock(__aenter__=AsyncMock(return_value=response))
        mock_session.post = MagicMock(return_value=post_context)

        with patch.object(qnap_client, "_get_session", return_value=mock_session):
            await qnap_client._request("Task", "Query", {"limit": 10})

            call_args = mock_session.post.call_args
            data = call_args.kwargs.get("data", {})
            assert data.get("sid") == "session123"
            assert data.get("limit") == 10

    @pytest.mark.asyncio
    async def test_request_auto_login(self, qnap_client, mock_response):
        """Test that _request triggers login when not authenticated."""
        qnap_client._sid = None

        response = mock_response({"error": 0, "data": []})

        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.post = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=response)))

        with patch.object(qnap_client, "_get_session", return_value=mock_session):
            with patch.object(qnap_client, "login", new_callable=AsyncMock) as mock_login:
                await qnap_client._request("Task", "Query", require_auth=True)

                mock_login.assert_called_once()

    @pytest.mark.asyncio
    async def test_request_http_error(self, qnap_client, mock_response):
        """Test _request handling of HTTP errors."""
        qnap_client._sid = "session123"

        response = mock_response({}, status=500, reason="Internal Server Error")

        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.post = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=response)))

        with patch.object(qnap_client, "_get_session", return_value=mock_session):
            with pytest.raises(Exception) as exc_info:
                await qnap_client._request("Task", "Query")

            assert "HTTP 500" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_request_session_expired_retry(self, qnap_client, mock_response):
        """Test _request retries on session expiry."""
        qnap_client._sid = "expired_session"

        # First response: session expired, second: success
        expired_response = mock_response({"error": 2, "reason": "Session expired"})
        success_response = mock_response({"error": 0, "data": "success"})

        mock_session = AsyncMock()
        mock_session.closed = False

        call_count = 0
        def make_post_response(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return AsyncMock(__aenter__=AsyncMock(return_value=expired_response))
            return AsyncMock(__aenter__=AsyncMock(return_value=success_response))

        mock_session.post = MagicMock(side_effect=make_post_response)

        with patch.object(qnap_client, "_get_session", return_value=mock_session):
            with patch.object(qnap_client, "login", new_callable=AsyncMock) as mock_login:
                # After session expiry, login should be called
                result = await qnap_client._request("Task", "Query")

                assert mock_login.call_count >= 1

    @pytest.mark.asyncio
    async def test_request_client_error(self, qnap_client):
        """Test _request handling of aiohttp client errors."""
        qnap_client._sid = "session123"

        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.post = MagicMock(side_effect=aiohttp.ClientError("Connection refused"))

        with patch.object(qnap_client, "_get_session", return_value=mock_session):
            with pytest.raises(aiohttp.ClientError):
                await qnap_client._request("Task", "Query")


# ============================================================================
# Download Task Operations Tests
# ============================================================================

class TestQnapClientAddDownload:
    """Test add_download functionality."""

    @pytest.mark.asyncio
    async def test_add_download_success(self, qnap_client):
        """Test successfully adding a download."""
        with patch.object(qnap_client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = {"error": 0, "id": "task123"}

            success, task_id = await qnap_client.add_download(
                url="https://example.com/file.zip",
                temp_folder="Download",
                dest_folder="/Movies",
            )

            assert success is True
            assert task_id == "task123"
            mock_request.assert_called_once_with(
                "Task", "AddUrl",
                {"url": "https://example.com/file.zip", "temp": "Download", "move": "/Movies"}
            )

    @pytest.mark.asyncio
    async def test_add_download_without_dest_folder(self, qnap_client):
        """Test adding download without destination folder."""
        with patch.object(qnap_client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = {"error": 0}

            success, task_id = await qnap_client.add_download(
                url="https://example.com/file.zip",
            )

            assert success is True
            call_args = mock_request.call_args
            params = call_args[0][2]
            assert "move" not in params

    @pytest.mark.asyncio
    async def test_add_download_with_task_id_field(self, qnap_client):
        """Test add_download with 'task_id' field instead of 'id'."""
        with patch.object(qnap_client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = {"error": 0, "task_id": "task456"}

            success, task_id = await qnap_client.add_download(
                url="https://example.com/file.zip",
            )

            assert success is True
            assert task_id == "task456"

    @pytest.mark.asyncio
    async def test_add_download_failure(self, qnap_client):
        """Test add_download failure."""
        with patch.object(qnap_client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.side_effect = Exception("Network error")

            success, task_id = await qnap_client.add_download(
                url="https://example.com/file.zip",
            )

            assert success is False
            assert task_id is None


class TestQnapClientGetTaskStatus:
    """Test get_task_status functionality."""

    @pytest.mark.asyncio
    async def test_get_task_status_downloading(self, qnap_client):
        """Test getting status of a downloading task."""
        with patch.object(qnap_client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = {
                "error": 0,
                "data": {
                    "source": "https://example.com/file.zip",
                    "size": 1000000,
                    "have": 500000,
                    "status": 2,  # DOWNLOADING
                    "down_rate": 1024,
                    "move": "/Movies",
                }
            }

            task = await qnap_client.get_task_status("task123")

            assert task is not None
            assert task.id == "task123"
            assert task.name == "https://example.com/file.zip"
            assert task.size == 1000000
            assert task.downloaded == 500000
            assert task.progress == 0.5
            assert task.status == QnapTaskStatus.DOWNLOADING
            assert task.download_speed == 1024
            assert task.destination == "/Movies"

    @pytest.mark.asyncio
    async def test_get_task_status_completed(self, qnap_client):
        """Test getting status of a completed task."""
        with patch.object(qnap_client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = {
                "error": 0,
                "data": {
                    "source": "file.zip",
                    "size": 1000000,
                    "have": 1000000,
                    "status": 5,  # COMPLETED
                }
            }

            task = await qnap_client.get_task_status("task123")

            assert task.status == QnapTaskStatus.COMPLETED
            assert task.progress == 1.0

    @pytest.mark.asyncio
    async def test_get_task_status_error_state(self, qnap_client):
        """Test getting status of a task in error state."""
        with patch.object(qnap_client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = {
                "error": 0,
                "data": {
                    "source": "file.zip",
                    "size": 1000000,
                    "have": 0,
                    "status": 6,  # ERROR
                    "error": "File not found",
                }
            }

            task = await qnap_client.get_task_status("task123")

            assert task.status == QnapTaskStatus.ERROR
            assert task.error_message == "File not found"

    @pytest.mark.asyncio
    async def test_get_task_status_unknown_status_code(self, qnap_client):
        """Test handling of unknown status code."""
        with patch.object(qnap_client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = {
                "error": 0,
                "data": {
                    "source": "file.zip",
                    "size": 1000,
                    "have": 0,
                    "status": 999,  # Unknown status
                }
            }

            task = await qnap_client.get_task_status("task123")

            # Should default to ERROR for unknown status
            assert task.status == QnapTaskStatus.ERROR

    @pytest.mark.asyncio
    async def test_get_task_status_zero_size(self, qnap_client):
        """Test handling of zero size task."""
        with patch.object(qnap_client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = {
                "error": 0,
                "data": {
                    "source": "file.zip",
                    "size": 0,
                    "have": 0,
                    "status": 1,
                }
            }

            task = await qnap_client.get_task_status("task123")

            assert task.progress == 0.0  # Division by zero avoided

    @pytest.mark.asyncio
    async def test_get_task_status_failure(self, qnap_client):
        """Test get_task_status when request fails."""
        with patch.object(qnap_client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.side_effect = Exception("Network error")

            task = await qnap_client.get_task_status("task123")

            assert task is None


class TestQnapClientQueryTasks:
    """Test query_tasks functionality."""

    @pytest.mark.asyncio
    async def test_query_tasks_success(self, qnap_client):
        """Test querying multiple tasks."""
        with patch.object(qnap_client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = {
                "error": 0,
                "data": [
                    {
                        "id": "1",
                        "name": "File1.zip",
                        "source": "https://example.com/file1.zip",
                        "size": 1000000,
                        "have": 500000,
                        "status": 2,
                        "down_rate": 1024,
                    },
                    {
                        "id": "2",
                        "name": "File2.zip",
                        "source": "https://example.com/file2.zip",
                        "size": 2000000,
                        "have": 2000000,
                        "status": 5,
                    },
                ]
            }

            tasks = await qnap_client.query_tasks(limit=50)

            assert len(tasks) == 2
            assert tasks[0].id == "1"
            assert tasks[0].status == QnapTaskStatus.DOWNLOADING
            assert tasks[1].id == "2"
            assert tasks[1].status == QnapTaskStatus.COMPLETED

            mock_request.assert_called_once_with("Task", "Query", {"limit": 50})

    @pytest.mark.asyncio
    async def test_query_tasks_empty(self, qnap_client):
        """Test querying when no tasks exist."""
        with patch.object(qnap_client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = {"error": 0, "data": []}

            tasks = await qnap_client.query_tasks()

            assert tasks == []

    @pytest.mark.asyncio
    async def test_query_tasks_failure(self, qnap_client):
        """Test query_tasks when request fails."""
        with patch.object(qnap_client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.side_effect = Exception("Connection error")

            tasks = await qnap_client.query_tasks()

            assert tasks == []

    @pytest.mark.asyncio
    async def test_query_tasks_default_limit(self, qnap_client):
        """Test query_tasks uses default limit of 100."""
        with patch.object(qnap_client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = {"error": 0, "data": []}

            await qnap_client.query_tasks()

            mock_request.assert_called_once_with("Task", "Query", {"limit": 100})

    @pytest.mark.asyncio
    async def test_query_tasks_unknown_status_code(self, qnap_client):
        """Test query_tasks handling of unknown status code."""
        with patch.object(qnap_client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = {
                "error": 0,
                "data": [
                    {
                        "id": "1",
                        "name": "File1.zip",
                        "source": "https://example.com/file1.zip",
                        "size": 1000000,
                        "have": 500000,
                        "status": 999,  # Unknown status code
                    },
                ]
            }

            tasks = await qnap_client.query_tasks()

            assert len(tasks) == 1
            # Unknown status should default to ERROR
            assert tasks[0].status == QnapTaskStatus.ERROR


class TestQnapClientRemoveTask:
    """Test remove_task functionality."""

    @pytest.mark.asyncio
    async def test_remove_task_success(self, qnap_client):
        """Test successfully removing a task."""
        with patch.object(qnap_client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = {"error": 0}

            result = await qnap_client.remove_task("task123")

            assert result is True
            mock_request.assert_called_once_with("Task", "Remove", {"id": "task123"})

    @pytest.mark.asyncio
    async def test_remove_task_failure(self, qnap_client):
        """Test remove_task when request fails."""
        with patch.object(qnap_client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.side_effect = Exception("Task not found")

            result = await qnap_client.remove_task("task123")

            assert result is False


class TestQnapClientPauseTask:
    """Test pause_task functionality."""

    @pytest.mark.asyncio
    async def test_pause_task_success(self, qnap_client):
        """Test successfully pausing a task."""
        with patch.object(qnap_client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = {"error": 0}

            result = await qnap_client.pause_task("task123")

            assert result is True
            mock_request.assert_called_once_with("Task", "Pause", {"id": "task123"})

    @pytest.mark.asyncio
    async def test_pause_task_failure(self, qnap_client):
        """Test pause_task when request fails."""
        with patch.object(qnap_client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.side_effect = Exception("Cannot pause")

            result = await qnap_client.pause_task("task123")

            assert result is False


class TestQnapClientStartTask:
    """Test start_task functionality."""

    @pytest.mark.asyncio
    async def test_start_task_success(self, qnap_client):
        """Test successfully starting/resuming a task."""
        with patch.object(qnap_client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = {"error": 0}

            result = await qnap_client.start_task("task123")

            assert result is True
            mock_request.assert_called_once_with("Task", "Start", {"id": "task123"})

    @pytest.mark.asyncio
    async def test_start_task_failure(self, qnap_client):
        """Test start_task when request fails."""
        with patch.object(qnap_client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.side_effect = Exception("Cannot start")

            result = await qnap_client.start_task("task123")

            assert result is False


# ============================================================================
# Environment and Directory Tests
# ============================================================================

class TestQnapClientEnvironment:
    """Test get_environment functionality."""

    @pytest.mark.asyncio
    async def test_get_environment_success(self, qnap_client):
        """Test getting environment info."""
        with patch.object(qnap_client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = {
                "error": 0,
                "data": {
                    "version": "4.3.0",
                    "build": "20231001",
                    "device": "TS-453D",
                }
            }

            env = await qnap_client.get_environment()

            assert env["version"] == "4.3.0"
            assert env["device"] == "TS-453D"
            mock_request.assert_called_once_with("Misc", "Env")

    @pytest.mark.asyncio
    async def test_get_environment_failure(self, qnap_client):
        """Test get_environment when request fails."""
        with patch.object(qnap_client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.side_effect = Exception("Connection error")

            env = await qnap_client.get_environment()

            assert env == {}


class TestQnapClientDirectories:
    """Test get_directories functionality."""

    @pytest.mark.asyncio
    async def test_get_directories_success(self, qnap_client):
        """Test getting available directories."""
        with patch.object(qnap_client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = {
                "error": 0,
                "data": ["/Download", "/Multimedia/Movies", "/Multimedia/TV"]
            }

            dirs = await qnap_client.get_directories()

            assert len(dirs) == 3
            assert "/Download" in dirs
            mock_request.assert_called_once_with("Misc", "Dir")

    @pytest.mark.asyncio
    async def test_get_directories_failure(self, qnap_client):
        """Test get_directories when request fails."""
        with patch.object(qnap_client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.side_effect = Exception("Connection error")

            dirs = await qnap_client.get_directories()

            assert dirs == []


# ============================================================================
# Connection Test
# ============================================================================

class TestQnapClientTestConnection:
    """Test test_connection functionality."""

    @pytest.mark.asyncio
    async def test_connection_success(self, qnap_client):
        """Test successful connection test."""
        with patch.object(qnap_client, "login", new_callable=AsyncMock) as mock_login:
            with patch.object(qnap_client, "get_environment", new_callable=AsyncMock) as mock_env:
                mock_login.return_value = True
                mock_env.return_value = {"version": "4.3.0"}

                success, message = await qnap_client.test_connection()

                assert success is True
                assert "4.3.0" in message

    @pytest.mark.asyncio
    async def test_connection_login_failure(self, qnap_client):
        """Test connection test when login fails."""
        with patch.object(qnap_client, "login", new_callable=AsyncMock) as mock_login:
            mock_login.side_effect = Exception("Invalid credentials")

            success, message = await qnap_client.test_connection()

            assert success is False
            assert "Invalid credentials" in message


# ============================================================================
# Wait for Completion Tests
# ============================================================================

class TestQnapClientWaitForCompletion:
    """Test wait_for_completion functionality."""

    @pytest.mark.asyncio
    async def test_wait_for_completion_immediate_complete(self, qnap_client):
        """Test waiting for a task that's already complete."""
        complete_task = QnapTask(
            id="task123",
            name="file.zip",
            source_url="https://example.com/file.zip",
            size=1000000,
            downloaded=1000000,
            progress=1.0,
            status=QnapTaskStatus.COMPLETED,
        )

        with patch.object(qnap_client, "get_task_status", new_callable=AsyncMock) as mock_status:
            mock_status.return_value = complete_task

            result = await qnap_client.wait_for_completion("task123", poll_interval=0.1)

            assert result is True

    @pytest.mark.asyncio
    async def test_wait_for_completion_progresses_to_complete(self, qnap_client):
        """Test waiting for a task that completes over time."""
        downloading_task = QnapTask(
            id="task123",
            name="file.zip",
            source_url="https://example.com/file.zip",
            size=1000000,
            downloaded=500000,
            progress=0.5,
            status=QnapTaskStatus.DOWNLOADING,
            download_speed=1024,
        )

        complete_task = QnapTask(
            id="task123",
            name="file.zip",
            source_url="https://example.com/file.zip",
            size=1000000,
            downloaded=1000000,
            progress=1.0,
            status=QnapTaskStatus.COMPLETED,
        )

        call_count = 0
        async def mock_get_status(task_id):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return downloading_task
            return complete_task

        with patch.object(qnap_client, "get_task_status", side_effect=mock_get_status):
            result = await qnap_client.wait_for_completion("task123", poll_interval=0.01)

            assert result is True
            assert call_count >= 3

    @pytest.mark.asyncio
    async def test_wait_for_completion_task_error(self, qnap_client):
        """Test waiting for a task that errors out."""
        error_task = QnapTask(
            id="task123",
            name="file.zip",
            source_url="https://example.com/file.zip",
            size=1000000,
            downloaded=0,
            progress=0.0,
            status=QnapTaskStatus.ERROR,
            error_message="File not found",
        )

        with patch.object(qnap_client, "get_task_status", new_callable=AsyncMock) as mock_status:
            mock_status.return_value = error_task

            result = await qnap_client.wait_for_completion("task123", poll_interval=0.1)

            assert result is False

    @pytest.mark.asyncio
    async def test_wait_for_completion_task_not_found(self, qnap_client):
        """Test waiting when task is not found."""
        with patch.object(qnap_client, "get_task_status", new_callable=AsyncMock) as mock_status:
            mock_status.return_value = None

            result = await qnap_client.wait_for_completion("task123", poll_interval=0.1)

            assert result is False

    @pytest.mark.asyncio
    async def test_wait_for_completion_timeout(self, qnap_client):
        """Test waiting for a task that times out."""
        downloading_task = QnapTask(
            id="task123",
            name="file.zip",
            source_url="https://example.com/file.zip",
            size=1000000,
            downloaded=500000,
            progress=0.5,
            status=QnapTaskStatus.DOWNLOADING,
        )

        with patch.object(qnap_client, "get_task_status", new_callable=AsyncMock) as mock_status:
            mock_status.return_value = downloading_task

            result = await qnap_client.wait_for_completion(
                "task123",
                poll_interval=0.01,
                timeout=0.05,
            )

            assert result is False

    @pytest.mark.asyncio
    async def test_wait_for_completion_with_progress_callback(self, qnap_client):
        """Test progress callback is called."""
        downloading_task = QnapTask(
            id="task123",
            name="file.zip",
            source_url="https://example.com/file.zip",
            size=1000000,
            downloaded=500000,
            progress=0.5,
            status=QnapTaskStatus.DOWNLOADING,
            download_speed=1024,
        )

        complete_task = QnapTask(
            id="task123",
            name="file.zip",
            source_url="https://example.com/file.zip",
            size=1000000,
            downloaded=1000000,
            progress=1.0,
            status=QnapTaskStatus.COMPLETED,
        )

        call_count = 0
        async def mock_get_status(task_id):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                return downloading_task
            return complete_task

        progress_updates = []
        def progress_callback(progress, speed):
            progress_updates.append((progress, speed))

        with patch.object(qnap_client, "get_task_status", side_effect=mock_get_status):
            result = await qnap_client.wait_for_completion(
                "task123",
                poll_interval=0.01,
                progress_callback=progress_callback,
            )

            assert result is True
            assert len(progress_updates) >= 2
            assert progress_updates[0] == (0.5, 1024)


# ============================================================================
# QnapTaskStatus Enum Tests
# ============================================================================

class TestQnapTaskStatus:
    """Test QnapTaskStatus enum."""

    def test_status_values(self):
        """Test status enum values match QNAP API."""
        assert QnapTaskStatus.WAITING.value == 1
        assert QnapTaskStatus.DOWNLOADING.value == 2
        assert QnapTaskStatus.PAUSED.value == 3
        assert QnapTaskStatus.COMPLETED.value == 5
        assert QnapTaskStatus.ERROR.value == 6
        assert QnapTaskStatus.SEEDING.value == 100
        assert QnapTaskStatus.CHECKING.value == 101


# ============================================================================
# QnapTask Dataclass Tests
# ============================================================================

class TestQnapTask:
    """Test QnapTask dataclass."""

    def test_task_creation(self):
        """Test creating a QnapTask."""
        task = QnapTask(
            id="123",
            name="test.zip",
            source_url="https://example.com/test.zip",
            size=1000000,
            downloaded=500000,
            progress=0.5,
            status=QnapTaskStatus.DOWNLOADING,
            download_speed=1024,
            error_message=None,
            destination="/Downloads",
        )

        assert task.id == "123"
        assert task.name == "test.zip"
        assert task.source_url == "https://example.com/test.zip"
        assert task.size == 1000000
        assert task.downloaded == 500000
        assert task.progress == 0.5
        assert task.status == QnapTaskStatus.DOWNLOADING
        assert task.download_speed == 1024
        assert task.error_message is None
        assert task.destination == "/Downloads"

    def test_task_default_values(self):
        """Test QnapTask default values."""
        task = QnapTask(
            id="123",
            name="test.zip",
            source_url="https://example.com/test.zip",
            size=1000000,
            downloaded=500000,
            progress=0.5,
            status=QnapTaskStatus.WAITING,
        )

        assert task.download_speed == 0
        assert task.error_message is None
        assert task.destination == ""


# ============================================================================
# SSL/TLS Configuration Tests
# ============================================================================

class TestQnapClientSSL:
    """Test SSL/TLS configuration."""

    @pytest.mark.asyncio
    async def test_https_with_ssl_verification(self):
        """Test HTTPS client with SSL verification enabled."""
        client = QnapDownloadStationClient(
            host="nas.example.com",
            username="admin",
            password="password",
            port=443,
            use_https=True,
            verify_ssl=True,
        )

        with patch("aiohttp.TCPConnector") as mock_connector:
            with patch("aiohttp.ClientSession") as mock_session_class:
                mock_session = AsyncMock()
                mock_session.closed = False
                mock_session_class.return_value = mock_session

                await client._get_session()

                # verify_ssl=True should pass ssl=True to TCPConnector
                mock_connector.assert_called_once_with(ssl=True)

    @pytest.mark.asyncio
    async def test_https_without_ssl_verification(self):
        """Test HTTPS client with SSL verification disabled."""
        client = QnapDownloadStationClient(
            host="nas.example.com",
            username="admin",
            password="password",
            port=443,
            use_https=True,
            verify_ssl=False,
        )

        with patch("aiohttp.TCPConnector") as mock_connector:
            with patch("aiohttp.ClientSession") as mock_session_class:
                mock_session = AsyncMock()
                mock_session.closed = False
                mock_session_class.return_value = mock_session

                await client._get_session()

                # verify_ssl=False should pass ssl=False to TCPConnector
                mock_connector.assert_called_once_with(ssl=False)

    @pytest.mark.asyncio
    async def test_http_no_ssl_connector(self):
        """Test HTTP client doesn't configure SSL."""
        client = QnapDownloadStationClient(
            host="192.168.1.100",
            username="admin",
            password="password",
            use_https=False,
        )

        with patch("aiohttp.TCPConnector") as mock_connector:
            with patch("aiohttp.ClientSession") as mock_session_class:
                mock_session = AsyncMock()
                mock_session.closed = False
                mock_session_class.return_value = mock_session

                await client._get_session()

                # HTTP should pass ssl=None
                mock_connector.assert_called_once_with(ssl=None)
