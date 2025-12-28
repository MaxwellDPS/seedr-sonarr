"""
Centralized State Management for Seedr-Sonarr Proxy
Provides thread-safe state access with automatic persistence.
"""

import asyncio
import base64
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Dict, List, Set, Callable, Awaitable

from .persistence import (
    PersistenceManager,
    PersistedTorrent,
    PersistedQueuedTorrent,
    PersistedCategory,
)

logger = logging.getLogger(__name__)


class TorrentPhase(Enum):
    """Granular torrent lifecycle phases."""
    QUEUED_STORAGE = "queued_storage"           # Waiting for Seedr storage
    FETCHING_METADATA = "fetching_metadata"     # Getting torrent metadata
    DOWNLOADING_TO_SEEDR = "downloading_to_seedr"  # Internet -> Seedr
    SEEDR_PROCESSING = "seedr_processing"       # Seedr is processing/extracting
    SEEDR_COMPLETE = "seedr_complete"           # In Seedr, not yet downloading locally
    QUEUED_LOCAL = "queued_local"               # Waiting for local download slot
    DOWNLOADING_TO_LOCAL = "downloading_to_local"  # Seedr -> Local
    COMPLETED = "completed"                      # Fully downloaded locally
    ERROR = "error"                              # Error state
    PAUSED = "paused"                           # Paused by user
    DELETED = "deleted"                          # Marked for deletion


# Map phases to qBittorrent-compatible states
PHASE_TO_QBIT_STATE = {
    TorrentPhase.QUEUED_STORAGE: "queuedDL",
    TorrentPhase.FETCHING_METADATA: "metaDL",
    TorrentPhase.DOWNLOADING_TO_SEEDR: "downloading",
    TorrentPhase.SEEDR_PROCESSING: "downloading",
    TorrentPhase.SEEDR_COMPLETE: "pausedUP",
    TorrentPhase.QUEUED_LOCAL: "queuedDL",
    TorrentPhase.DOWNLOADING_TO_LOCAL: "downloading",
    TorrentPhase.COMPLETED: "pausedUP",
    TorrentPhase.ERROR: "error",
    TorrentPhase.PAUSED: "pausedDL",
    TorrentPhase.DELETED: "missingFiles",
}


@dataclass
class TorrentState:
    """Runtime state for a torrent."""
    hash: str
    seedr_id: Optional[str]
    name: str
    size: int
    category: str
    instance_id: str
    phase: TorrentPhase
    seedr_progress: float  # 0.0 to 1.0 - progress of internet -> Seedr
    local_progress: float  # 0.0 to 1.0 - progress of Seedr -> local
    added_on: int
    save_path: str
    content_path: Optional[str] = None
    error_count: int = 0
    last_error: Optional[str] = None
    last_error_time: Optional[float] = None
    download_speed: int = 0  # Current download speed in bytes/sec
    seeders: int = 0
    leechers: int = 0

    @property
    def qbit_state(self) -> str:
        """Get qBittorrent-compatible state string."""
        return PHASE_TO_QBIT_STATE.get(self.phase, "unknown")

    @property
    def combined_progress(self) -> float:
        """
        Get combined progress (0.0 to 1.0) for qBittorrent API.
        0-50% = Seedr download, 50-100% = local download
        """
        return (self.seedr_progress * 0.5) + (self.local_progress * 0.5)

    @property
    def eta(self) -> int:
        """Estimate time remaining in seconds."""
        if self.phase == TorrentPhase.COMPLETED:
            return 0
        if self.download_speed <= 0:
            return 8640000  # Unknown

        remaining_bytes = self.size * (1 - self.combined_progress)
        return int(remaining_bytes / self.download_speed)


@dataclass
class QueuedTorrentState:
    """Runtime state for a queued torrent."""
    id: str
    magnet_link: Optional[str]
    torrent_file: Optional[bytes]
    category: str
    instance_id: str
    name: str
    estimated_size: int
    added_time: float
    retry_count: int = 0
    last_retry: Optional[float] = None


