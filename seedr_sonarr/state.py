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
    PersistedQnapPending,
    PersistedNameHashMapping,
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
        self._tags: Set[str] = set()  # Set of all tag names
        self._torrent_tags: Dict[str, Set[str]] = {}  # torrent_hash -> set of tags
        # Name hash mappings cache: (normalized_name, instance_id) -> PersistedNameHashMapping
        self._name_hash_mappings: Dict[tuple, PersistedNameHashMapping] = {}

        # Activity callbacks (protected by _callback_lock)
        self._callback_lock = asyncio.Lock()
        self._on_phase_change: List[Callable[[str, TorrentPhase, TorrentPhase], Awaitable]] = []
        self._on_error: List[Callable[[str, str], Awaitable]] = []
        self._on_complete: List[Callable[[str], Awaitable]] = []

        # Constants
        self._max_error_count = 1000  # Prevent unbounded error counts

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

            # Load tags
            self._tags = set(await self._persistence.get_tags())
            logger.info(f"Loaded {len(self._tags)} tags from persistence")

            # Load torrent tags
            for torrent_hash in self._torrents.keys():
                tags = await self._persistence.get_torrent_tags(torrent_hash)
                if tags:
                    self._torrent_tags[torrent_hash] = set(tags)
            logger.info(f"Loaded tags for {len(self._torrent_tags)} torrents from persistence")

            # Load name hash mappings
            name_hash_mappings = await self._persistence.get_all_name_hash_mappings()
            for mapping in name_hash_mappings:
                cache_key = (mapping.normalized_name, mapping.instance_id)
                self._name_hash_mappings[cache_key] = mapping
            logger.info(f"Loaded {len(self._name_hash_mappings)} name hash mappings from persistence")

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

    async def on_phase_change(
        self, callback: Callable[[str, TorrentPhase, TorrentPhase], Awaitable]
    ) -> None:
        """Register callback for phase changes."""
        async with self._callback_lock:
            self._on_phase_change.append(callback)

    async def on_error(self, callback: Callable[[str, str], Awaitable]) -> None:
        """Register callback for errors."""
        async with self._callback_lock:
            self._on_error.append(callback)

    async def on_complete(self, callback: Callable[[str], Awaitable]) -> None:
        """Register callback for download completion."""
        async with self._callback_lock:
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
        async with self._lock:
            # Check hash mapping
            real_hash = self._hash_mappings.get(hash.upper(), hash.upper())
            return self._torrents.get(real_hash)

    async def get_torrents(self) -> List[TorrentState]:
        """Get all torrents."""
        async with self._lock:
            return list(self._torrents.values())

    async def find_torrent_by_name(self, name: str) -> Optional[PersistedTorrent]:
        """Find a torrent by fuzzy name match (for hash transitions)."""
        if self._persistence:
            return await self._persistence.find_torrent_by_name(name)
        return None

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

            # Clean up torrent tags (fix memory leak)
            if real_hash in self._torrent_tags:
                del self._torrent_tags[real_hash]

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
                torrent.seedr_progress = max(0.0, min(1.0, seedr_progress))  # Clamp to [0, 1]
            if local_progress is not None:
                torrent.local_progress = max(0.0, min(1.0, local_progress))  # Clamp to [0, 1]

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
        async with self._lock:
            real_hash = self._hash_mappings.get(hash.upper(), hash.upper())
            torrent = self._torrents.get(real_hash)

            if not torrent:
                return

            if seedr_progress is not None:
                torrent.seedr_progress = max(0.0, min(1.0, seedr_progress))  # Clamp to [0, 1]
            if local_progress is not None:
                torrent.local_progress = max(0.0, min(1.0, local_progress))  # Clamp to [0, 1]
            if download_speed is not None:
                torrent.download_speed = max(0, download_speed)  # Ensure non-negative

            if self._persist_enabled:
                await self._persistence.update_torrent_progress(
                    real_hash, torrent.seedr_progress, torrent.local_progress
                )

    async def record_error(self, hash: str, error: str) -> None:
        """Record an error for a torrent."""
        async with self._lock:
            real_hash = self._hash_mappings.get(hash.upper(), hash.upper())
            torrent = self._torrents.get(real_hash)

            if not torrent:
                return

            torrent.error_count = min(torrent.error_count + 1, self._max_error_count)  # Bound error count
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
        async with self._lock:
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
        async with self._lock:
            if not self._queue:
                return None
            return self._queue[0]

    async def remove_from_queue(self, id: str) -> bool:
        """Remove a specific item from the queue."""
        async with self._lock:
            for queued in list(self._queue):
                if queued.id == id:
                    self._queue.remove(queued)
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

    async def get_queue_size(self) -> int:
        """Get the queue size (thread-safe)."""
        async with self._lock:
            return len(self._queue)

    @property
    def queue_size(self) -> int:
        """Get the queue size (non-blocking, may be approximate under concurrency)."""
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
        async with self._lock:
            return self._categories.get(name)

    async def get_categories(self) -> Dict[str, CategoryState]:
        """Get all categories."""
        async with self._lock:
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

    async def resolve_hash(self, hash: str) -> str:
        """Resolve a hash (queue hash -> real hash or same)."""
        async with self._lock:
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

    async def save_completed_torrent(
        self,
        hash: str,
        seedr_id: str,
        name: str,
        size: int,
        category: str,
        instance_id: str,
        save_path: str,
        content_path: str,
    ) -> None:
        """
        Save completed torrent info to persistence.
        This ensures the torrent can be displayed even after deletion from Seedr.
        """
        async with self._lock:
            real_hash = self._hash_mappings.get(hash.upper(), hash.upper())

            # Create or update torrent state in memory
            torrent = TorrentState(
                hash=real_hash,
                seedr_id=seedr_id,
                name=name,
                size=size,
                category=category,
                instance_id=instance_id,
                phase=TorrentPhase.COMPLETED,
                seedr_progress=1.0,
                local_progress=1.0,
                added_on=int(datetime.now().timestamp()),
                save_path=save_path,
                content_path=content_path,
            )
            self._torrents[real_hash] = torrent

            if self._persist_enabled:
                await self._persistence.save_torrent(PersistedTorrent(
                    hash=real_hash,
                    seedr_id=seedr_id,
                    name=name,
                    size=size,
                    category=category,
                    instance_id=instance_id,
                    state=torrent.qbit_state,
                    phase=TorrentPhase.COMPLETED.value,
                    seedr_progress=1.0,
                    local_progress=1.0,
                    added_on=torrent.added_on,
                    save_path=save_path,
                    content_path=content_path,
                ))
                await self._persistence.log_activity(
                    action="torrent_completed",
                    torrent_hash=real_hash,
                    torrent_name=name,
                    category=category,
                    details=f"Completed and persisted (content: {content_path})",
                )

            logger.info(f"Saved completed torrent: {name} (hash: {real_hash})")

    async def is_local_download(self, hash: str) -> bool:
        """Check if a torrent is downloaded locally."""
        async with self._lock:
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
    # QNAP Pending Operations
    # -------------------------------------------------------------------------

    async def save_qnap_pending(
        self,
        torrent_hash: str,
        folder_id: str,
        folder_name: str,
        total_size: int,
        category: str,
        instance_id: str,
        save_path: str,
        content_path: str,
    ) -> None:
        """Save a QNAP pending download for persistence across restarts."""
        if self._persist_enabled:
            await self._persistence.save_qnap_pending(PersistedQnapPending(
                torrent_hash=torrent_hash,
                folder_id=folder_id,
                folder_name=folder_name,
                total_size=total_size,
                category=category,
                instance_id=instance_id,
                save_path=save_path,
                content_path=content_path,
            ))

    async def get_qnap_pending(self) -> List[PersistedQnapPending]:
        """Get all QNAP pending downloads."""
        if not self._persist_enabled:
            return []
        return await self._persistence.get_qnap_pending()

    async def delete_qnap_pending(self, torrent_hash: str) -> None:
        """Delete a QNAP pending download."""
        if self._persist_enabled:
            await self._persistence.delete_qnap_pending(torrent_hash)

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
        async with self._lock:
            stats = {
                "torrents": len(self._torrents),
                "queued": len(self._queue),
                "categories": len(self._categories),
                "hash_mappings": len(self._hash_mappings),
                "local_downloads": len(self._local_downloads),
            }

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

        if self._persist_enabled:
            db_stats = await self._persistence.get_stats()
            stats["persistence"] = db_stats

        return stats

    # -------------------------------------------------------------------------
    # Tag Operations
    # -------------------------------------------------------------------------

    async def create_tag(self, name: str) -> None:
        """Create a new tag."""
        async with self._lock:
            self._tags.add(name)

            if self._persist_enabled:
                await self._persistence.save_tag(name)

    async def get_tags(self) -> List[str]:
        """Get all tags."""
        async with self._lock:
            return sorted(list(self._tags))

    async def delete_tag(self, name: str) -> bool:
        """Delete a tag and remove it from all torrents."""
        async with self._lock:
            if name not in self._tags:
                return False

            self._tags.discard(name)

            # Remove tag from all torrents
            for torrent_hash in list(self._torrent_tags.keys()):
                if name in self._torrent_tags[torrent_hash]:
                    self._torrent_tags[torrent_hash].discard(name)
                    if not self._torrent_tags[torrent_hash]:
                        del self._torrent_tags[torrent_hash]

            if self._persist_enabled:
                await self._persistence.delete_tag(name)

            return True

    async def add_torrent_tags(self, hashes: List[str], tags: List[str]) -> None:
        """Add tags to torrents."""
        async with self._lock:
            for hash in hashes:
                real_hash = self._hash_mappings.get(hash.upper(), hash.upper())

                if real_hash not in self._torrent_tags:
                    self._torrent_tags[real_hash] = set()

                for tag in tags:
                    self._tags.add(tag)
                    self._torrent_tags[real_hash].add(tag)

                    if self._persist_enabled:
                        await self._persistence.add_torrent_tag(real_hash, tag)

    async def remove_torrent_tags(self, hashes: List[str], tags: List[str]) -> None:
        """Remove tags from torrents."""
        async with self._lock:
            for hash in hashes:
                real_hash = self._hash_mappings.get(hash.upper(), hash.upper())

                if real_hash in self._torrent_tags:
                    for tag in tags:
                        self._torrent_tags[real_hash].discard(tag)

                        if self._persist_enabled:
                            await self._persistence.remove_torrent_tag(real_hash, tag)

                    # Clean up empty sets
                    if not self._torrent_tags[real_hash]:
                        del self._torrent_tags[real_hash]

    async def get_torrent_tags(self, hash: str) -> List[str]:
        """Get tags for a specific torrent."""
        async with self._lock:
            real_hash = self._hash_mappings.get(hash.upper(), hash.upper())
            tags = self._torrent_tags.get(real_hash, set())
            return sorted(list(tags))

    # -------------------------------------------------------------------------
    # Name Hash Mapping Operations
    # -------------------------------------------------------------------------

    async def add_name_hash_mapping(
        self,
        normalized_name: str,
        original_name: str,
        torrent_hash: str,
        category: str = "",
        instance_id: str = "",
        magnet_hash: Optional[str] = None,
    ) -> None:
        """Add or update a name-to-hash mapping for tracking hash transitions."""
        async with self._lock:
            cache_key = (normalized_name, instance_id)

            # Create mapping object
            mapping = PersistedNameHashMapping(
                id=None,
                normalized_name=normalized_name,
                original_name=original_name,
                torrent_hash=torrent_hash,
                category=category,
                instance_id=instance_id,
                magnet_hash=magnet_hash,
            )

            # Update cache
            self._name_hash_mappings[cache_key] = mapping

            # Persist
            if self._persist_enabled:
                await self._persistence.save_name_hash_mapping(
                    normalized_name=normalized_name,
                    original_name=original_name,
                    torrent_hash=torrent_hash,
                    category=category,
                    instance_id=instance_id,
                    magnet_hash=magnet_hash,
                )

    async def get_name_hash_mapping(
        self,
        normalized_name: str,
        instance_id: Optional[str] = None,
    ) -> Optional[PersistedNameHashMapping]:
        """Get a name-to-hash mapping, optionally filtered by instance."""
        async with self._lock:
            # Try exact cache match first
            if instance_id is not None:
                cache_key = (normalized_name, instance_id)
                if cache_key in self._name_hash_mappings:
                    return self._name_hash_mappings[cache_key]

            # Try any instance in cache
            for (name, _inst), mapping in self._name_hash_mappings.items():
                if name == normalized_name:
                    return mapping

            # Fall back to persistence lookup
            if self._persist_enabled:
                mapping = await self._persistence.get_name_hash_mapping(
                    normalized_name, instance_id
                )
                if mapping:
                    # Update cache
                    cache_key = (mapping.normalized_name, mapping.instance_id)
                    self._name_hash_mappings[cache_key] = mapping
                return mapping

            return None

    async def get_name_hash_mapping_by_magnet(
        self,
        magnet_hash: str,
    ) -> Optional[PersistedNameHashMapping]:
        """Get a name-to-hash mapping by magnet info hash."""
        async with self._lock:
            # Check cache first
            for mapping in self._name_hash_mappings.values():
                if mapping.magnet_hash == magnet_hash:
                    return mapping

            # Fall back to persistence
            if self._persist_enabled:
                mapping = await self._persistence.get_name_hash_mapping_by_magnet(magnet_hash)
                if mapping:
                    # Update cache
                    cache_key = (mapping.normalized_name, mapping.instance_id)
                    self._name_hash_mappings[cache_key] = mapping
                return mapping

            return None

    async def update_name_hash_transition(
        self,
        normalized_name: str,
        old_hash: str,
        new_hash: str,
        instance_id: str = "",
    ) -> None:
        """Update a name hash mapping when a hash transition occurs."""
        async with self._lock:
            cache_key = (normalized_name, instance_id)

            # Update cache
            if cache_key in self._name_hash_mappings:
                mapping = self._name_hash_mappings[cache_key]
                # Create updated mapping
                updated_mapping = PersistedNameHashMapping(
                    id=mapping.id,
                    normalized_name=mapping.normalized_name,
                    original_name=mapping.original_name,
                    torrent_hash=new_hash,
                    category=mapping.category,
                    instance_id=mapping.instance_id,
                    magnet_hash=mapping.magnet_hash,
                    created_at=mapping.created_at,
                )
                self._name_hash_mappings[cache_key] = updated_mapping

            # Update persistence
            if self._persist_enabled:
                await self._persistence.update_name_hash_mapping_hash(old_hash, new_hash)

            logger.debug(f"Updated name hash transition: {old_hash} -> {new_hash} for {normalized_name}")

    async def deactivate_name_hash_mappings(self, torrent_hash: str) -> None:
        """Deactivate all name hash mappings for a deleted torrent."""
        async with self._lock:
            # Remove from cache
            keys_to_remove = [
                key for key, mapping in self._name_hash_mappings.items()
                if mapping.torrent_hash == torrent_hash
            ]
            for key in keys_to_remove:
                del self._name_hash_mappings[key]

            # Deactivate in persistence
            if self._persist_enabled:
                await self._persistence.deactivate_name_hash_mappings(torrent_hash)

    async def get_all_name_hash_mappings(self) -> List[PersistedNameHashMapping]:
        """Get all active name hash mappings."""
        async with self._lock:
            return list(self._name_hash_mappings.values())


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
