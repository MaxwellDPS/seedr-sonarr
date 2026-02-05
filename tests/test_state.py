"""
Tests for State Manager (seedr_sonarr/state.py)
"""

import asyncio
import pytest
import time
from unittest.mock import AsyncMock, MagicMock, patch

from seedr_sonarr.state import (
    StateManager,
    TorrentState,
    TorrentPhase,
    QueuedTorrentState,
    CategoryState,
    PHASE_TO_QBIT_STATE,
    extract_instance_id,
)


class TestTorrentPhase:
    """Tests for TorrentPhase enum and mappings."""

    def test_all_phases_have_qbit_mapping(self):
        """Verify all phases have a qBittorrent state mapping."""
        for phase in TorrentPhase:
            assert phase in PHASE_TO_QBIT_STATE
            assert isinstance(PHASE_TO_QBIT_STATE[phase], str)

    def test_phase_values(self):
        """Test phase enum values."""
        assert TorrentPhase.QUEUED_STORAGE.value == "queued_storage"
        assert TorrentPhase.FETCHING_METADATA.value == "fetching_metadata"
        assert TorrentPhase.DOWNLOADING_TO_SEEDR.value == "downloading_to_seedr"
        assert TorrentPhase.SEEDR_COMPLETE.value == "seedr_complete"
        assert TorrentPhase.DOWNLOADING_TO_LOCAL.value == "downloading_to_local"
        assert TorrentPhase.COMPLETED.value == "completed"
        assert TorrentPhase.ERROR.value == "error"


class TestTorrentState:
    """Tests for TorrentState dataclass."""

    def test_qbit_state_property(self):
        """Test qBittorrent state mapping."""
        torrent = TorrentState(
            hash="ABC123",
            seedr_id="12345",
            name="Test Movie",
            size=1000000,
            category="radarr",
            instance_id="radarr",
            phase=TorrentPhase.DOWNLOADING_TO_SEEDR,
            seedr_progress=0.5,
            local_progress=0.0,
            added_on=int(time.time()),
            save_path="/downloads",
        )
        assert torrent.qbit_state == "downloading"

    def test_combined_progress(self):
        """Test combined progress calculation."""
        torrent = TorrentState(
            hash="ABC123",
            seedr_id="12345",
            name="Test Movie",
            size=1000000,
            category="radarr",
            instance_id="radarr",
            phase=TorrentPhase.DOWNLOADING_TO_SEEDR,
            seedr_progress=0.5,
            local_progress=0.0,
            added_on=int(time.time()),
            save_path="/downloads",
        )
        # 0.5 * 0.5 + 0.0 * 0.5 = 0.25
        assert torrent.combined_progress == 0.25

        # Test with both progresses
        torrent.seedr_progress = 1.0
        torrent.local_progress = 0.5
        # 1.0 * 0.5 + 0.5 * 0.5 = 0.75
        assert torrent.combined_progress == 0.75

        # Test completed
        torrent.local_progress = 1.0
        # 1.0 * 0.5 + 1.0 * 0.5 = 1.0
        assert torrent.combined_progress == 1.0

    def test_eta_completed(self):
        """Test ETA for completed torrent."""
        torrent = TorrentState(
            hash="ABC123",
            seedr_id="12345",
            name="Test Movie",
            size=1000000,
            category="radarr",
            instance_id="radarr",
            phase=TorrentPhase.COMPLETED,
            seedr_progress=1.0,
            local_progress=1.0,
            added_on=int(time.time()),
            save_path="/downloads",
        )
        assert torrent.eta == 0

    def test_eta_no_speed(self):
        """Test ETA with no download speed."""
        torrent = TorrentState(
            hash="ABC123",
            seedr_id="12345",
            name="Test Movie",
            size=1000000,
            category="radarr",
            instance_id="radarr",
            phase=TorrentPhase.DOWNLOADING_TO_SEEDR,
            seedr_progress=0.5,
            local_progress=0.0,
            added_on=int(time.time()),
            save_path="/downloads",
            download_speed=0,
        )
        assert torrent.eta == 8640000  # Unknown

    def test_eta_calculation(self):
        """Test ETA calculation."""
        torrent = TorrentState(
            hash="ABC123",
            seedr_id="12345",
            name="Test Movie",
            size=1000000,  # 1MB
            category="radarr",
            instance_id="radarr",
            phase=TorrentPhase.DOWNLOADING_TO_SEEDR,
            seedr_progress=0.5,  # 25% total (50% of first half)
            local_progress=0.0,
            added_on=int(time.time()),
            save_path="/downloads",
            download_speed=10000,  # 10KB/s
        )
        # Remaining: 1000000 * (1 - 0.25) = 750000 bytes
        # ETA: 750000 / 10000 = 75 seconds
        assert torrent.eta == 75


