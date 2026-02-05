"""
Tests for the persistence layer (SQLite database operations).
"""

import asyncio
import os
import tempfile
from datetime import datetime

import pytest

from seedr_sonarr.persistence import (
    PersistenceManager,
    PersistedTorrent,
    PersistedQueuedTorrent,
)


@pytest.fixture
async def persistence():
    """Create a temporary persistence manager for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    pm = PersistenceManager(db_path)
    await pm.initialize()

    yield pm

    await pm.close()
    os.unlink(db_path)


class TestPersistenceInitialization:
    """Test persistence manager initialization."""

    @pytest.mark.asyncio
    async def test_initialize_creates_tables(self):
        """Test that initialization creates all required tables."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            pm = PersistenceManager(db_path)
            await pm.initialize()

            stats = await pm.get_stats()
            assert "torrents" in stats
            assert "queued_torrents" in stats
            assert "categories" in stats
            assert "hash_mappings" in stats
            assert "local_downloads" in stats
            assert "activity_log" in stats

            await pm.close()
        finally:
            os.unlink(db_path)

    @pytest.mark.asyncio
    async def test_initialize_idempotent(self, persistence):
        """Test that initialize can be called multiple times safely."""
        await persistence.initialize()
        await persistence.initialize()
        # Should not raise any errors


class TestTorrentOperations:
    """Test torrent CRUD operations."""

    @pytest.mark.asyncio
    async def test_save_and_get_torrent(self, persistence):
        """Test saving and retrieving a torrent."""
        torrent = PersistedTorrent(
            hash="ABC123",
            seedr_id="12345",
            name="Test Torrent",
            size=1000000,
            category="radarr",
            instance_id="radarr",
            state="downloading",
            phase="downloading_to_seedr",
            seedr_progress=0.5,
            local_progress=0.0,
            added_on=int(datetime.now().timestamp()),
            save_path="/downloads/radarr",
        )

        await persistence.save_torrent(torrent)

        retrieved = await persistence.get_torrent("ABC123")
        assert retrieved is not None
        assert retrieved.hash == "ABC123"
        assert retrieved.name == "Test Torrent"
        assert retrieved.size == 1000000
        assert retrieved.category == "radarr"
        assert retrieved.instance_id == "radarr"
        assert retrieved.seedr_progress == 0.5

    @pytest.mark.asyncio
    async def test_get_all_torrents(self, persistence):
        """Test retrieving all torrents."""
        for i in range(3):
            torrent = PersistedTorrent(
                hash=f"HASH{i}",
                seedr_id=f"{i}",
                name=f"Torrent {i}",
                size=1000 * i,
                category="sonarr",
                instance_id="sonarr",
                state="downloading",
                phase="downloading_to_seedr",
                seedr_progress=0.0,
                local_progress=0.0,
                added_on=int(datetime.now().timestamp()),
                save_path="/downloads/sonarr",
            )
            await persistence.save_torrent(torrent)

        torrents = await persistence.get_torrents()
        assert len(torrents) == 3

    @pytest.mark.asyncio
    async def test_update_torrent(self, persistence):
        """Test updating an existing torrent."""
        torrent = PersistedTorrent(
            hash="UPDATE_TEST",
            seedr_id="1",
            name="Original Name",
            size=1000,
            category="radarr",
            instance_id="radarr",
            state="downloading",
            phase="downloading_to_seedr",
            seedr_progress=0.0,
            local_progress=0.0,
            added_on=int(datetime.now().timestamp()),
            save_path="/downloads",
        )
        await persistence.save_torrent(torrent)

        # Update the torrent
        torrent.name = "Updated Name"
        torrent.seedr_progress = 0.75
        await persistence.save_torrent(torrent)

        retrieved = await persistence.get_torrent("UPDATE_TEST")
        assert retrieved.name == "Updated Name"
        assert retrieved.seedr_progress == 0.75

    @pytest.mark.asyncio
    async def test_delete_torrent(self, persistence):
        """Test deleting a torrent."""
        torrent = PersistedTorrent(
            hash="DELETE_TEST",
            seedr_id="1",
            name="To Delete",
            size=1000,
            category="",
            instance_id="",
            state="completed",
            phase="completed",
            seedr_progress=1.0,
            local_progress=1.0,
            added_on=int(datetime.now().timestamp()),
            save_path="/downloads",
        )
        await persistence.save_torrent(torrent)

        await persistence.delete_torrent("DELETE_TEST")

        retrieved = await persistence.get_torrent("DELETE_TEST")
        assert retrieved is None

    @pytest.mark.asyncio
    async def test_update_torrent_progress(self, persistence):
        """Test updating torrent progress efficiently."""
        torrent = PersistedTorrent(
            hash="PROGRESS_TEST",
            seedr_id="1",
            name="Progress Test",
            size=1000,
            category="",
            instance_id="",
            state="downloading",
            phase="downloading_to_seedr",
            seedr_progress=0.0,
            local_progress=0.0,
            added_on=int(datetime.now().timestamp()),
            save_path="/downloads",
        )
        await persistence.save_torrent(torrent)

        await persistence.update_torrent_progress("PROGRESS_TEST", seedr_progress=0.5)

        retrieved = await persistence.get_torrent("PROGRESS_TEST")
        assert retrieved.seedr_progress == 0.5

    @pytest.mark.asyncio
    async def test_update_torrent_error(self, persistence):
        """Test recording torrent errors."""
        torrent = PersistedTorrent(
            hash="ERROR_TEST",
            seedr_id="1",
            name="Error Test",
            size=1000,
            category="",
            instance_id="",
            state="downloading",
            phase="downloading_to_seedr",
            seedr_progress=0.0,
            local_progress=0.0,
            added_on=int(datetime.now().timestamp()),
            save_path="/downloads",
        )
        await persistence.save_torrent(torrent)

        await persistence.update_torrent_error("ERROR_TEST", "Connection failed")

        retrieved = await persistence.get_torrent("ERROR_TEST")
        assert retrieved.error_count == 1
        assert retrieved.last_error == "Connection failed"

        await persistence.update_torrent_error("ERROR_TEST", "Timeout")

        retrieved = await persistence.get_torrent("ERROR_TEST")
        assert retrieved.error_count == 2
        assert retrieved.last_error == "Timeout"

    @pytest.mark.asyncio
    async def test_clear_torrent_error(self, persistence):
        """Test clearing torrent errors."""
        torrent = PersistedTorrent(
            hash="CLEAR_ERROR_TEST",
            seedr_id="1",
            name="Clear Error Test",
            size=1000,
            category="",
            instance_id="",
            state="error",
            phase="error",
            seedr_progress=0.0,
            local_progress=0.0,
            added_on=int(datetime.now().timestamp()),
            save_path="/downloads",
            error_count=5,
            last_error="Some error",
        )
        await persistence.save_torrent(torrent)

        await persistence.clear_torrent_error("CLEAR_ERROR_TEST")

        retrieved = await persistence.get_torrent("CLEAR_ERROR_TEST")
        assert retrieved.error_count == 0
        assert retrieved.last_error is None


