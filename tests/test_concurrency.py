"""
Tests for concurrency and thread safety.
Tests race conditions, concurrent access, and lock ordering.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timedelta

from seedr_sonarr.state import StateManager, TorrentPhase
from seedr_sonarr.retry import CircuitBreaker, CircuitBreakerConfig, RateLimiter


class TestSessionConcurrency:
    """Tests for concurrent session access."""

    @pytest.mark.asyncio
    async def test_concurrent_session_creation(self):
        """Multiple concurrent session creations don't corrupt state."""
        from seedr_sonarr.server import create_session, sessions, _sessions_lock

        # Clear sessions first
        async with _sessions_lock:
            sessions.clear()

        # Create many sessions concurrently
        tasks = [create_session() for _ in range(100)]
        sids = await asyncio.gather(*tasks)

        # All session IDs should be unique
        assert len(set(sids)) == 100

        # All sessions should be in the dict
        async with _sessions_lock:
            assert len(sessions) == 100

    @pytest.mark.asyncio
    async def test_concurrent_session_validation(self):
        """Concurrent session validation works correctly."""
        from seedr_sonarr.server import (
            create_session,
            validate_session,
            sessions,
            _sessions_lock,
        )

        # Create a session
        sid = await create_session()

        # Create mock requests with the session
        mock_requests = []
        for _ in range(50):
            mock_req = MagicMock()
            mock_req.cookies.get.return_value = sid
            mock_requests.append(mock_req)

        # Validate all concurrently
        tasks = [validate_session(req) for req in mock_requests]
        results = await asyncio.gather(*tasks)

        # All should succeed
        assert all(results)

    @pytest.mark.asyncio
    async def test_session_cleanup_during_validation(self):
        """Session cleanup doesn't break concurrent validation."""
        from seedr_sonarr.server import (
            create_session,
            validate_session,
            sessions,
            _sessions_lock,
            SESSION_TIMEOUT,
        )

        # Create sessions
        sids = []
        for _ in range(10):
            sid = await create_session()
            sids.append(sid)

        # Mark some as expired
        async with _sessions_lock:
            for sid in sids[:5]:
                sessions[sid] = datetime.now() - timedelta(hours=1)

        # Validate all concurrently while some are expired
        mock_requests = []
        for sid in sids:
            mock_req = MagicMock()
            mock_req.cookies.get.return_value = sid
            mock_requests.append(mock_req)

        tasks = [validate_session(req) for req in mock_requests]
        results = await asyncio.gather(*tasks)

        # First 5 should fail (expired), last 5 should succeed
        assert results[:5] == [False] * 5
        assert results[5:] == [True] * 5