@dataclass
class CategoryState:
    """Runtime state for a category."""
    name: str
    save_path: str
    instance_id: str


class StateManager:
    """
    Centralized state management with persistence.
    Provides thread-safe access to all application state.
    """

    def __init__(
        self,
        persistence: Optional[PersistenceManager] = None,
        persist_enabled: bool = True,
    ):
        self._persistence = persistence
        self._persist_enabled = persist_enabled and persistence is not None
        self._lock = asyncio.Lock()
        self._initialized = False

        # In-memory state
        self._torrents: Dict[str, TorrentState] = {}
        self._queue: deque[QueuedTorrentState] = deque()
        self._categories: Dict[str, CategoryState] = {}
        self._hash_mappings: Dict[str, str] = {}  # queue_hash -> real_hash
        self._local_downloads: Set[str] = set()

        # Activity callbacks
        self._on_phase_change: List[Callable[[str, TorrentPhase, TorrentPhase], Awaitable]] = []
        self._on_error: List[Callable[[str, str], Awaitable]] = []
        self._on_complete: List[Callable[[str], Awaitable]] = []

    async def initialize(self) -> None:
        """Initialize state manager and load persisted state."""
        async with self._lock:
            if self._initialized:
                return

            if self._persist_enabled:
                await self._persistence.initialize()
                await self._load_state()

            self._initialized = True
            logger.info("State manager initialized")

    async def _load_state(self) -> None:
        """Load state from persistence."""
        try:
            # Load torrents
            persisted_torrents = await self._persistence.get_torrents()
            for pt in persisted_torrents:
                self._torrents[pt.hash] = TorrentState(
                    hash=pt.hash,
                    seedr_id=pt.seedr_id,
                    name=pt.name,
                    size=pt.size,
                    category=pt.category,
                    instance_id=pt.instance_id,
                    phase=TorrentPhase(pt.phase) if pt.phase else TorrentPhase.QUEUED_STORAGE,
                    seedr_progress=pt.seedr_progress,
                    local_progress=pt.local_progress,
                    added_on=pt.added_on,
                    save_path=pt.save_path,
                    content_path=pt.content_path,
                    error_count=pt.error_count,
                    last_error=pt.last_error,
                    last_error_time=pt.last_error_time,
                )
            logger.info(f"Loaded {len(self._torrents)} torrents from persistence")

            # Load queue
            persisted_queue = await self._persistence.get_queued()
            for pq in persisted_queue:
                torrent_file = None
                if pq.torrent_file_b64:
                    torrent_file = base64.b64decode(pq.torrent_file_b64)

                self._queue.append(QueuedTorrentState(
                    id=pq.id,
                    magnet_link=pq.magnet_link,
                    torrent_file=torrent_file,
                    category=pq.category,
                    instance_id=pq.instance_id,
                    name=pq.name,
                    estimated_size=pq.estimated_size,
                    added_time=pq.added_time,
                    retry_count=pq.retry_count,
                    last_retry=pq.last_retry,
                ))
            logger.info(f"Loaded {len(self._queue)} queued torrents from persistence")

            # Load categories
            persisted_categories = await self._persistence.get_categories()
            for name, pc in persisted_categories.items():
                self._categories[name] = CategoryState(
                    name=pc.name,
                    save_path=pc.save_path,
                    instance_id=pc.instance_id,
                )
            logger.info(f"Loaded {len(self._categories)} categories from persistence")

            # Load hash mappings
            self._hash_mappings = await self._persistence.get_hash_mappings()
            logger.info(f"Loaded {len(self._hash_mappings)} hash mappings from persistence")

            # Load local downloads
            self._local_downloads = await self._persistence.get_local_downloads()
            logger.info(f"Loaded {len(self._local_downloads)} local downloads from persistence")

        except Exception as e:
            logger.error(f"Error loading state from persistence: {e}")

    async def close(self) -> None:
        """Close the state manager."""
        if self._persist_enabled:
            await self._persistence.close()
        self._initialized = False

    # -------------------------------------------------------------------------
    # Callback Registration
    # -------------------------------------------------------------------------

    def on_phase_change(
        self, callback: Callable[[str, TorrentPhase, TorrentPhase], Awaitable]
    ) -> None:
        """Register callback for phase changes."""
        self._on_phase_change.append(callback)

    def on_error(self, callback: Callable[[str, str], Awaitable]) -> None:
        """Register callback for errors."""
        self._on_error.append(callback)

    def on_complete(self, callback: Callable[[str], Awaitable]) -> None:
        """Register callback for download completion."""
        self._on_complete.append(callback)

    # -------------------------------------------------------------------------
    # Torrent Operations
    # -------------------------------------------------------------------------

    async def add_torrent(self, torrent: TorrentState) -> None:
        """Add or update a torrent."""
        async with self._lock:
            old_phase = None
            if torrent.hash in self._torrents:
                old_phase = self._torrents[torrent.hash].phase

            self._torrents[torrent.hash] = torrent

            if self._persist_enabled:
                await self._persistence.save_torrent(PersistedTorrent(
                    hash=torrent.hash,
                    seedr_id=torrent.seedr_id,
                    name=torrent.name,
                    size=torrent.size,
                    category=torrent.category,
                    instance_id=torrent.instance_id,
                    state=torrent.qbit_state,
                    phase=torrent.phase.value,
                    seedr_progress=torrent.seedr_progress,
                    local_progress=torrent.local_progress,
                    added_on=torrent.added_on,
                    save_path=torrent.save_path,
                    content_path=torrent.content_path,
                    error_count=torrent.error_count,
                    last_error=torrent.last_error,
                    last_error_time=torrent.last_error_time,
                ))

            # Fire phase change callbacks
            if old_phase and old_phase != torrent.phase:
                for callback in self._on_phase_change:
                    try:
                        await callback(torrent.hash, old_phase, torrent.phase)
                    except Exception as e:
                        logger.error(f"Error in phase change callback: {e}")

    async def get_torrent(self, hash: str) -> Optional[TorrentState]:
        """Get a torrent by hash."""
        # Check hash mapping
        real_hash = self._hash_mappings.get(hash.upper(), hash.upper())
        return self._torrents.get(real_hash)

    async def get_torrents(self) -> List[TorrentState]:
        """Get all torrents."""
        return list(self._torrents.values())

    async def delete_torrent(self, hash: str) -> bool:
        """Delete a torrent."""
        async with self._lock:
            real_hash = self._hash_mappings.get(hash.upper(), hash.upper())

            if real_hash not in self._torrents:
                return False

            del self._torrents[real_hash]

            if self._persist_enabled:
                await self._persistence.delete_torrent(real_hash)
                await self._persistence.delete_local_download(real_hash)
                await self._persistence.delete_hash_mappings_by_real(real_hash)

            self._local_downloads.discard(real_hash)

            # Clean up hash mappings
            keys_to_remove = [k for k, v in self._hash_mappings.items() if v == real_hash]
            for k in keys_to_remove:
                del self._hash_mappings[k]

            return True

    async def update_phase(
        self,
        hash: str,
        phase: TorrentPhase,
        seedr_progress: float = None,
        local_progress: float = None,
    ) -> None:
        """Update torrent phase and optionally progress."""
        async with self._lock:
            real_hash = self._hash_mappings.get(hash.upper(), hash.upper())
            torrent = self._torrents.get(real_hash)

            if not torrent:
                return

            old_phase = torrent.phase
            torrent.phase = phase

            if seedr_progress is not None:
                torrent.seedr_progress = seedr_progress
            if local_progress is not None:
                torrent.local_progress = local_progress

            if self._persist_enabled:
                await self._persistence.save_torrent(PersistedTorrent(
                    hash=torrent.hash,
                    seedr_id=torrent.seedr_id,
                    name=torrent.name,
                    size=torrent.size,
                    category=torrent.category,
                    instance_id=torrent.instance_id,
                    state=torrent.qbit_state,
                    phase=torrent.phase.value,
                    seedr_progress=torrent.seedr_progress,
                    local_progress=torrent.local_progress,
                    added_on=torrent.added_on,
                    save_path=torrent.save_path,
                    content_path=torrent.content_path,
                    error_count=torrent.error_count,
                    last_error=torrent.last_error,
                    last_error_time=torrent.last_error_time,
                ))

            # Fire callbacks
            if old_phase != phase:
                for callback in self._on_phase_change:
                    try:
                        await callback(real_hash, old_phase, phase)
                    except Exception as e:
                        logger.error(f"Error in phase change callback: {e}")

                # Fire completion callback
                if phase == TorrentPhase.COMPLETED:
                    for callback in self._on_complete:
                        try:
                            await callback(real_hash)
                        except Exception as e:
                            logger.error(f"Error in completion callback: {e}")

    async def update_progress(
        self,
        hash: str,
        seedr_progress: float = None,
        local_progress: float = None,
        download_speed: int = None,
    ) -> None:
        """Update torrent progress efficiently."""
        real_hash = self._hash_mappings.get(hash.upper(), hash.upper())
        torrent = self._torrents.get(real_hash)

        if not torrent:
            return

        if seedr_progress is not None:
            torrent.seedr_progress = seedr_progress
        if local_progress is not None:
            torrent.local_progress = local_progress
        if download_speed is not None:
            torrent.download_speed = download_speed

        if self._persist_enabled:
            await self._persistence.update_torrent_progress(
                real_hash, seedr_progress, local_progress
            )

    async def record_error(self, hash: str, error: str) -> None:
        """Record an error for a torrent."""
        async with self._lock:
            real_hash = self._hash_mappings.get(hash.upper(), hash.upper())
            torrent = self._torrents.get(real_hash)

            if not torrent:
                return

            torrent.error_count += 1
            torrent.last_error = error
            torrent.last_error_time = datetime.now().timestamp()
            torrent.phase = TorrentPhase.ERROR

            if self._persist_enabled:
                await self._persistence.update_torrent_error(real_hash, error)
                await self._persistence.log_activity(
                    action="error",
                    torrent_hash=real_hash,
                    torrent_name=torrent.name,
                    category=torrent.category,
                    details=error,
                    level="ERROR",
                )

            # Fire error callbacks
            for callback in self._on_error:
                try:
                    await callback(real_hash, error)
                except Exception as e:
                    logger.error(f"Error in error callback: {e}")

    async def clear_error(self, hash: str) -> None:
        """Clear error state for a torrent."""
        async with self._lock:
            real_hash = self._hash_mappings.get(hash.upper(), hash.upper())
            torrent = self._torrents.get(real_hash)

            if not torrent:
                return

            torrent.error_count = 0
            torrent.last_error = None
            torrent.last_error_time = None

            if self._persist_enabled:
                await self._persistence.clear_torrent_error(real_hash)

    # -------------------------------------------------------------------------
    # Queue Operations
    # -------------------------------------------------------------------------

    async def add_to_queue(self, queued: QueuedTorrentState) -> None:
        """Add a torrent to the queue."""
        async with self._lock:
            self._queue.append(queued)

            if self._persist_enabled:
                torrent_file_b64 = None
                if queued.torrent_file:
                    torrent_file_b64 = base64.b64encode(queued.torrent_file).decode()

                await self._persistence.save_queued(PersistedQueuedTorrent(
                    id=queued.id,
                    magnet_link=queued.magnet_link,
                    torrent_file_b64=torrent_file_b64,
                    category=queued.category,
                    instance_id=queued.instance_id,
                    name=queued.name,
                    estimated_size=queued.estimated_size,
                    added_time=queued.added_time,
                    retry_count=queued.retry_count,
                    last_retry=queued.last_retry,
                ))

                await self._persistence.log_activity(
                    action="queued",
                    torrent_name=queued.name,
                    category=queued.category,
                    details=f"Added to queue (position {len(self._queue)})",
                )

    async def get_queue(self) -> List[QueuedTorrentState]:
        """Get all queued torrents."""
        return list(self._queue)

    async def pop_queue(self) -> Optional[QueuedTorrentState]:
        """Pop the first item from the queue."""
        async with self._lock:
            if not self._queue:
                return None

            queued = self._queue.popleft()

            if self._persist_enabled:
                await self._persistence.delete_queued(queued.id)

            return queued

    async def peek_queue(self) -> Optional[QueuedTorrentState]:
        """Peek at the first item in the queue without removing it."""
        if not self._queue:
            return None
        return self._queue[0]

    async def remove_from_queue(self, id: str) -> bool:
        """Remove a specific item from the queue."""
        async with self._lock:
            for i, queued in enumerate(list(self._queue)):
                if queued.id == id:
                    del self._queue[i]
                    if self._persist_enabled:
                        await self._persistence.delete_queued(id)
                    return True
            return False

    async def requeue(self, queued: QueuedTorrentState) -> None:
        """Put an item back at the front of the queue."""
        async with self._lock:
            self._queue.appendleft(queued)

            if self._persist_enabled:
                await self._persistence.update_queued_retry(queued.id)

    async def clear_queue(self) -> int:
        """Clear all queued torrents. Returns count."""
        async with self._lock:
            count = len(self._queue)
            self._queue.clear()

            if self._persist_enabled:
                await self._persistence.clear_queue()

            return count

    @property
    def queue_size(self) -> int:
        """Get the queue size."""
        return len(self._queue)

    # -------------------------------------------------------------------------
    # Category Operations
    # -------------------------------------------------------------------------

    async def add_category(self, category: CategoryState) -> None:
        """Add or update a category."""
        async with self._lock:
            self._categories[category.name] = category

            if self._persist_enabled:
                await self._persistence.save_category(
                    category.name, category.save_path, category.instance_id
                )

    async def get_category(self, name: str) -> Optional[CategoryState]:
        """Get a category by name."""
        return self._categories.get(name)

    async def get_categories(self) -> Dict[str, CategoryState]:
        """Get all categories."""
        return dict(self._categories)

    async def delete_category(self, name: str) -> bool:
        """Delete a category."""
        async with self._lock:
            if name not in self._categories:
                return False

            del self._categories[name]

            if self._persist_enabled:
                await self._persistence.delete_category(name)

            return True

    # -------------------------------------------------------------------------
    # Hash Mapping Operations
    # -------------------------------------------------------------------------

    async def add_hash_mapping(self, queue_hash: str, real_hash: str) -> None:
        """Add a queue hash to real hash mapping."""
        async with self._lock:
            self._hash_mappings[queue_hash.upper()] = real_hash.upper()

            if self._persist_enabled:
                await self._persistence.save_hash_mapping(
                    queue_hash.upper(), real_hash.upper()
                )

    def resolve_hash(self, hash: str) -> str:
        """Resolve a hash (queue hash -> real hash or same)."""
        return self._hash_mappings.get(hash.upper(), hash.upper())

    async def delete_hash_mapping(self, queue_hash: str) -> None:
        """Delete a hash mapping."""
        async with self._lock:
            queue_hash = queue_hash.upper()
            if queue_hash in self._hash_mappings:
                del self._hash_mappings[queue_hash]

                if self._persist_enabled:
                    await self._persistence.delete_hash_mapping(queue_hash)

    # -------------------------------------------------------------------------
    # Local Download Operations
    # -------------------------------------------------------------------------

    async def mark_local_download(self, hash: str) -> None:
        """Mark a torrent as downloaded locally."""
        async with self._lock:
            real_hash = self._hash_mappings.get(hash.upper(), hash.upper())
            self._local_downloads.add(real_hash)

            if self._persist_enabled:
                await self._persistence.mark_local_download(real_hash)
                await self._persistence.log_activity(
                    action="download_complete",
                    torrent_hash=real_hash,
                    details="Downloaded to local storage",
                )

    def is_local_download(self, hash: str) -> bool:
        """Check if a torrent is downloaded locally."""
        real_hash = self._hash_mappings.get(hash.upper(), hash.upper())
        return real_hash in self._local_downloads

    async def remove_local_download(self, hash: str) -> None:
        """Remove a torrent from local downloads."""
        async with self._lock:
            real_hash = self._hash_mappings.get(hash.upper(), hash.upper())
            self._local_downloads.discard(real_hash)

            if self._persist_enabled:
                await self._persistence.delete_local_download(real_hash)

    # -------------------------------------------------------------------------
    # Activity Log Operations
    # -------------------------------------------------------------------------

    async def log_activity(
        self,
        action: str,
        torrent_hash: str = None,
        torrent_name: str = None,
        category: str = None,
        details: str = None,
        level: str = "INFO",
    ) -> None:
        """Log an activity."""
        if self._persist_enabled:
            await self._persistence.log_activity(
                action=action,
                torrent_hash=torrent_hash,
                torrent_name=torrent_name,
                category=category,
                details=details,
                level=level,
            )

    async def get_activity_log(
        self,
        limit: int = 100,
        level: str = None,
        torrent_hash: str = None,
    ) -> List[dict]:
        """Get activity log entries."""
        if not self._persist_enabled:
            return []

        entries = await self._persistence.get_activity_log(
            limit=limit, level=level, torrent_hash=torrent_hash
        )
        return [
            {
                "id": e.id,
                "timestamp": e.timestamp,
                "torrent_hash": e.torrent_hash,
                "torrent_name": e.torrent_name,
                "category": e.category,
                "action": e.action,
                "details": e.details,
                "level": e.level,
            }
            for e in entries
        ]

    # -------------------------------------------------------------------------
    # Utility Operations
    # -------------------------------------------------------------------------

    async def get_stats(self) -> Dict:
        """Get state statistics."""
        stats = {
            "torrents": len(self._torrents),
            "queued": len(self._queue),
            "categories": len(self._categories),
            "hash_mappings": len(self._hash_mappings),
            "local_downloads": len(self._local_downloads),
        }

        if self._persist_enabled:
            db_stats = await self._persistence.get_stats()
            stats["persistence"] = db_stats

        # Count by phase
        phases = {}
        for torrent in self._torrents.values():
            phase = torrent.phase.value
            phases[phase] = phases.get(phase, 0) + 1
        stats["by_phase"] = phases

        # Count by instance
        instances = {}
        for torrent in self._torrents.values():
            instance = torrent.instance_id or "default"
            instances[instance] = instances.get(instance, 0) + 1
        stats["by_instance"] = instances

        return stats


def extract_instance_id(category: str) -> str:
    """
    Extract instance identifier from category.

    Examples:
        'radarr' -> 'radarr'
        'radarr-4k' -> 'radarr-4k'
        'tv-sonarr' -> 'sonarr'
        'sonarr-anime' -> 'sonarr-anime'
        '' -> 'default'
    """
    if not category:
        return "default"

    category_lower = category.lower()

    # Look for known prefixes
    for prefix in ["radarr", "sonarr", "lidarr", "readarr", "prowlarr"]:
        if category_lower.startswith(prefix):
            # Return full category as instance ID
            return category_lower
        if prefix in category_lower:
            # Find the prefix position and extract from there
            idx = category_lower.find(prefix)
            # Take from prefix to end, but stop at next separator if any
            rest = category_lower[idx:]
            return rest

    # Use category as instance ID
    return category_lower