class TestQueueOperations:
    """Test queue CRUD operations."""

    @pytest.mark.asyncio
    async def test_save_and_get_queued(self, persistence):
        """Test saving and retrieving queued torrents."""
        queued = PersistedQueuedTorrent(
            id="Q001",
            magnet_link="magnet:?xt=urn:btih:abc123",
            torrent_file_b64=None,
            category="radarr",
            instance_id="radarr",
            name="Queued Movie",
            estimated_size=5000000000,
            added_time=datetime.now().timestamp(),
        )
        await persistence.save_queued(queued)

        queue = await persistence.get_queued()
        assert len(queue) == 1
        assert queue[0].id == "Q001"
        assert queue[0].name == "Queued Movie"
        assert queue[0].instance_id == "radarr"

    @pytest.mark.asyncio
    async def test_queue_ordering(self, persistence):
        """Test that queue is ordered by added_time."""
        now = datetime.now().timestamp()

        # Add in non-chronological order
        for i, offset in enumerate([10, 0, 5]):
            queued = PersistedQueuedTorrent(
                id=f"Q{i}",
                magnet_link=f"magnet:?xt=urn:btih:hash{i}",
                torrent_file_b64=None,
                category="",
                instance_id="",
                name=f"Torrent {i}",
                estimated_size=1000,
                added_time=now + offset,
            )
            await persistence.save_queued(queued)

        queue = await persistence.get_queued()
        assert len(queue) == 3
        # Should be ordered by added_time
        assert queue[0].id == "Q1"  # offset 0
        assert queue[1].id == "Q2"  # offset 5
        assert queue[2].id == "Q0"  # offset 10

    @pytest.mark.asyncio
    async def test_delete_queued(self, persistence):
        """Test deleting a queued torrent."""
        queued = PersistedQueuedTorrent(
            id="DELETE_Q",
            magnet_link="magnet:?xt=urn:btih:delete",
            torrent_file_b64=None,
            category="",
            instance_id="",
            name="To Delete",
            estimated_size=1000,
            added_time=datetime.now().timestamp(),
        )
        await persistence.save_queued(queued)

        await persistence.delete_queued("DELETE_Q")

        queue = await persistence.get_queued()
        assert len(queue) == 0

    @pytest.mark.asyncio
    async def test_clear_queue(self, persistence):
        """Test clearing all queued torrents."""
        for i in range(5):
            queued = PersistedQueuedTorrent(
                id=f"CLEAR{i}",
                magnet_link=f"magnet:?xt=urn:btih:clear{i}",
                torrent_file_b64=None,
                category="",
                instance_id="",
                name=f"Clear {i}",
                estimated_size=1000,
                added_time=datetime.now().timestamp(),
            )
            await persistence.save_queued(queued)

        count = await persistence.clear_queue()
        assert count == 5

        queue = await persistence.get_queued()
        assert len(queue) == 0

    @pytest.mark.asyncio
    async def test_update_queued_retry(self, persistence):
        """Test incrementing retry count for queued torrent."""
        queued = PersistedQueuedTorrent(
            id="RETRY_Q",
            magnet_link="magnet:?xt=urn:btih:retry",
            torrent_file_b64=None,
            category="",
            instance_id="",
            name="Retry Test",
            estimated_size=1000,
            added_time=datetime.now().timestamp(),
            retry_count=0,
        )
        await persistence.save_queued(queued)

        await persistence.update_queued_retry("RETRY_Q")
        await persistence.update_queued_retry("RETRY_Q")

        queue = await persistence.get_queued()
        assert queue[0].retry_count == 2
        assert queue[0].last_retry is not None