class TestExtractInstanceId:
    """Tests for extract_instance_id function."""

    def test_empty_category(self):
        """Test empty category returns default."""
        assert extract_instance_id("") == "default"
        assert extract_instance_id(None) == "default"

    def test_radarr_categories(self):
        """Test radarr category extraction."""
        assert extract_instance_id("radarr") == "radarr"
        assert extract_instance_id("radarr-4k") == "radarr-4k"
        assert extract_instance_id("radarr-anime") == "radarr-anime"
        assert extract_instance_id("Radarr") == "radarr"
        assert extract_instance_id("RADARR") == "radarr"

    def test_sonarr_categories(self):
        """Test sonarr category extraction."""
        assert extract_instance_id("sonarr") == "sonarr"
        assert extract_instance_id("sonarr-4k") == "sonarr-4k"
        assert extract_instance_id("sonarr-anime") == "sonarr-anime"
        assert extract_instance_id("Sonarr") == "sonarr"

    def test_other_arrs(self):
        """Test other arr instances."""
        assert extract_instance_id("lidarr") == "lidarr"
        assert extract_instance_id("readarr") == "readarr"
        assert extract_instance_id("prowlarr") == "prowlarr"

    def test_category_with_prefix(self):
        """Test categories with prefix before arr name."""
        assert extract_instance_id("tv-sonarr") == "sonarr"
        assert extract_instance_id("movies-radarr") == "radarr"

    def test_unknown_category(self):
        """Test unknown category returns as-is lowercased."""
        assert extract_instance_id("custom") == "custom"
        assert extract_instance_id("Custom-Category") == "custom-category"


class TestStateManagerInitialization:
    """Tests for StateManager initialization."""

    @pytest.mark.asyncio
    async def test_init_without_persistence(self):
        """Test initialization without persistence."""
        manager = StateManager(persistence=None, persist_enabled=False)
        await manager.initialize()
        assert manager._initialized
        await manager.close()

    @pytest.mark.asyncio
    async def test_init_with_persistence(self, tmp_path):
        """Test initialization with persistence."""
        from seedr_sonarr.persistence import PersistenceManager

        db_path = tmp_path / "test.db"
        persistence = PersistenceManager(str(db_path))

        manager = StateManager(persistence=persistence, persist_enabled=True)
        await manager.initialize()
        assert manager._initialized
        await manager.close()

    @pytest.mark.asyncio
    async def test_double_initialization(self):
        """Test that double initialization is safe."""
        manager = StateManager(persistence=None, persist_enabled=False)
        await manager.initialize()
        await manager.initialize()  # Should not raise
        assert manager._initialized
        await manager.close()