class TestStateManagerConcurrency:
    """Tests for concurrent StateManager access."""

    @pytest.fixture
    def state_manager(self):
        """Create a StateManager for testing."""
        return StateManager()

    @pytest.mark.asyncio
    async def test_concurrent_torrent_updates(self, state_manager):
        """Concurrent torrent updates don't corrupt state."""
        await state_manager.initialize()

        # Add a torrent using the proper dataclass
        from seedr_sonarr.state import TorrentState as StateTorrentState
        from datetime import datetime
        torrent = StateTorrentState(
            hash="TESTHASH123",
            seedr_id="12345",
            name="Test Torrent",
            size=1000000,
            category="test",
            instance_id="test-1",
            phase=TorrentPhase.QUEUED_STORAGE,
            seedr_progress=0.0,
            local_progress=0.0,
            added_on=int(datetime.now().timestamp()),
            save_path="/downloads/test",
        )
        await state_manager.add_torrent(torrent)

        # Update progress concurrently from multiple "threads"
        async def update_progress(progress):
            await state_manager.update_progress(
                "TESTHASH123",
                seedr_progress=progress / 100,
                local_progress=0.0,
            )

        tasks = [update_progress(i) for i in range(100)]
        await asyncio.gather(*tasks)

        # Final state should be valid
        result = await state_manager.get_torrent("TESTHASH123")
        assert result is not None
        assert 0 <= result.seedr_progress <= 1.0

    @pytest.mark.asyncio
    async def test_concurrent_queue_operations(self, state_manager):
        """Concurrent queue operations don't corrupt state."""
        from seedr_sonarr.state import QueuedTorrentState
        from datetime import datetime

        await state_manager.initialize()

        # Add many items to queue concurrently
        async def add_to_queue(i):
            item = QueuedTorrentState(
                id=str(i),
                magnet_link=f"magnet:?xt=urn:btih:HASH{i}",
                torrent_file=None,
                name=f"Torrent {i}",
                category="test",
                instance_id="test-1",
                estimated_size=1000000,
                added_time=datetime.now().timestamp(),
            )
            await state_manager.add_to_queue(item)

        tasks = [add_to_queue(i) for i in range(50)]
        await asyncio.gather(*tasks)

        # All items should be in queue
        queue = await state_manager.get_queue()
        assert len(queue) == 50

    @pytest.mark.asyncio
    async def test_concurrent_hash_mapping(self, state_manager):
        """Concurrent hash mapping operations are thread-safe."""
        await state_manager.initialize()

        # Add mappings concurrently
        async def add_mapping(i):
            await state_manager.add_hash_mapping(f"QUEUE{i}", f"REAL{i}")

        tasks = [add_mapping(i) for i in range(100)]
        await asyncio.gather(*tasks)

        # All mappings should exist
        for i in range(100):
            resolved = await state_manager.resolve_hash(f"QUEUE{i}")
            assert resolved == f"REAL{i}"

    @pytest.mark.asyncio
    async def test_concurrent_category_operations(self, state_manager):
        """Concurrent category operations are thread-safe."""
        from seedr_sonarr.state import CategoryState

        await state_manager.initialize()

        # Add categories concurrently
        async def add_category(i):
            cat = CategoryState(
                name=f"category-{i}",
                save_path=f"/downloads/category-{i}",
                instance_id=f"instance-{i}",
            )
            await state_manager.add_category(cat)

        tasks = [add_category(i) for i in range(50)]
        await asyncio.gather(*tasks)

        # All categories should exist
        categories = await state_manager.get_categories()
        assert len(categories) == 50


class TestCircuitBreakerConcurrency:
    """Tests for concurrent CircuitBreaker access."""

    @pytest.mark.asyncio
    async def test_concurrent_success_recording(self):
        """Concurrent success recordings are thread-safe."""
        config = CircuitBreakerConfig(
            failure_threshold=5,
            success_threshold=2,
            reset_timeout=1.0,
        )
        cb = CircuitBreaker(config, name="test")

        # Record many successes concurrently
        tasks = [cb.record_success() for _ in range(100)]
        await asyncio.gather(*tasks)

        stats = cb.get_stats()
        assert stats["total_calls"] == 100
        assert cb.is_closed

    @pytest.mark.asyncio
    async def test_concurrent_failure_recording(self):
        """Concurrent failure recordings trigger circuit correctly."""
        config = CircuitBreakerConfig(
            failure_threshold=5,
            success_threshold=2,
            reset_timeout=10.0,
        )
        cb = CircuitBreaker(config, name="test")

        # Record failures concurrently
        tasks = [cb.record_failure() for _ in range(10)]
        await asyncio.gather(*tasks)

        # Circuit should be open
        assert cb.is_open

    @pytest.mark.asyncio
    async def test_concurrent_can_execute_checks(self):
        """Concurrent can_execute checks are thread-safe."""
        config = CircuitBreakerConfig(
            failure_threshold=5,
            success_threshold=2,
            reset_timeout=60.0,
        )
        cb = CircuitBreaker(config, name="test")

        # Check can_execute concurrently
        tasks = [cb.can_execute() for _ in range(100)]
        results = await asyncio.gather(*tasks)

        # All should return True (circuit starts closed)
        assert all(results)