class TestCategoryOperations:
    """Test category operations."""

    @pytest.mark.asyncio
    async def test_save_and_get_category(self, persistence):
        """Test saving and retrieving categories."""
        await persistence.save_category("radarr", "/downloads/radarr", "radarr")
        await persistence.save_category("sonarr", "/downloads/sonarr", "sonarr")

        categories = await persistence.get_categories()
        assert len(categories) == 2
        assert "radarr" in categories
        assert categories["radarr"].save_path == "/downloads/radarr"
        assert categories["radarr"].instance_id == "radarr"

    @pytest.mark.asyncio
    async def test_delete_category(self, persistence):
        """Test deleting a category."""
        await persistence.save_category("to_delete", "/downloads/delete", "default")
        await persistence.delete_category("to_delete")

        categories = await persistence.get_categories()
        assert "to_delete" not in categories


class TestHashMappingOperations:
    """Test hash mapping operations."""

    @pytest.mark.asyncio
    async def test_save_and_get_hash_mappings(self, persistence):
        """Test saving and retrieving hash mappings."""
        await persistence.save_hash_mapping("QUEUE001", "REAL_HASH_1")
        await persistence.save_hash_mapping("QUEUE002", "REAL_HASH_2")

        mappings = await persistence.get_hash_mappings()
        assert len(mappings) == 2
        assert mappings["QUEUE001"] == "REAL_HASH_1"
        assert mappings["QUEUE002"] == "REAL_HASH_2"

    @pytest.mark.asyncio
    async def test_delete_hash_mapping(self, persistence):
        """Test deleting a hash mapping."""
        await persistence.save_hash_mapping("TO_DELETE", "REAL")
        await persistence.delete_hash_mapping("TO_DELETE")

        mappings = await persistence.get_hash_mappings()
        assert "TO_DELETE" not in mappings

    @pytest.mark.asyncio
    async def test_delete_hash_mappings_by_real(self, persistence):
        """Test deleting all mappings pointing to a real hash."""
        await persistence.save_hash_mapping("Q1", "REAL_ABC")
        await persistence.save_hash_mapping("Q2", "REAL_ABC")
        await persistence.save_hash_mapping("Q3", "REAL_XYZ")

        await persistence.delete_hash_mappings_by_real("REAL_ABC")

        mappings = await persistence.get_hash_mappings()
        assert "Q1" not in mappings
        assert "Q2" not in mappings
        assert "Q3" in mappings


class TestLocalDownloadOperations:
    """Test local download tracking operations."""

    @pytest.mark.asyncio
    async def test_mark_and_get_local_downloads(self, persistence):
        """Test marking and retrieving local downloads."""
        await persistence.mark_local_download("HASH1")
        await persistence.mark_local_download("HASH2")

        downloads = await persistence.get_local_downloads()
        assert len(downloads) == 2
        assert "HASH1" in downloads
        assert "HASH2" in downloads

    @pytest.mark.asyncio
    async def test_delete_local_download(self, persistence):
        """Test removing a local download."""
        await persistence.mark_local_download("TO_REMOVE")
        await persistence.delete_local_download("TO_REMOVE")

        downloads = await persistence.get_local_downloads()
        assert "TO_REMOVE" not in downloads