class TestStateManagerTorrentOperations:
    """Tests for torrent operations in StateManager."""

    @pytest.fixture
    async def manager(self):
        """Create a state manager for tests."""
        manager = StateManager(persistence=None, persist_enabled=False)
        await manager.initialize()
        yield manager
        await manager.close()

    @pytest.fixture
    def sample_torrent(self):
        """Create a sample torrent state."""
        return TorrentState(
            hash="ABC123",
            seedr_id="12345",
            name="Test Movie",
            size=1000000,
            category="radarr",
            instance_id="radarr",
            phase=TorrentPhase.QUEUED_STORAGE,
            seedr_progress=0.0,
            local_progress=0.0,
            added_on=int(time.time()),
            save_path="/downloads",
        )

    @pytest.mark.asyncio
    async def test_add_torrent(self, manager, sample_torrent):
        """Test adding a torrent."""
        await manager.add_torrent(sample_torrent)
        torrent = await manager.get_torrent("ABC123")
        assert torrent is not None
        assert torrent.name == "Test Movie"
        assert torrent.phase == TorrentPhase.QUEUED_STORAGE

    @pytest.mark.asyncio
    async def test_get_torrents(self, manager, sample_torrent):
        """Test getting all torrents."""
        await manager.add_torrent(sample_torrent)

        torrent2 = TorrentState(
            hash="DEF456",
            seedr_id="67890",
            name="Test Show",
            size=2000000,
            category="sonarr",
            instance_id="sonarr",
            phase=TorrentPhase.DOWNLOADING_TO_SEEDR,
            seedr_progress=0.5,
            local_progress=0.0,
            added_on=int(time.time()),
            save_path="/downloads",
        )
        await manager.add_torrent(torrent2)

        torrents = await manager.get_torrents()
        assert len(torrents) == 2

    @pytest.mark.asyncio
    async def test_delete_torrent(self, manager, sample_torrent):
        """Test deleting a torrent."""
        await manager.add_torrent(sample_torrent)
        result = await manager.delete_torrent("ABC123")
        assert result is True

        torrent = await manager.get_torrent("ABC123")
        assert torrent is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent_torrent(self, manager):
        """Test deleting a nonexistent torrent."""
        result = await manager.delete_torrent("NONEXISTENT")
        assert result is False

    @pytest.mark.asyncio
    async def test_update_phase(self, manager, sample_torrent):
        """Test updating torrent phase."""
        await manager.add_torrent(sample_torrent)

        await manager.update_phase(
            "ABC123",
            TorrentPhase.DOWNLOADING_TO_SEEDR,
            seedr_progress=0.5,
        )

        torrent = await manager.get_torrent("ABC123")
        assert torrent.phase == TorrentPhase.DOWNLOADING_TO_SEEDR
        assert torrent.seedr_progress == 0.5

    @pytest.mark.asyncio
    async def test_update_progress(self, manager, sample_torrent):
        """Test updating torrent progress."""
        await manager.add_torrent(sample_torrent)

        await manager.update_progress(
            "ABC123",
            seedr_progress=0.75,
            local_progress=0.0,
            download_speed=50000,
        )

        torrent = await manager.get_torrent("ABC123")
        assert torrent.seedr_progress == 0.75
        assert torrent.download_speed == 50000

    @pytest.mark.asyncio
    async def test_record_error(self, manager, sample_torrent):
        """Test recording an error."""
        await manager.add_torrent(sample_torrent)

        await manager.record_error("ABC123", "Connection timeout")

        torrent = await manager.get_torrent("ABC123")
        assert torrent.phase == TorrentPhase.ERROR
        assert torrent.error_count == 1
        assert torrent.last_error == "Connection timeout"
        assert torrent.last_error_time is not None

    @pytest.mark.asyncio
    async def test_clear_error(self, manager, sample_torrent):
        """Test clearing an error."""
        await manager.add_torrent(sample_torrent)
        await manager.record_error("ABC123", "Connection timeout")
        await manager.clear_error("ABC123")

        torrent = await manager.get_torrent("ABC123")
        assert torrent.error_count == 0
        assert torrent.last_error is None

    @pytest.mark.asyncio
    async def test_phase_change_callback(self, manager, sample_torrent):
        """Test phase change callback."""
        callback = AsyncMock()
        await manager.on_phase_change(callback)

        await manager.add_torrent(sample_torrent)
        await manager.update_phase("ABC123", TorrentPhase.DOWNLOADING_TO_SEEDR)

        callback.assert_called_once_with(
            "ABC123",
            TorrentPhase.QUEUED_STORAGE,
            TorrentPhase.DOWNLOADING_TO_SEEDR,
        )

    @pytest.mark.asyncio
    async def test_completion_callback(self, manager, sample_torrent):
        """Test completion callback."""
        callback = AsyncMock()
        await manager.on_complete(callback)

        await manager.add_torrent(sample_torrent)
        await manager.update_phase("ABC123", TorrentPhase.COMPLETED)

        callback.assert_called_once_with("ABC123")

    @pytest.mark.asyncio
    async def test_error_callback(self, manager, sample_torrent):
        """Test error callback."""
        callback = AsyncMock()
        await manager.on_error(callback)

        await manager.add_torrent(sample_torrent)
        await manager.record_error("ABC123", "Test error")

        callback.assert_called_once_with("ABC123", "Test error")


