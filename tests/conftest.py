"""
Pytest configuration and shared fixtures.
"""

import pytest
import time
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


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
        mock._queued_torrents = []
        mock._local_downloads = set()
        mock._state_manager = None
        mock._retry_handler = None
        mock._circuit_breaker = None
        yield mock


@pytest.fixture
def client(mock_seedr_client):
    """Create test client with mocked Seedr."""
    from seedr_sonarr.server import app
    from fastapi.testclient import TestClient
    return TestClient(app)


@pytest.fixture
def auth_cookies(client):
    """Get authenticated session cookies."""
    response = client.post(
        "/api/v2/auth/login",
        data={"username": "admin", "password": "adminadmin"}
    )
    return response.cookies


# ============================================================================
# Persistence Fixtures
# ============================================================================

@pytest.fixture
def temp_db_path(tmp_path):
    """Create a temporary database path."""
    return str(tmp_path / "test.db")


@pytest.fixture
async def persistence_manager(temp_db_path):
    """Create an initialized persistence manager."""
    from seedr_sonarr.persistence import PersistenceManager

    manager = PersistenceManager(temp_db_path)
    await manager.initialize()
    yield manager
    await manager.close()


# ============================================================================
# State Manager Fixtures
# ============================================================================

@pytest.fixture
async def state_manager():
    """Create a state manager without persistence."""
    from seedr_sonarr.state import StateManager

    manager = StateManager(persistence=None, persist_enabled=False)
    await manager.initialize()
    yield manager
    await manager.close()


@pytest.fixture
async def state_manager_with_persistence(temp_db_path):
    """Create a state manager with persistence."""
    from seedr_sonarr.persistence import PersistenceManager
    from seedr_sonarr.state import StateManager

    persistence = PersistenceManager(temp_db_path)
    manager = StateManager(persistence=persistence, persist_enabled=True)
    await manager.initialize()
    yield manager
    await manager.close()


# ============================================================================
# Retry/Circuit Breaker Fixtures
# ============================================================================

@pytest.fixture
def retry_config():
    """Create a retry config with fast settings for tests."""
    from seedr_sonarr.retry import RetryConfig

    return RetryConfig(
        max_attempts=3,
        initial_delay=0.01,  # Fast for tests
        max_delay=0.1,
        jitter=False,
    )


@pytest.fixture
def retry_handler(retry_config):
    """Create a retry handler for tests."""
    from seedr_sonarr.retry import RetryHandler

    return RetryHandler(retry_config)


@pytest.fixture
def circuit_config():
    """Create a circuit breaker config for tests."""
    from seedr_sonarr.retry import CircuitBreakerConfig

    return CircuitBreakerConfig(
        failure_threshold=3,
        success_threshold=2,
        reset_timeout=0.1,  # Fast for tests
        half_open_max_calls=2,
    )


@pytest.fixture
def circuit_breaker(circuit_config):
    """Create a circuit breaker for tests."""
    from seedr_sonarr.retry import CircuitBreaker

    return CircuitBreaker(circuit_config, name="test")


@pytest.fixture
def resilient_executor(retry_config, circuit_config):
    """Create a resilient executor for tests."""
    from seedr_sonarr.retry import ResilientExecutor

    return ResilientExecutor(retry_config, circuit_config, name="test")


# ============================================================================
# Sample Data Fixtures
# ============================================================================

@pytest.fixture
def sample_torrent_state():
    """Create a sample torrent state."""
    from seedr_sonarr.state import TorrentState, TorrentPhase

    return TorrentState(
        hash="ABC123",
        seedr_id="12345",
        name="Test Movie (2024)",
        size=1500000000,  # 1.5GB
        category="radarr",
        instance_id="radarr",
        phase=TorrentPhase.QUEUED_STORAGE,
        seedr_progress=0.0,
        local_progress=0.0,
        added_on=int(time.time()),
        save_path="/downloads/movies",
    )


@pytest.fixture
def sample_queued_state():
    """Create a sample queued torrent state."""
    from seedr_sonarr.state import QueuedTorrentState

    return QueuedTorrentState(
        id="queue-1",
        magnet_link="magnet:?xt=urn:btih:ABC123&dn=Test+Movie",
        torrent_file=None,
        category="radarr",
        instance_id="radarr",
        name="Test Movie (2024)",
        estimated_size=1500000000,
        added_time=time.time(),
    )


@pytest.fixture
def sample_category_state():
    """Create a sample category state."""
    from seedr_sonarr.state import CategoryState

    return CategoryState(
        name="radarr-4k",
        save_path="/downloads/movies-4k",
        instance_id="radarr-4k",
    )


@pytest.fixture
def sample_persisted_torrent():
    """Create a sample persisted torrent."""
    from seedr_sonarr.persistence import PersistedTorrent

    return PersistedTorrent(
        hash="ABC123",
        seedr_id="12345",
        name="Test Movie (2024)",
        size=1500000000,
        category="radarr",
        instance_id="radarr",
        state="downloading",
        phase="downloading_to_seedr",
        seedr_progress=0.5,
        local_progress=0.0,
        added_on=int(time.time()),
        save_path="/downloads/movies",
    )


@pytest.fixture
def sample_persisted_queued():
    """Create a sample persisted queued torrent."""
    from seedr_sonarr.persistence import PersistedQueuedTorrent

    return PersistedQueuedTorrent(
        id="queue-1",
        magnet_link="magnet:?xt=urn:btih:ABC123&dn=Test+Movie",
        torrent_file_b64=None,
        category="radarr",
        instance_id="radarr",
        name="Test Movie (2024)",
        estimated_size=1500000000,
        added_time=time.time(),
    )


# ============================================================================
# Logging Fixtures
# ============================================================================

@pytest.fixture
def activity_log_handler():
    """Create an activity log handler for tests."""
    from seedr_sonarr.logging_config import ActivityLogHandler

    return ActivityLogHandler(max_entries=100)


@pytest.fixture
def clean_logging():
    """Clean up logging handlers before and after test."""
    import logging

    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level

    yield

    # Restore original state
    for handler in root.handlers[:]:
        root.removeHandler(handler)
    for handler in original_handlers:
        root.addHandler(handler)
    root.setLevel(original_level)


# ============================================================================
# Async Event Loop Configuration
# ============================================================================

@pytest.fixture(scope="session")
def event_loop_policy():
    """Use the default event loop policy."""
    return asyncio.DefaultEventLoopPolicy()