class TestActivityLogOperations:
    """Test activity log operations."""

    @pytest.mark.asyncio
    async def test_log_and_get_activity(self, persistence):
        """Test logging and retrieving activity."""
        await persistence.log_activity(
            action="torrent_added",
            torrent_hash="ABC123",
            torrent_name="Test Movie",
            category="radarr",
            details="Added via API",
            level="INFO",
        )

        logs = await persistence.get_activity_log(limit=10)
        assert len(logs) == 1
        assert logs[0].action == "torrent_added"
        assert logs[0].torrent_hash == "ABC123"
        assert logs[0].level == "INFO"

    @pytest.mark.asyncio
    async def test_activity_log_filtering(self, persistence):
        """Test filtering activity logs."""
        await persistence.log_activity("info_action", level="INFO")
        await persistence.log_activity("warning_action", level="WARNING")
        await persistence.log_activity("error_action", level="ERROR")

        # Filter by level
        error_logs = await persistence.get_activity_log(level="ERROR")
        assert len(error_logs) == 1
        assert error_logs[0].action == "error_action"

        warning_logs = await persistence.get_activity_log(level="WARNING")
        assert len(warning_logs) == 2  # WARNING and ERROR

    @pytest.mark.asyncio
    async def test_activity_log_by_hash(self, persistence):
        """Test filtering activity logs by torrent hash."""
        await persistence.log_activity("action1", torrent_hash="HASH_A")
        await persistence.log_activity("action2", torrent_hash="HASH_B")
        await persistence.log_activity("action3", torrent_hash="HASH_A")

        logs = await persistence.get_activity_log(torrent_hash="HASH_A")
        assert len(logs) == 2
        assert all(log.torrent_hash == "HASH_A" for log in logs)

    @pytest.mark.asyncio
    async def test_prune_activity_log(self, persistence):
        """Test pruning old activity log entries."""
        # Add many entries
        for i in range(100):
            await persistence.log_activity(f"action_{i}")

        # Prune to 50
        deleted = await persistence.prune_activity_log(max_entries=50)
        assert deleted == 50

        logs = await persistence.get_activity_log(limit=100)
        assert len(logs) == 50


class TestUtilityOperations:
    """Test utility operations."""

    @pytest.mark.asyncio
    async def test_get_stats(self, persistence):
        """Test getting database statistics."""
        # Add some data
        torrent = PersistedTorrent(
            hash="STATS_TEST",
            seedr_id="1",
            name="Stats Test",
            size=1000,
            category="",
            instance_id="",
            state="completed",
            phase="completed",
            seedr_progress=1.0,
            local_progress=1.0,
            added_on=int(datetime.now().timestamp()),
            save_path="/downloads",
        )
        await persistence.save_torrent(torrent)
        await persistence.save_category("test", "/test", "test")
        await persistence.log_activity("test_action")

        stats = await persistence.get_stats()
        assert stats["torrents"] == 1
        assert stats["categories"] == 1
        assert stats["activity_log"] == 1

    @pytest.mark.asyncio
    async def test_vacuum(self, persistence):
        """Test database vacuum operation."""
        # Just verify it doesn't raise
        await persistence.vacuum()