class TestStateManagerQueueOperations:
    """Tests for queue operations in StateManager."""

    @pytest.fixture
    async def manager(self):
        """Create a state manager for tests."""
        manager = StateManager(persistence=None, persist_enabled=False)
        await manager.initialize()
        yield manager
        await manager.close()

    @pytest.fixture
    def sample_queued(self):
        """Create a sample queued torrent."""
        return QueuedTorrentState(
            id="queue-1",
            magnet_link="magnet:?xt=urn:btih:ABC123",
            torrent_file=None,
            category="radarr",
            instance_id="radarr",
            name="Test Movie",
            estimated_size=1000000,
            added_time=time.time(),
        )

    @pytest.mark.asyncio
    async def test_add_to_queue(self, manager, sample_queued):
        """Test adding to queue."""
        await manager.add_to_queue(sample_queued)
        assert manager.queue_size == 1

    @pytest.mark.asyncio
    async def test_get_queue(self, manager, sample_queued):
        """Test getting queue."""
        await manager.add_to_queue(sample_queued)
        queue = await manager.get_queue()
        assert len(queue) == 1
        assert queue[0].name == "Test Movie"

    @pytest.mark.asyncio
    async def test_pop_queue(self, manager, sample_queued):
        """Test popping from queue."""
        await manager.add_to_queue(sample_queued)
        item = await manager.pop_queue()
        assert item is not None
        assert item.id == "queue-1"
        assert manager.queue_size == 0

    @pytest.mark.asyncio
    async def test_pop_empty_queue(self, manager):
        """Test popping from empty queue."""
        item = await manager.pop_queue()
        assert item is None

    @pytest.mark.asyncio
    async def test_peek_queue(self, manager, sample_queued):
        """Test peeking at queue."""
        await manager.add_to_queue(sample_queued)
        item = await manager.peek_queue()
        assert item is not None
        assert item.id == "queue-1"
        assert manager.queue_size == 1  # Item still in queue

    @pytest.mark.asyncio
    async def test_remove_from_queue(self, manager, sample_queued):
        """Test removing specific item from queue."""
        await manager.add_to_queue(sample_queued)
        result = await manager.remove_from_queue("queue-1")
        assert result is True
        assert manager.queue_size == 0

    @pytest.mark.asyncio
    async def test_remove_nonexistent_from_queue(self, manager):
        """Test removing nonexistent item from queue."""
        result = await manager.remove_from_queue("nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_requeue(self, manager, sample_queued):
        """Test requeuing an item."""
        await manager.add_to_queue(sample_queued)
        item = await manager.pop_queue()

        # Requeue
        item.retry_count += 1
        await manager.requeue(item)

        queue = await manager.get_queue()
        assert len(queue) == 1
        assert queue[0].retry_count == 1

    @pytest.mark.asyncio
    async def test_clear_queue(self, manager, sample_queued):
        """Test clearing queue."""
        await manager.add_to_queue(sample_queued)
        await manager.add_to_queue(QueuedTorrentState(
            id="queue-2",
            magnet_link="magnet:?xt=urn:btih:DEF456",
            torrent_file=None,
            category="sonarr",
            instance_id="sonarr",
            name="Test Show",
            estimated_size=2000000,
            added_time=time.time(),
        ))

        count = await manager.clear_queue()
        assert count == 2
        assert manager.queue_size == 0

    @pytest.mark.asyncio
    async def test_queue_ordering(self, manager):
        """Test queue maintains FIFO order."""
        for i in range(5):
            await manager.add_to_queue(QueuedTorrentState(
                id=f"queue-{i}",
                magnet_link=f"magnet:?xt=urn:btih:HASH{i}",
                torrent_file=None,
                category="radarr",
                instance_id="radarr",
                name=f"Movie {i}",
                estimated_size=1000000,
                added_time=time.time(),
            ))

        for i in range(5):
            item = await manager.pop_queue()
            assert item.id == f"queue-{i}"


class TestStateManagerCategoryOperations:
    """Tests for category operations in StateManager."""

    @pytest.fixture
    async def manager(self):
        """Create a state manager for tests."""
        manager = StateManager(persistence=None, persist_enabled=False)
        await manager.initialize()
        yield manager
        await manager.close()

    @pytest.mark.asyncio
    async def test_add_category(self, manager):
        """Test adding a category."""
        category = CategoryState(
            name="radarr-4k",
            save_path="/downloads/movies-4k",
            instance_id="radarr-4k",
        )
        await manager.add_category(category)

        result = await manager.get_category("radarr-4k")
        assert result is not None
        assert result.save_path == "/downloads/movies-4k"

    @pytest.mark.asyncio
    async def test_get_categories(self, manager):
        """Test getting all categories."""
        await manager.add_category(CategoryState(
            name="radarr", save_path="/downloads/movies", instance_id="radarr"
        ))
        await manager.add_category(CategoryState(
            name="sonarr", save_path="/downloads/shows", instance_id="sonarr"
        ))

        categories = await manager.get_categories()
        assert len(categories) == 2
        assert "radarr" in categories
        assert "sonarr" in categories

    @pytest.mark.asyncio
    async def test_delete_category(self, manager):
        """Test deleting a category."""
        await manager.add_category(CategoryState(
            name="radarr", save_path="/downloads/movies", instance_id="radarr"
        ))
        result = await manager.delete_category("radarr")
        assert result is True

        category = await manager.get_category("radarr")
        assert category is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent_category(self, manager):
        """Test deleting nonexistent category."""
        result = await manager.delete_category("nonexistent")
        assert result is False


class TestStateManagerHashMappings:
    """Tests for hash mapping operations in StateManager."""

    @pytest.fixture
    async def manager(self):
        """Create a state manager for tests."""
        manager = StateManager(persistence=None, persist_enabled=False)
        await manager.initialize()
        yield manager
        await manager.close()

    @pytest.mark.asyncio
    async def test_add_hash_mapping(self, manager):
        """Test adding hash mapping."""
        await manager.add_hash_mapping("QUEUE123", "REAL456")
        resolved = await manager.resolve_hash("QUEUE123")
        assert resolved == "REAL456"

    @pytest.mark.asyncio
    async def test_resolve_unmapped_hash(self, manager):
        """Test resolving unmapped hash returns same hash."""
        resolved = await manager.resolve_hash("ABC123")
        assert resolved == "ABC123"

    @pytest.mark.asyncio
    async def test_resolve_hash_case_insensitive(self, manager):
        """Test hash resolution is case insensitive."""
        await manager.add_hash_mapping("queue123", "real456")
        resolved = await manager.resolve_hash("QUEUE123")
        assert resolved == "REAL456"

    @pytest.mark.asyncio
    async def test_delete_hash_mapping(self, manager):
        """Test deleting hash mapping."""
        await manager.add_hash_mapping("QUEUE123", "REAL456")
        await manager.delete_hash_mapping("QUEUE123")
        resolved = await manager.resolve_hash("QUEUE123")
        assert resolved == "QUEUE123"

    @pytest.mark.asyncio
    async def test_get_torrent_via_mapping(self, manager):
        """Test getting torrent via hash mapping."""
        torrent = TorrentState(
            hash="REAL456",
            seedr_id="12345",
            name="Test Movie",
            size=1000000,
            category="radarr",
            instance_id="radarr",
            phase=TorrentPhase.QUEUED_STORAGE,
            seedr_progress=0.0,
            local_progress=0.0,
            added_on=int(time.time()),
            save_path="/downloads",
        )
        await manager.add_torrent(torrent)
        await manager.add_hash_mapping("QUEUE123", "REAL456")

        result = await manager.get_torrent("QUEUE123")
        assert result is not None
        assert result.hash == "REAL456"


class TestStateManagerLocalDownloads:
    """Tests for local download tracking in StateManager."""

    @pytest.fixture
    async def manager(self):
        """Create a state manager for tests."""
        manager = StateManager(persistence=None, persist_enabled=False)
        await manager.initialize()
        yield manager
        await manager.close()

    @pytest.mark.asyncio
    async def test_mark_local_download(self, manager):
        """Test marking a torrent as locally downloaded."""
        await manager.mark_local_download("ABC123")
        assert await manager.is_local_download("ABC123") is True

    @pytest.mark.asyncio
    async def test_is_local_download_false(self, manager):
        """Test checking unmarked torrent."""
        assert await manager.is_local_download("ABC123") is False

    @pytest.mark.asyncio
    async def test_remove_local_download(self, manager):
        """Test removing local download mark."""
        await manager.mark_local_download("ABC123")
        await manager.remove_local_download("ABC123")
        assert await manager.is_local_download("ABC123") is False

    @pytest.mark.asyncio
    async def test_local_download_via_mapping(self, manager):
        """Test local download with hash mapping."""
        await manager.add_hash_mapping("QUEUE123", "REAL456")
        await manager.mark_local_download("QUEUE123")

        assert await manager.is_local_download("REAL456") is True
        assert await manager.is_local_download("QUEUE123") is True


class TestStateManagerStats:
    """Tests for statistics in StateManager."""

    @pytest.fixture
    async def manager(self):
        """Create a state manager for tests."""
        manager = StateManager(persistence=None, persist_enabled=False)
        await manager.initialize()
        yield manager
        await manager.close()

    @pytest.mark.asyncio
    async def test_get_stats(self, manager):
        """Test getting statistics."""
        # Add some test data
        await manager.add_torrent(TorrentState(
            hash="ABC123",
            seedr_id="12345",
            name="Test Movie",
            size=1000000,
            category="radarr",
            instance_id="radarr",
            phase=TorrentPhase.DOWNLOADING_TO_SEEDR,
            seedr_progress=0.5,
            local_progress=0.0,
            added_on=int(time.time()),
            save_path="/downloads",
        ))

        await manager.add_to_queue(QueuedTorrentState(
            id="queue-1",
            magnet_link="magnet:?xt=urn:btih:DEF456",
            torrent_file=None,
            category="sonarr",
            instance_id="sonarr",
            name="Test Show",
            estimated_size=2000000,
            added_time=time.time(),
        ))

        await manager.add_category(CategoryState(
            name="radarr", save_path="/downloads/movies", instance_id="radarr"
        ))

        stats = await manager.get_stats()

        assert stats["torrents"] == 1
        assert stats["queued"] == 1
        assert stats["categories"] == 1
        assert stats["by_phase"]["downloading_to_seedr"] == 1
        assert stats["by_instance"]["radarr"] == 1


class TestStateManagerPersistence:
    """Tests for persistence integration in StateManager."""

    @pytest.mark.asyncio
    async def test_load_state_from_persistence(self, tmp_path):
        """Test loading state from persistence."""
        from seedr_sonarr.persistence import PersistenceManager, PersistedTorrent

        db_path = tmp_path / "test.db"
        persistence = PersistenceManager(str(db_path))
        await persistence.initialize()

        # Save data directly to persistence
        await persistence.save_torrent(PersistedTorrent(
            hash="ABC123",
            seedr_id="12345",
            name="Test Movie",
            size=1000000,
            category="radarr",
            instance_id="radarr",
            state="downloading",
            phase="downloading_to_seedr",
            seedr_progress=0.5,
            local_progress=0.0,
            added_on=int(time.time()),
            save_path="/downloads",
        ))

        await persistence.close()

        # Create new persistence and state manager
        persistence2 = PersistenceManager(str(db_path))
        manager = StateManager(persistence=persistence2, persist_enabled=True)
        await manager.initialize()

        # Verify state was loaded
        torrent = await manager.get_torrent("ABC123")
        assert torrent is not None
        assert torrent.name == "Test Movie"
        assert torrent.phase == TorrentPhase.DOWNLOADING_TO_SEEDR

        await manager.close()

    @pytest.mark.asyncio
    async def test_persistence_on_add_torrent(self, tmp_path):
        """Test that adding torrent persists to database."""
        from seedr_sonarr.persistence import PersistenceManager

        db_path = tmp_path / "test.db"
        persistence = PersistenceManager(str(db_path))

        manager = StateManager(persistence=persistence, persist_enabled=True)
        await manager.initialize()

        await manager.add_torrent(TorrentState(
            hash="ABC123",
            seedr_id="12345",
            name="Test Movie",
            size=1000000,
            category="radarr",
            instance_id="radarr",
            phase=TorrentPhase.QUEUED_STORAGE,
            seedr_progress=0.0,
            local_progress=0.0,
            added_on=int(time.time()),
            save_path="/downloads",
        ))

        await manager.close()

        # Reload and verify
        persistence2 = PersistenceManager(str(db_path))
        manager2 = StateManager(persistence=persistence2, persist_enabled=True)
        await manager2.initialize()

        torrent = await manager2.get_torrent("ABC123")
        assert torrent is not None
        assert torrent.name == "Test Movie"

        await manager2.close()