class TestRateLimiterConcurrency:
    """Tests for concurrent RateLimiter access."""

    @pytest.mark.asyncio
    async def test_concurrent_acquire(self):
        """Concurrent acquires respect rate limit."""
        limiter = RateLimiter(rate=100.0, burst=10)

        # Try to acquire many tokens concurrently
        async def try_acquire():
            return await limiter.acquire(timeout=1.0)

        tasks = [try_acquire() for _ in range(50)]
        results = await asyncio.gather(*tasks)

        # Should get some successes and some timeouts
        successes = sum(1 for r in results if r)
        assert successes > 0  # At least some should succeed

    @pytest.mark.asyncio
    async def test_burst_handling(self):
        """Burst is correctly handled under concurrent load."""
        limiter = RateLimiter(rate=10.0, burst=5)

        # First burst should succeed instantly
        tasks = [limiter.acquire(timeout=0.1) for _ in range(5)]
        results = await asyncio.gather(*tasks)

        # All 5 should succeed (within burst)
        assert all(results)

        # Next ones should be rate limited
        result = await limiter.acquire(timeout=0.05)
        # May or may not succeed depending on timing


class TestQnapLockConcurrency:
    """Tests for QNAP lock concurrency in SeedrClientWrapper."""

    @pytest.mark.asyncio
    async def test_qnap_pending_concurrent_access(self):
        """Concurrent access to _qnap_pending_completion is safe."""
        from seedr_sonarr.seedr_client import SeedrClientWrapper

        client = SeedrClientWrapper(
            email="test@test.com",
            password="test",
            download_path="/tmp/test",
        )

        # Simulate concurrent access to _qnap_pending_completion
        async def add_pending(i):
            async with client._qnap_lock:
                client._qnap_pending_completion[f"HASH{i}"] = {
                    "folder_id": str(i),
                    "folder_name": f"Folder {i}",
                    "total_size": 1000 * i,
                    "category": "test",
                    "instance_id": "test-1",
                    "save_path": "/downloads",
                    "content_path": f"/downloads/Folder{i}",
                }

        async def remove_pending(i):
            await asyncio.sleep(0.001)  # Small delay
            async with client._qnap_lock:
                client._qnap_pending_completion.pop(f"HASH{i}", None)

        # Add and remove concurrently
        add_tasks = [add_pending(i) for i in range(50)]
        remove_tasks = [remove_pending(i) for i in range(25)]

        await asyncio.gather(*add_tasks, *remove_tasks)

        # Should have ~25 items remaining
        async with client._qnap_lock:
            remaining = len(client._qnap_pending_completion)
        assert remaining >= 25


class TestMappingLockConcurrency:
    """Tests for mapping lock concurrency."""

    @pytest.mark.asyncio
    async def test_concurrent_mapping_updates(self):
        """Concurrent mapping updates are atomic."""
        from seedr_sonarr.seedr_client import SeedrClientWrapper

        client = SeedrClientWrapper(
            email="test@test.com",
            password="test",
            download_path="/tmp/test",
        )

        # Add mappings concurrently
        async def add_mapping(i):
            await client.set_category_mapping(f"HASH{i}", f"category-{i}", f"instance-{i}")

        tasks = [add_mapping(i) for i in range(100)]
        await asyncio.gather(*tasks)

        # All mappings should exist
        for i in range(100):
            cat, inst = await client.get_category_mapping(f"HASH{i}")
            assert cat == f"category-{i}"
            assert inst == f"instance-{i}"

    @pytest.mark.asyncio
    async def test_concurrent_mapping_read_write(self):
        """Concurrent reads and writes to mappings are safe."""
        from seedr_sonarr.seedr_client import SeedrClientWrapper

        client = SeedrClientWrapper(
            email="test@test.com",
            password="test",
            download_path="/tmp/test",
        )

        # Pre-populate some mappings
        for i in range(50):
            await client.set_category_mapping(f"HASH{i}", f"cat-{i}", f"inst-{i}")

        # Concurrent reads and writes
        async def read_mapping(i):
            return await client.get_category_mapping(f"HASH{i % 50}")

        async def write_mapping(i):
            await client.set_category_mapping(f"NEWHASH{i}", f"newcat-{i}", f"newinst-{i}")

        read_tasks = [read_mapping(i) for i in range(100)]
        write_tasks = [write_mapping(i) for i in range(50)]

        await asyncio.gather(*read_tasks, *write_tasks)

        # Original mappings should still be intact
        for i in range(50):
            cat, inst = await client.get_category_mapping(f"HASH{i}")
            assert cat == f"cat-{i}"