class TestNameHashMappingOperations:
    """Test name hash mapping operations for robust category recovery."""

    @pytest.mark.asyncio
    async def test_save_and_get_name_hash_mapping(self, persistence):
        """Test saving and retrieving a name hash mapping."""
        await persistence.save_name_hash_mapping(
            normalized_name="test movie 2024",
            original_name="Test.Movie.2024",
            torrent_hash="ABC123",
            category="radarr",
            instance_id="radarr",
            magnet_hash="DEADBEEF1234",
        )

        mapping = await persistence.get_name_hash_mapping("test movie 2024")
        assert mapping is not None
        assert mapping.normalized_name == "test movie 2024"
        assert mapping.original_name == "Test.Movie.2024"
        assert mapping.torrent_hash == "ABC123"
        assert mapping.category == "radarr"
        assert mapping.instance_id == "radarr"
        assert mapping.magnet_hash == "DEADBEEF1234"
        assert mapping.is_active == 1

    @pytest.mark.asyncio
    async def test_get_name_hash_mapping_with_instance(self, persistence):
        """Test retrieving mapping with instance filter."""
        # Add two mappings with same name but different instances
        await persistence.save_name_hash_mapping(
            normalized_name="same name",
            original_name="Same Name",
            torrent_hash="HASH1",
            category="radarr",
            instance_id="radarr",
        )
        await persistence.save_name_hash_mapping(
            normalized_name="same name",
            original_name="Same Name",
            torrent_hash="HASH2",
            category="sonarr",
            instance_id="sonarr",
        )

        # Get with specific instance
        mapping = await persistence.get_name_hash_mapping("same name", instance_id="sonarr")
        assert mapping is not None
        assert mapping.torrent_hash == "HASH2"
        assert mapping.instance_id == "sonarr"

        # Get with different instance
        mapping = await persistence.get_name_hash_mapping("same name", instance_id="radarr")
        assert mapping is not None
        assert mapping.torrent_hash == "HASH1"
        assert mapping.instance_id == "radarr"

    @pytest.mark.asyncio
    async def test_get_name_hash_mapping_by_magnet(self, persistence):
        """Test retrieving mapping by magnet hash."""
        await persistence.save_name_hash_mapping(
            normalized_name="torrent with magnet",
            original_name="Torrent With Magnet",
            torrent_hash="HASH123",
            category="radarr",
            instance_id="radarr",
            magnet_hash="MAGNETABC123",
        )

        mapping = await persistence.get_name_hash_mapping_by_magnet("MAGNETABC123")
        assert mapping is not None
        assert mapping.torrent_hash == "HASH123"
        assert mapping.magnet_hash == "MAGNETABC123"

    @pytest.mark.asyncio
    async def test_get_all_name_hash_mappings(self, persistence):
        """Test retrieving all active mappings."""
        for i in range(3):
            await persistence.save_name_hash_mapping(
                normalized_name=f"torrent {i}",
                original_name=f"Torrent {i}",
                torrent_hash=f"HASH{i}",
                category="radarr",
                instance_id="radarr",
            )

        mappings = await persistence.get_all_name_hash_mappings()
        assert len(mappings) == 3

    @pytest.mark.asyncio
    async def test_update_name_hash_mapping_hash(self, persistence):
        """Test updating the hash in a mapping (for hash transitions)."""
        await persistence.save_name_hash_mapping(
            normalized_name="transitioning torrent",
            original_name="Transitioning Torrent",
            torrent_hash="OLD_HASH",
            category="radarr",
            instance_id="radarr",
        )

        # Update hash
        count = await persistence.update_name_hash_mapping_hash("OLD_HASH", "NEW_HASH")
        assert count == 1

        # Verify update
        mapping = await persistence.get_name_hash_mapping("transitioning torrent")
        assert mapping.torrent_hash == "NEW_HASH"

    @pytest.mark.asyncio
    async def test_deactivate_name_hash_mappings(self, persistence):
        """Test deactivating mappings when torrent is deleted."""
        await persistence.save_name_hash_mapping(
            normalized_name="to delete",
            original_name="To Delete",
            torrent_hash="DELETE_HASH",
            category="radarr",
            instance_id="radarr",
        )

        # Deactivate
        count = await persistence.deactivate_name_hash_mappings("DELETE_HASH")
        assert count == 1

        # Should not be returned anymore
        mapping = await persistence.get_name_hash_mapping("to delete")
        assert mapping is None

    @pytest.mark.asyncio
    async def test_save_updates_existing_mapping(self, persistence):
        """Test that saving a mapping with same name/instance updates it."""
        await persistence.save_name_hash_mapping(
            normalized_name="update test",
            original_name="Update Test",
            torrent_hash="ORIGINAL_HASH",
            category="radarr",
            instance_id="radarr",
        )

        # Save again with same name/instance but different hash
        await persistence.save_name_hash_mapping(
            normalized_name="update test",
            original_name="Update Test V2",
            torrent_hash="UPDATED_HASH",
            category="radarr-4k",
            instance_id="radarr",
        )

        # Should only have one mapping
        mappings = await persistence.get_all_name_hash_mappings()
        assert len(mappings) == 1
        assert mappings[0].torrent_hash == "UPDATED_HASH"
        assert mappings[0].category == "radarr-4k"

    @pytest.mark.asyncio
    async def test_name_hash_mapping_in_stats(self, persistence):
        """Test that name_hash_mappings table is included in stats."""
        await persistence.save_name_hash_mapping(
            normalized_name="stats test",
            original_name="Stats Test",
            torrent_hash="STATS_HASH",
            category="radarr",
            instance_id="radarr",
        )

        stats = await persistence.get_stats()
        assert "name_hash_mappings" in stats
        assert stats["name_hash_mappings"] == 1
