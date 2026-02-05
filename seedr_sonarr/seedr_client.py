"""
Seedr Client Wrapper
Provides a clean interface to the Seedr API using seedrcc library.
Includes automatic downloading, storage management, queue system,
retry logic, state persistence, and multi-instance support.
"""

import asyncio
import logging
import os
import tempfile
import urllib.parse
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional, Callable, TYPE_CHECKING, Awaitable, TypeAlias

import aiofiles
import aiohttp

from .logging_config import LogContext
from .retry import RetryHandler, CircuitBreaker, RetryConfig, CircuitBreakerConfig, RateLimiter, RateLimitTimeoutError
from .state import TorrentPhase, TorrentState, extract_instance_id

if TYPE_CHECKING:
    from .state import StateManager
    from .qnap_client import QnapDownloadStationClient

# Type aliases for callbacks
ProgressCallback: TypeAlias = Callable[[int], None]
AsyncCallback: TypeAlias = Callable[[], Awaitable[None]]

logger = logging.getLogger(__name__)


class TorrentState(Enum):
    """Maps internal states to qBittorrent-compatible states."""
    QUEUED = "queuedDL"
    DOWNLOADING = "downloading"
    STALLED = "stalledDL"
    UPLOADING = "uploading"
    SEEDING = "stalledUP"
    PAUSED = "pausedDL"
    COMPLETED = "pausedUP"
    ERROR = "error"
    MISSING_FILES = "missingFiles"
    METADATA = "metaDL"
    CHECKING = "checkingDL"
    MOVING = "moving"
    # Custom states for local download tracking
    DOWNLOADING_LOCAL = "downloading"  # Downloading from Seedr to local
    COMPLETED_LOCAL = "pausedUP"  # Fully downloaded locally
    # Queue states
    QUEUED_STORAGE = "queuedDL"  # Waiting for Seedr storage space


@dataclass
class QueuedTorrent:
    """Represents a torrent waiting to be added to Seedr."""
    id: str  # Unique queue ID
    magnet_link: Optional[str] = None
    torrent_file: Optional[bytes] = None
    category: str = ""
    instance_id: str = ""  # Which Radarr/Sonarr instance owns this
    added_time: float = 0.0
    name: str = "Queued Torrent"
    estimated_size: int = 0  # Estimated size in bytes (if known)
    retry_count: int = 0
    last_error: Optional[str] = None

    def __post_init__(self):
        if not self.added_time:
            self.added_time = datetime.now().timestamp()
        if not self.instance_id and self.category:
            self.instance_id = extract_instance_id(self.category)


@dataclass
class SeedrFile:
    """Represents a file in Seedr."""
    id: str
    name: str
    size: int
    folder_id: str
    local_path: Optional[str] = None
    downloaded: bool = False
    download_progress: float = 0.0


@dataclass
class SeedrTorrent:
    """Represents a torrent/transfer in Seedr."""
    id: str
    hash: str
    name: str
    size: int
    progress: float
    state: TorrentState
    download_speed: int = 0
    upload_speed: int = 0
    ratio: float = 0.0
    eta: int = 8640000  # Default to max
    added_on: int = 0
    completion_on: int = 0
    save_path: str = ""
    content_path: str = ""
    category: str = ""
    instance_id: str = ""  # Which Radarr/Sonarr instance owns this
    tags: str = ""
    seeders: int = 0
    leechers: int = 0
    completed: int = 0
    downloaded: int = 0
    uploaded: int = 0
    files: list = field(default_factory=list)
    # Local download tracking
    local_progress: float = 0.0  # Progress of downloading to local storage
    is_local: bool = False  # True if fully downloaded locally
    # Queue info
    is_queued: bool = False  # True if waiting in queue
    # Error tracking
    error_count: int = 0
    last_error: Optional[str] = None
    # Phase tracking
    phase: TorrentPhase = TorrentPhase.QUEUED_STORAGE

    def __post_init__(self):
        if not self.instance_id and self.category:
            self.instance_id = extract_instance_id(self.category)


def normalize_folder_name(name: str) -> str:
    """
    Normalize folder name for consistent lookups.
    Seedr sometimes normalizes names (e.g., '+' -> space, special chars removed).
    This ensures we can match names regardless of normalization.
    """
    if not name:
        return ""
    # Replace common special chars with space
    normalized = name.replace("+", " ")
    normalized = normalized.replace(".", " ")
    normalized = normalized.replace("_", " ")
    normalized = normalized.replace("-", " ")
    # Remove brackets and their variations
    for char in "[](){}":
        normalized = normalized.replace(char, " ")
    # Collapse multiple spaces into one
    while "  " in normalized:
        normalized = normalized.replace("  ", " ")
    # Convert to lowercase for case-insensitive matching
    return normalized.strip().lower()


def sanitize_path_component(name: str) -> str:
    """
    Sanitize a path component (category name or folder name) to prevent path traversal.

    Removes:
    - Parent directory references (..)
    - Absolute path indicators (leading /)
    - Null bytes
    - Other dangerous characters

    Args:
        name: The path component to sanitize

    Returns:
        A safe path component string
    """
    if not name:
        return ""

    # Remove null bytes
    sanitized = name.replace("\x00", "")

    # Remove leading/trailing whitespace
    sanitized = sanitized.strip()

    # Remove absolute path indicators
    sanitized = sanitized.lstrip("/\\")

    # Remove parent directory references
    # Handle both forward and backward slashes
    while ".." in sanitized:
        sanitized = sanitized.replace("..", "")

    # Remove any remaining slashes (we only want a single path component)
    sanitized = sanitized.replace("/", "_").replace("\\", "_")

    # Collapse multiple underscores
    while "__" in sanitized:
        sanitized = sanitized.replace("__", "_")

    # Strip leading/trailing underscores
    sanitized = sanitized.strip("_")

    return sanitized


def safe_join_path(base_path: str, *components: str) -> str:
    """
    Safely join path components, ensuring the result stays within base_path.

    Args:
        base_path: The base directory that must contain the final path
        *components: Path components to join (will be sanitized)

    Returns:
        A safe absolute path within base_path

    Raises:
        ValueError: If the resulting path would escape base_path
    """
    # Sanitize each component
    sanitized_components = [sanitize_path_component(c) for c in components if c]

    # Filter out empty components
    sanitized_components = [c for c in sanitized_components if c]

    # Join the path
    if sanitized_components:
        result = os.path.join(base_path, *sanitized_components)
    else:
        result = base_path

    # Resolve to absolute path
    result = os.path.abspath(result)
    base_abs = os.path.abspath(base_path)

    # Ensure the result is within base_path
    if not result.startswith(base_abs + os.sep) and result != base_abs:
        raise ValueError(
            f"Path traversal detected: {result} is outside {base_abs}"
        )

    return result


class SeedrClientWrapper:
    """
    Wrapper around seedrcc that provides simplified interface for the proxy.
    Handles authentication, token refresh, API calls, storage management,
    queuing, retry logic, and multi-instance support.
    """

    def __init__(
        self,
        email: Optional[str] = None,
        password: Optional[str] = None,
        token: Optional[str] = None,
        download_path: str = "/downloads",
        auto_download: bool = True,
        delete_after_download: bool = True,
        storage_buffer_mb: int = 100,
        state_manager: Optional["StateManager"] = None,
        retry_config: Optional[RetryConfig] = None,
        circuit_config: Optional[CircuitBreakerConfig] = None,
        # QNAP Download Station settings
        qnap_client: Optional["QnapDownloadStationClient"] = None,
        qnap_temp_folder: str = "Download",
        qnap_dest_folder: str = "",
    ):
        self.email = email
        self.password = password
        self._token = token
        self.download_path = download_path
        self.auto_download = auto_download
        self.delete_after_download = delete_after_download
        self.storage_buffer_mb = storage_buffer_mb
        self.storage_buffer_bytes = storage_buffer_mb * 1024 * 1024

        # QNAP Download Station client (optional - if set, uses QNAP for local downloads)
        self._qnap_client = qnap_client
        self._qnap_temp_folder = qnap_temp_folder
        self._qnap_dest_folder = qnap_dest_folder
        self._qnap_folder_progress: dict[str, dict] = {}  # folder_name -> {progress, speed, eta, status}
        self._qnap_last_query: float = 0  # Timestamp of last QNAP query
        self._qnap_query_interval: float = 5.0  # Query QNAP every 5 seconds
        self._qnap_cleaned_folders: set[str] = set()  # Folders whose completed tasks have been removed
        self._qnap_pending_completion: dict[str, dict] = {}  # torrent_hash -> info for persistence when QNAP completes

        self._client = None
        # Lock ordering to prevent deadlocks (always acquire in this order):
        # 1. _lock (main operations lock - protects API calls and cache)
        # 2. _queue_lock (queue operations)
        # 3. _download_lock (download operations)
        # 4. _mapping_lock (category/instance/hash mappings)
        # 5. _qnap_lock (QNAP tracking data)
        # 6. _error_lock (error tracking)
        # Note: Most operations only need one lock. Nested locking is rare.
        self._lock = asyncio.Lock()
        self._download_lock = asyncio.Lock()
        self._queue_lock = asyncio.Lock()
        self._mapping_lock = asyncio.Lock()
        self._qnap_lock = asyncio.Lock()

        # State manager for persistence
        self._state_manager = state_manager

        # Retry, circuit breaker, and rate limiter
        self._retry_handler = RetryHandler(retry_config or RetryConfig())
        self._circuit_breaker = CircuitBreaker(
            circuit_config or CircuitBreakerConfig(),
            name="seedr_api"
        )
        self._rate_limiter = RateLimiter(rate=5.0, burst=10)  # 5 req/s, burst of 10

        # Caches and state
        self._torrents_cache: dict[str, SeedrTorrent] = {}
        self._category_mapping: dict[str, str] = {}  # torrent_hash -> category
        self._instance_mapping: dict[str, str] = {}  # torrent_hash -> instance_id
        self._download_tasks: dict[str, asyncio.Task] = {}  # torrent_hash -> download task
        self._download_progress: dict[str, float] = {}  # torrent_hash -> local download progress
        self._local_downloads: set[str] = set()  # Set of torrent hashes fully downloaded locally
        self._active_downloads: dict[str, int] = {}  # torrent_hash -> bytes downloaded per second

        # Error tracking
        self._error_counts: dict[str, int] = {}  # torrent_hash -> error count
        self._last_errors: dict[str, str] = {}  # torrent_hash -> last error message
        self._download_retry_after: dict[str, float] = {}  # torrent_hash -> timestamp when retry is allowed
        self._error_lock = asyncio.Lock()  # Lock for error tracking
        self._max_error_entries = 1000  # Maximum entries before pruning

        # Queue system for storage management
        self._torrent_queue: deque[QueuedTorrent] = deque()
        self._queue_counter: int = 0
        self._queue_processing: bool = False

        # Hash mapping: queue_hash -> real_hash (for Sonarr tracking)
        self._hash_mapping: dict[str, str] = {}

        # Name to hash mapping: torrent_name -> original_hash (for folder transition tracking)
        self._name_to_hash_mapping: dict[str, str] = {}

        # Storage info cache
        self._storage_used: int = 0
        self._storage_max: int = 0
        self._last_storage_check: float = 0

        # Cache staleness tracking
        self._last_successful_api_call: float = 0
        self._cache_max_age: float = 300.0  # 5 minutes max cache age

    async def initialize(self):
        """Initialize the Seedr client."""
        try:
            from seedrcc import AsyncSeedr, Token

            if self._token:
                try:
                    token_obj = Token.from_json(self._token)
                    self._client = AsyncSeedr(token=token_obj)
                except Exception:
                    if self.email and self.password:
                        self._client = await AsyncSeedr.from_password(
                            self.email, self.password
                        )
                    else:
                        raise ValueError("Invalid token and no email/password provided")
            elif self.email and self.password:
                self._client = await AsyncSeedr.from_password(
                    self.email, self.password
                )
            else:
                raise ValueError("Either token or email/password required")

            # Get initial storage info
            await self._update_storage_info()

            # Restore state from persistence if available
            await self._restore_state()

            logger.info(
                f"Seedr client initialized. Storage: "
                f"{self._storage_used / 1024 / 1024:.1f}MB / "
                f"{self._storage_max / 1024 / 1024:.1f}MB"
            )
            return True
        except ImportError:
            logger.error("seedrcc library not installed. Run: pip install seedrcc")
            raise
        except Exception as e:
            logger.error(f"Failed to initialize Seedr client: {e}")
            raise

    async def _restore_state(self):
        """Restore state from state manager."""
        if not self._state_manager:
            return

        try:
            # Restore local downloads and mappings
            for torrent in await self._state_manager.get_torrents():
                if torrent.phase == TorrentPhase.COMPLETED:
                    self._local_downloads.add(torrent.hash)
                if torrent.category:
                    self._category_mapping[torrent.hash] = torrent.category
                    self._instance_mapping[torrent.hash] = torrent.instance_id
                # Always restore name → hash mapping for folder transitions (even without category)
                # Use normalized name for consistent lookups
                if torrent.name:
                    self._name_to_hash_mapping[normalize_folder_name(torrent.name)] = torrent.hash

            # Restore queue
            for queued in await self._state_manager.get_queue():
                self._torrent_queue.append(QueuedTorrent(
                    id=queued.id,
                    magnet_link=queued.magnet_link,
                    torrent_file=queued.torrent_file,
                    category=queued.category,
                    instance_id=queued.instance_id,
                    added_time=queued.added_time,
                    name=queued.name,
                    estimated_size=queued.estimated_size,
                    retry_count=queued.retry_count,
                ))
                # Update queue counter
                try:
                    counter = int(queued.id)
                    if counter > self._queue_counter:
                        self._queue_counter = counter
                except ValueError:
                    pass

            # Restore QNAP pending downloads
            for pending in await self._state_manager.get_qnap_pending():
                self._qnap_pending_completion[pending.torrent_hash] = {
                    "folder_id": pending.folder_id,
                    "folder_name": pending.folder_name,
                    "total_size": pending.total_size,
                    "category": pending.category,
                    "instance_id": pending.instance_id,
                    "save_path": pending.save_path,
                    "content_path": pending.content_path,
                }
                # Also restore category mapping
                if pending.category:
                    self._category_mapping[pending.torrent_hash] = pending.category
                    self._instance_mapping[pending.torrent_hash] = pending.instance_id
                # Restore name mapping (normalized for consistent lookups)
                if pending.folder_name:
                    self._name_to_hash_mapping[normalize_folder_name(pending.folder_name)] = pending.torrent_hash

            # Restore name hash mappings from persistent storage
            # This ensures category recovery works across server restarts
            name_hash_mappings = await self._state_manager.get_all_name_hash_mappings()
            for mapping in name_hash_mappings:
                self._name_to_hash_mapping[mapping.normalized_name] = mapping.torrent_hash
                # Also restore category/instance mappings if not already set
                if mapping.category and mapping.torrent_hash not in self._category_mapping:
                    self._category_mapping[mapping.torrent_hash] = mapping.category
                    self._instance_mapping[mapping.torrent_hash] = mapping.instance_id

            logger.info(
                f"Restored state: {len(self._local_downloads)} local downloads, "
                f"{len(self._torrent_queue)} queued torrents, "
                f"{len(self._qnap_pending_completion)} QNAP pending, "
                f"{len(name_hash_mappings)} name hash mappings"
            )

        except Exception as e:
            logger.warning(f"Failed to restore state: {e}")

    async def close(self):
        """Close the client connection."""
        # Cancel all download tasks
        for task in self._download_tasks.values():
            task.cancel()
        self._download_tasks.clear()

        if self._client:
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None

    async def get_token(self) -> Optional[str]:
        """Get the current token for saving."""
        if self._client and hasattr(self._client, 'token'):
            return self._client.token.to_json()
        return None

    async def _update_storage_info(self):
        """Update cached storage information."""
        try:
            usage = await self._client.get_memory_bandwidth()
            self._storage_used = getattr(usage, 'space_used', 0) or 0
            self._storage_max = getattr(usage, 'space_max', 0) or 0
            self._last_storage_check = datetime.now().timestamp()
            logger.debug(
                f"Storage updated: {self._storage_used / 1024 / 1024:.1f}MB / "
                f"{self._storage_max / 1024 / 1024:.1f}MB"
            )
        except Exception as e:
            logger.warning(f"Failed to update storage info: {e}")

    def get_available_storage(self) -> int:
        """Get available storage in bytes (with buffer)."""
        available = self._storage_max - self._storage_used - self.storage_buffer_bytes
        return max(0, available)

    def _parse_progress(self, progress_str: str) -> float:
        """Parse progress string to float (0.0 to 1.0)."""
        try:
            progress = float(str(progress_str).replace('%', '').strip())
            return progress / 100.0 if progress > 1 else progress
        except (ValueError, TypeError):
            return 0.0

    def _get_save_path(self, category: str = "") -> str:
        """Get the save path for a category."""
        if category:
            return safe_join_path(self.download_path, category)
        return self.download_path

    def _determine_phase(
        self,
        is_queued: bool,
        seedr_progress: float,
        is_downloading_local: bool,
        is_local: bool,
        has_error: bool,
    ) -> TorrentPhase:
        """Determine the current phase of a torrent."""
        if has_error:
            return TorrentPhase.ERROR
        if is_queued:
            return TorrentPhase.QUEUED_STORAGE
        if is_local:
            return TorrentPhase.COMPLETED
        if is_downloading_local:
            return TorrentPhase.DOWNLOADING_TO_LOCAL
        if seedr_progress >= 1.0:
            return TorrentPhase.SEEDR_COMPLETE
        if seedr_progress > 0:
            return TorrentPhase.DOWNLOADING_TO_SEEDR
        return TorrentPhase.FETCHING_METADATA

    async def get_torrents(self) -> list[SeedrTorrent]:
        """Get all torrents from Seedr, including queued ones."""
        if not self._client:
            await self.initialize()

        # Check circuit breaker
        if not await self._circuit_breaker.can_execute():
            cache_age = datetime.now().timestamp() - self._last_successful_api_call
            if cache_age > self._cache_max_age:
                logger.warning(
                    f"Circuit breaker open and cache stale ({cache_age:.0f}s old). "
                    "Data may be outdated."
                )
            else:
                logger.warning("Circuit breaker open, returning cached torrents")
            return list(self._torrents_cache.values())

        async with self._lock:
            try:
                torrents = []

                # Update storage info periodically (every 60 seconds)
                if datetime.now().timestamp() - self._last_storage_check > 60:
                    await self._update_storage_info()

                # Update QNAP progress if enabled
                if self._qnap_client:
                    await self._update_qnap_progress()

                # Get root folder contents with retry and rate limiting
                await self._rate_limiter.acquire_or_raise()
                contents = await self._retry_handler.with_retry(
                    operation=lambda: self._client.list_contents(),
                    operation_id="list_contents",
                    max_attempts=3,
                )
                await self._circuit_breaker.record_success()
                self._last_successful_api_call = datetime.now().timestamp()

                # Process active torrents (downloading in Seedr from internet)
                for transfer in contents.torrents:
                    torrent_hash = transfer.hash.upper() if transfer.hash else f"SEEDR{transfer.id}"

                    seedr_progress = self._parse_progress(transfer.progress)

                    if getattr(transfer, 'stopped', 0):
                        state = TorrentState.PAUSED
                    elif seedr_progress >= 1.0:
                        state = TorrentState.COMPLETED
                    elif seedr_progress > 0:
                        state = TorrentState.DOWNLOADING
                    else:
                        state = TorrentState.QUEUED

                    size = transfer.size or 0
                    category = self._category_mapping.get(torrent_hash, "")
                    instance_id = self._instance_mapping.get(torrent_hash, extract_instance_id(category))

                    # Track name → hash mapping for folder transition
                    # IMPORTANT: Always track this, even if no category - needed for hash transitions
                    # Use normalized name for consistent lookups (Seedr may change + to spaces, etc.)
                    if transfer.name:
                        self._name_to_hash_mapping[normalize_folder_name(transfer.name)] = torrent_hash

                    # Seedr progress maps to 0-50% of total progress
                    effective_progress = seedr_progress * 0.5

                    # Calculate ETA for Seedr download phase
                    # Since this is only the first 50%, we need to estimate total time
                    download_rate = getattr(transfer, 'download_rate', 0) or 0
                    if download_rate > 0 and size > 0:
                        # Time remaining for Seedr phase
                        seedr_remaining = size * (1.0 - seedr_progress)
                        seedr_eta = int(seedr_remaining / download_rate)
                        # Double it roughly to account for local download phase
                        eta = seedr_eta * 2
                    else:
                        eta = 8640000  # Unknown

                    phase = self._determine_phase(
                        is_queued=False,
                        seedr_progress=seedr_progress,
                        is_downloading_local=False,
                        is_local=False,
                        has_error=torrent_hash in self._last_errors,
                    )

                    torrent = SeedrTorrent(
                        id=str(transfer.id),
                        hash=torrent_hash,
                        name=transfer.name,
                        size=size,
                        progress=effective_progress,
                        state=state,
                        download_speed=download_rate,
                        upload_speed=getattr(transfer, 'upload_rate', 0) or 0,
                        eta=eta,
                        seeders=getattr(transfer, 'seeders', 0) or 0,
                        leechers=getattr(transfer, 'leechers', 0) or 0,
                        added_on=int(datetime.now().timestamp()),
                        save_path=self._get_save_path(category),
                        category=category,
                        instance_id=instance_id,
                        downloaded=int(size * effective_progress),
                        completed=int(size * effective_progress),
                        phase=phase,
                        error_count=self._error_counts.get(torrent_hash, 0),
                        last_error=self._last_errors.get(torrent_hash),
                    )
                    torrents.append(torrent)
                    self._torrents_cache[torrent_hash] = torrent

                # Process completed folders
                for folder in contents.folders:
                    torrent_hash = f"SEEDR{folder.id:016X}".upper()

                    last_update = folder.last_update
                    timestamp = int(last_update.timestamp()) if last_update else int(datetime.now().timestamp())

                    # Try to get category from direct mapping first
                    category = self._category_mapping.get(torrent_hash, "")
                    instance_id = self._instance_mapping.get(torrent_hash, "")

                    # If no category found, try to look up by name (for hash transitions)
                    # Use normalized name for lookups (Seedr may change + to spaces, etc.)
                    normalized_folder_name = normalize_folder_name(folder.name)

                    # First try: use persistent name hash mapping (survives restarts)
                    if not category and self._state_manager:
                        mapping = await self._state_manager.get_name_hash_mapping(
                            normalized_folder_name, instance_id=instance_id if instance_id else None
                        )
                        if mapping and mapping.category:
                            original_hash = mapping.torrent_hash
                            category = mapping.category
                            instance_id = mapping.instance_id or extract_instance_id(category)
                            # Copy mapping to new hash for future lookups
                            self._category_mapping[torrent_hash] = category
                            self._instance_mapping[torrent_hash] = instance_id
                            # Update name mapping to point to new hash
                            self._name_to_hash_mapping[normalized_folder_name] = torrent_hash
                            # Persist the hash transition
                            await self._state_manager.update_name_hash_transition(
                                normalized_folder_name, original_hash, torrent_hash, instance_id
                            )
                            await self._state_manager.add_hash_mapping(original_hash, torrent_hash)
                            logger.info(f"Hash transition detected (persistent): {original_hash} -> {torrent_hash} (category: {category})")

                    # Second try: in-memory name mapping (for same-session transitions)
                    if not category and normalized_folder_name in self._name_to_hash_mapping:
                        original_hash = self._name_to_hash_mapping[normalized_folder_name]
                        category = self._category_mapping.get(original_hash, "")
                        instance_id = self._instance_mapping.get(original_hash, "")
                        if category:
                            # Copy mapping to new hash for future lookups
                            self._category_mapping[torrent_hash] = category
                            self._instance_mapping[torrent_hash] = instance_id
                            # Also update name mapping to point to new hash
                            self._name_to_hash_mapping[normalized_folder_name] = torrent_hash
                            # Persist the hash transition
                            if self._state_manager:
                                await self._state_manager.update_name_hash_transition(
                                    normalized_folder_name, original_hash, torrent_hash, instance_id
                                )
                                await self._state_manager.add_hash_mapping(original_hash, torrent_hash)
                            logger.info(f"Hash transition detected (in-memory): {original_hash} -> {torrent_hash} (category: {category})")

                    # Fallback: try fuzzy name match in persistence if category still not found
                    # This handles cases where Seedr significantly changed the folder name
                    if not category and self._state_manager:
                        persisted = await self._state_manager.find_torrent_by_name(folder.name)
                        if persisted and persisted.category:
                            category = persisted.category
                            instance_id = persisted.instance_id or extract_instance_id(category)
                            # Cache for future lookups
                            self._category_mapping[torrent_hash] = category
                            self._instance_mapping[torrent_hash] = instance_id
                            self._name_to_hash_mapping[normalized_folder_name] = torrent_hash
                            if self._state_manager:
                                await self._state_manager.add_hash_mapping(persisted.hash, torrent_hash)
                            logger.info(f"Category recovered via fuzzy name match: {folder.name} -> {category} (from {persisted.name})")

                    if not instance_id:
                        instance_id = extract_instance_id(category)
                    save_path = self._get_save_path(category)
                    content_path = os.path.join(save_path, folder.name)

                    is_local = torrent_hash in self._local_downloads
                    local_progress = self._download_progress.get(torrent_hash, 0.0)
                    is_downloading = torrent_hash in self._download_tasks

                    # Get QNAP progress for this folder if available
                    qnap_info = self._get_qnap_progress(folder.name) if self._qnap_client else {}
                    qnap_progress = qnap_info.get("progress", 0.0)
                    qnap_speed = qnap_info.get("speed", 0)
                    qnap_eta = qnap_info.get("eta", 8640000)
                    qnap_status = qnap_info.get("status", "unknown")
                    # has_qnap_tasks = QNAP is actively handling this folder (downloading or waiting)
                    has_qnap_tasks = qnap_status in ("downloading", "waiting")
                    # qnap_completed = QNAP finished downloading this folder
                    qnap_completed = qnap_status == "completed"

                    if is_local:
                        state = TorrentState.COMPLETED_LOCAL
                        local_progress = 1.0
                    elif qnap_completed:
                        # QNAP finished downloading - mark as complete
                        state = TorrentState.COMPLETED_LOCAL
                        local_progress = 1.0
                        self._local_downloads.add(torrent_hash)
                        is_local = True

                        # Persist completion if we have pending info
                        async with self._qnap_lock:
                            pending = self._qnap_pending_completion.pop(torrent_hash, None)
                        if pending and self._state_manager:
                            logger.info(f"QNAP completed - persisting torrent: {pending['folder_name']}")
                            await self._state_manager.mark_local_download(torrent_hash)
                            await self._state_manager.save_completed_torrent(
                                hash=torrent_hash,
                                seedr_id=pending["folder_id"],
                                name=pending["folder_name"],
                                size=pending["total_size"],
                                category=pending["category"],
                                instance_id=pending["instance_id"],
                                save_path=pending["save_path"],
                                content_path=pending["content_path"],
                            )
                            # Remove from pending persistence
                            await self._state_manager.delete_qnap_pending(torrent_hash)

                            # NOW delete from Seedr since QNAP is done downloading
                            if self.delete_after_download:
                                try:
                                    folder_id = pending["folder_id"]
                                    await self._client.delete_folder(folder_id)
                                    logger.info(f"QNAP complete - deleted from Seedr: {pending['folder_name']}")
                                    await self._update_storage_info()
                                    await self._process_queue()
                                except Exception as e:
                                    logger.warning(f"Failed to delete from Seedr after QNAP completion: {e}")
                    elif has_qnap_tasks:
                        # QNAP is actively downloading/waiting
                        state = TorrentState.DOWNLOADING_LOCAL
                    elif is_downloading:
                        state = TorrentState.DOWNLOADING_LOCAL
                    else:
                        state = TorrentState.COMPLETED

                    # Calculate effective progress:
                    # 0-50%: Seedr download (from internet to Seedr cloud)
                    # 51-100%: Local download (from Seedr to local, via QNAP or direct)
                    if is_local:
                        effective_progress = 1.0
                    elif has_qnap_tasks:
                        # QNAP is handling the download - use QNAP progress for 51-100% range
                        effective_progress = 0.5 + (qnap_progress * 0.5)
                    elif is_downloading:
                        effective_progress = 0.5 + (local_progress * 0.5)
                    else:
                        effective_progress = 0.5

                    # Use QNAP speed if available, otherwise use tracked speed
                    if has_qnap_tasks and qnap_speed > 0:
                        download_speed = qnap_speed
                    elif torrent_hash in self._active_downloads:
                        download_speed = self._active_downloads.get(torrent_hash, 0)
                    else:
                        download_speed = 0

                    phase = self._determine_phase(
                        is_queued=False,
                        seedr_progress=1.0,
                        is_downloading_local=is_downloading or has_qnap_tasks,
                        is_local=is_local,
                        has_error=torrent_hash in self._last_errors,
                    )

                    # Calculate ETA
                    if is_local:
                        eta = 0  # Complete
                    elif has_qnap_tasks:
                        eta = qnap_eta  # Use QNAP's ETA
                    elif download_speed > 0:
                        remaining = folder.size * (1.0 - effective_progress)
                        eta = int(remaining / download_speed) if download_speed > 0 else 8640000
                    else:
                        eta = 8640000  # Unknown

                    torrent = SeedrTorrent(
                        id=str(folder.id),
                        hash=torrent_hash,
                        name=folder.name,
                        size=folder.size,
                        progress=effective_progress if self.auto_download else 1.0,
                        state=state,
                        download_speed=download_speed,
                        eta=eta,
                        added_on=timestamp,
                        completion_on=timestamp if is_local else 0,
                        save_path=save_path,
                        content_path=content_path,
                        category=category,
                        instance_id=instance_id,
                        downloaded=int(folder.size * effective_progress) if self.auto_download else folder.size,
                        completed=int(folder.size * effective_progress) if self.auto_download else folder.size,
                        local_progress=local_progress,
                        is_local=is_local,
                        phase=phase,
                        error_count=self._error_counts.get(torrent_hash, 0),
                        last_error=self._last_errors.get(torrent_hash),
                    )
                    torrents.append(torrent)
                    self._torrents_cache[torrent_hash] = torrent

                    # Start auto-download if enabled
                    # Skip if: already local, already downloading, QNAP is handling it, or pending QNAP completion
                    is_pending_qnap = torrent_hash in self._qnap_pending_completion
                    should_download = self.auto_download and not is_local and not has_qnap_tasks and not is_pending_qnap and torrent_hash not in self._download_tasks
                    if should_download:
                        self._start_download_task(torrent_hash, folder.id, folder.name, save_path)
                    elif self.auto_download and not is_local:
                        # Log why we're not downloading
                        reasons = []
                        if has_qnap_tasks:
                            reasons.append(f"QNAP has tasks (status={qnap_status})")
                        if is_pending_qnap:
                            reasons.append("pending QNAP completion")
                        if torrent_hash in self._download_tasks:
                            reasons.append("download task already running")
                        if reasons:
                            logger.debug(f"Skipping auto-download for {folder.name}: {', '.join(reasons)}")

                # Add queued torrents
                for queued in self._torrent_queue:
                    queue_hash = f"QUEUE{queued.id}".upper()
                    category = queued.category
                    instance_id = queued.instance_id or extract_instance_id(category)
                    save_path = self._get_save_path(category)

                    torrent = SeedrTorrent(
                        id=queued.id,
                        hash=queue_hash,
                        name=queued.name,
                        size=queued.estimated_size,
                        progress=0.0,
                        state=TorrentState.QUEUED_STORAGE,
                        added_on=int(queued.added_time),
                        save_path=save_path,
                        category=category,
                        instance_id=instance_id,
                        is_queued=True,
                        phase=TorrentPhase.QUEUED_STORAGE,
                        error_count=queued.retry_count,
                        last_error=queued.last_error,
                    )
                    torrents.append(torrent)
                    self._torrents_cache[queue_hash] = torrent

                # Add mapped queue hashes
                for queue_hash, real_hash in self._hash_mapping.items():
                    if real_hash in self._torrents_cache and queue_hash not in self._torrents_cache:
                        real_torrent = self._torrents_cache[real_hash]
                        mapped_torrent = SeedrTorrent(
                            id=real_torrent.id,
                            hash=queue_hash,
                            name=real_torrent.name,
                            size=real_torrent.size,
                            progress=real_torrent.progress,
                            state=real_torrent.state,
                            download_speed=real_torrent.download_speed,
                            upload_speed=real_torrent.upload_speed,
                            added_on=real_torrent.added_on,
                            completion_on=real_torrent.completion_on,
                            save_path=real_torrent.save_path,
                            content_path=real_torrent.content_path,
                            category=real_torrent.category,
                            instance_id=real_torrent.instance_id,
                            downloaded=real_torrent.downloaded,
                            completed=real_torrent.completed,
                            local_progress=real_torrent.local_progress,
                            is_local=real_torrent.is_local,
                            phase=real_torrent.phase,
                            error_count=real_torrent.error_count,
                            last_error=real_torrent.last_error,
                        )
                        torrents.append(mapped_torrent)
                        self._torrents_cache[queue_hash] = mapped_torrent

                # Add pending QNAP downloads (deleted from Seedr but still downloading via QNAP)
                seen_hashes = {t.hash for t in torrents}
                pending_to_remove = []  # Track items to remove after iteration
                # Take snapshot under lock to avoid race condition during iteration
                async with self._qnap_lock:
                    pending_snapshot = list(self._qnap_pending_completion.items())
                for pending_hash, pending in pending_snapshot:
                    if pending_hash not in seen_hashes:
                        # Get QNAP progress for this folder
                        qnap_info = self._get_qnap_progress(pending["folder_name"]) if self._qnap_client else {}
                        qnap_progress = qnap_info.get("progress", 0.0)
                        qnap_speed = qnap_info.get("speed", 0)
                        qnap_status = qnap_info.get("status", "unknown")

                        # Check if QNAP completed
                        if qnap_status == "completed":
                            state = TorrentState.COMPLETED_LOCAL
                            local_progress = 1.0
                            self._local_downloads.add(pending_hash)
                            # Persist completion
                            if self._state_manager:
                                logger.info(f"QNAP completed (pending) - persisting: {pending['folder_name']}")
                                await self._state_manager.mark_local_download(pending_hash)
                                await self._state_manager.save_completed_torrent(
                                    hash=pending_hash,
                                    seedr_id=pending["folder_id"],
                                    name=pending["folder_name"],
                                    size=pending["total_size"],
                                    category=pending["category"],
                                    instance_id=pending["instance_id"],
                                    save_path=pending["save_path"],
                                    content_path=pending["content_path"],
                                )
                                # Remove from pending persistence
                                await self._state_manager.delete_qnap_pending(pending_hash)

                            # NOW delete from Seedr since QNAP is done downloading
                            if self.delete_after_download:
                                try:
                                    folder_id = pending["folder_id"]
                                    await self._client.delete_folder(folder_id)
                                    logger.info(f"QNAP complete (pending) - deleted from Seedr: {pending['folder_name']}")
                                    await self._update_storage_info()
                                    await self._process_queue()
                                except Exception as e:
                                    # Folder might already be deleted, that's OK
                                    logger.debug(f"Could not delete from Seedr after QNAP completion: {e}")

                            # Mark for removal after iteration
                            pending_to_remove.append(pending_hash)
                            phase = TorrentPhase.COMPLETED
                        elif qnap_status in ("downloading", "waiting"):
                            state = TorrentState.DOWNLOADING_LOCAL
                            local_progress = qnap_progress
                            phase = TorrentPhase.DOWNLOADING_TO_LOCAL
                        else:
                            state = TorrentState.DOWNLOADING_LOCAL
                            local_progress = 0.5
                            phase = TorrentPhase.DOWNLOADING_TO_LOCAL

                        # Progress: 50% (Seedr done) + 50% * QNAP progress
                        effective_progress = 0.5 + (0.5 * local_progress)

                        torrent = SeedrTorrent(
                            id=pending["folder_id"],
                            hash=pending_hash,
                            name=pending["folder_name"],
                            size=pending["total_size"],
                            progress=effective_progress,
                            state=state,
                            download_speed=qnap_speed,
                            upload_speed=0,
                            added_on=int(datetime.now().timestamp()),
                            completion_on=int(datetime.now().timestamp()) if local_progress >= 1.0 else 0,
                            save_path=pending["save_path"],
                            content_path=pending["content_path"],
                            category=pending["category"],
                            instance_id=pending["instance_id"],
                            downloaded=int(pending["total_size"] * effective_progress),
                            completed=int(pending["total_size"] * local_progress),
                            local_progress=local_progress,
                            is_local=local_progress >= 1.0,
                            phase=phase,
                        )
                        torrents.append(torrent)
                        self._torrents_cache[pending_hash] = torrent
                        seen_hashes.add(pending_hash)
                        logger.debug(f"Added pending QNAP torrent: {pending['folder_name']} (status={qnap_status})")

                # Remove completed pending items after iteration
                if pending_to_remove:
                    async with self._qnap_lock:
                        for h in pending_to_remove:
                            self._qnap_pending_completion.pop(h, None)

                # Add persisted completed torrents that are no longer in Seedr
                # This ensures completed downloads still appear after Seedr deletion
                if self._state_manager:
                    for persisted in await self._state_manager.get_torrents():
                        if persisted.hash not in seen_hashes and persisted.phase == TorrentPhase.COMPLETED:
                            # This torrent was completed and deleted from Seedr
                            torrent = SeedrTorrent(
                                id=persisted.seedr_id or "",
                                hash=persisted.hash,
                                name=persisted.name,
                                size=persisted.size,
                                progress=1.0,
                                state=TorrentState.COMPLETED_LOCAL,
                                download_speed=0,
                                upload_speed=0,
                                added_on=persisted.added_on,
                                completion_on=persisted.added_on,
                                save_path=persisted.save_path,
                                content_path=persisted.content_path or "",
                                category=persisted.category,
                                instance_id=persisted.instance_id,
                                downloaded=persisted.size,
                                completed=persisted.size,
                                local_progress=1.0,
                                is_local=True,
                                phase=TorrentPhase.COMPLETED,
                            )
                            torrents.append(torrent)
                            self._torrents_cache[persisted.hash] = torrent
                            logger.debug(f"Added persisted completed torrent: {persisted.name}")

                return torrents

            except Exception as e:
                await self._circuit_breaker.record_failure()
                logger.error(f"Error getting torrents: {e}")
                return list(self._torrents_cache.values())

    def _start_download_task(self, torrent_hash: str, folder_id: int, folder_name: str, save_path: str):
        """Start a background task to download a folder from Seedr."""
        if torrent_hash in self._download_tasks:
            logger.debug(f"Download task already running for {folder_name}")
            return

        # Check if we need to wait before retrying (cooldown after failures)
        retry_after = self._download_retry_after.get(torrent_hash, 0)
        now = datetime.now().timestamp()
        if retry_after > now:
            remaining = int(retry_after - now)
            logger.debug(f"Download for {folder_name} in cooldown, {remaining}s remaining")
            return  # Still in cooldown period

        logger.info(f"Starting download task for {folder_name} (folder_id={folder_id})")
        task = asyncio.create_task(
            self._download_folder(torrent_hash, str(folder_id), folder_name, save_path)
        )
        self._download_tasks[torrent_hash] = task

        def cleanup(t):
            self._download_tasks.pop(torrent_hash, None)
            self._active_downloads.pop(torrent_hash, None)

        task.add_done_callback(cleanup)

    async def _update_qnap_progress(self):
        """Query QNAP Download Station and update progress tracking for active tasks."""
        if not self._qnap_client:
            return

        now = datetime.now().timestamp()
        if now - self._qnap_last_query < self._qnap_query_interval:
            return  # Rate limit queries

        self._qnap_last_query = now

        try:
            tasks = await self._qnap_client.query_tasks(limit=200)

            # Group tasks by destination folder
            folder_tasks: dict[str, list] = {}
            for task in tasks:
                # Extract folder name from destination path
                # e.g., "plex/downloads/tv-sonarr/Show.Name.S01E01" -> "Show.Name.S01E01"
                dest = task.destination or ""
                if "/" in dest:
                    folder_name = dest.rsplit("/", 1)[-1]
                else:
                    folder_name = dest

                if folder_name:
                    if folder_name not in folder_tasks:
                        folder_tasks[folder_name] = []
                    folder_tasks[folder_name].append(task)

            # Calculate aggregate progress per folder and clean up completed tasks
            new_progress = {}
            tasks_to_remove = []  # Collect completed task IDs to remove

            for folder_name, tasks_list in folder_tasks.items():
                total_size = sum(t.size for t in tasks_list)
                total_downloaded = sum(t.downloaded for t in tasks_list)
                total_speed = sum(t.download_speed for t in tasks_list)

                if total_size > 0:
                    progress = total_downloaded / total_size
                else:
                    progress = 0.0

                # Calculate ETA based on remaining bytes and current speed
                remaining = total_size - total_downloaded
                if total_speed > 0:
                    eta = int(remaining / total_speed)
                else:
                    eta = 8640000  # Unknown/infinite

                # Determine overall status
                statuses = [t.status.name for t in tasks_list]
                if all(s == "COMPLETED" for s in statuses):
                    status = "completed"
                    # Mark completed tasks for removal if not already cleaned
                    if folder_name not in self._qnap_cleaned_folders:
                        for task in tasks_list:
                            tasks_to_remove.append((task.id, folder_name))
                elif any(s == "ERROR" for s in statuses):
                    status = "error"
                elif any(s == "DOWNLOADING" for s in statuses):
                    status = "downloading"
                else:
                    status = "waiting"

                new_progress[folder_name] = {
                    "progress": progress,
                    "speed": total_speed,
                    "eta": eta,
                    "status": status,
                    "total_size": total_size,
                    "downloaded": total_downloaded,
                    "task_count": len(tasks_list),
                }

            # Remove completed tasks from QNAP Download Station
            # Collect folders to clean outside the lock, then remove tasks
            # Track removal success per folder - only mark as cleaned if ALL tasks removed
            folder_removal_results: dict[str, dict] = {}  # folder_name -> {total: int, removed: int}
            if tasks_to_remove:
                for task_id, folder_name in tasks_to_remove:
                    if folder_name not in folder_removal_results:
                        folder_removal_results[folder_name] = {"total": 0, "removed": 0}
                    folder_removal_results[folder_name]["total"] += 1

                    try:
                        success = await self._qnap_client.remove_task(task_id)
                        if success:
                            logger.info(f"Removed completed QNAP task {task_id} for {folder_name}")
                            folder_removal_results[folder_name]["removed"] += 1
                        else:
                            logger.warning(f"Failed to remove QNAP task {task_id}")
                    except Exception as e:
                        logger.warning(f"Error removing QNAP task {task_id}: {e}")

            # Only mark folders as cleaned if ALL their tasks were successfully removed
            folders_cleaned = {
                folder_name for folder_name, results in folder_removal_results.items()
                if results["removed"] == results["total"]
            }

            # Update tracking data with lock protection
            async with self._qnap_lock:
                self._qnap_folder_progress = new_progress

                # Mark folders as cleaned so we don't try to remove again
                if folders_cleaned:
                    self._qnap_cleaned_folders.update(folders_cleaned)

                # Clean up _qnap_cleaned_folders - remove entries for folders no longer in QNAP
                # This prevents the set from growing indefinitely
                current_folders = set(folder_tasks.keys())
                stale_cleaned = self._qnap_cleaned_folders - current_folders
                if stale_cleaned:
                    self._qnap_cleaned_folders -= stale_cleaned
                    logger.debug(f"Cleaned up {len(stale_cleaned)} stale folder entries from tracking")

        except Exception as e:
            logger.warning(f"Failed to query QNAP tasks: {e}")

    def _get_qnap_progress(self, folder_name: str) -> dict:
        """Get QNAP download progress for a folder."""
        return self._qnap_folder_progress.get(folder_name, {
            "progress": 0.0,
            "speed": 0,
            "eta": 8640000,
            "status": "unknown",
        })

    async def _download_folder(self, torrent_hash: str, folder_id: str, folder_name: str, save_path: str):
        """Download all files from a Seedr folder to local storage.

        If QNAP client is configured, uses QNAP Download Station to pull files.
        Otherwise, downloads directly via HTTP.
        """
        if self._qnap_client:
            await self._download_folder_via_qnap(torrent_hash, folder_id, folder_name, save_path)
        else:
            await self._download_folder_direct(torrent_hash, folder_id, folder_name, save_path)

    async def _download_folder_via_qnap(self, torrent_hash: str, folder_id: str, folder_name: str, save_path: str):
        """Download files from Seedr using QNAP Download Station.

        This method adds download URLs to QNAP Download Station and then considers the
        handoff complete. QNAP handles the actual downloading independently.
        """
        async with self._download_lock:
            try:
                with LogContext(torrent_hash=torrent_hash, torrent_name=folder_name):
                    logger.info(f"Starting QNAP download of {folder_name}")

                    # Get folder contents to get download URLs
                    await self._rate_limiter.acquire_or_raise()
                    folder = await self._retry_handler.with_retry(
                        operation=lambda: self._client.list_contents(folder_id),
                        operation_id=f"list_folder_{folder_id}",
                        max_attempts=3,
                    )

                    total_size = sum(f.size for f in folder.files)
                    failed_files = []

                    # Determine QNAP destination folder
                    # Structure: {qnap_dest_folder}/{category}/{folder_name}/
                    # Example: plex/downloads/sonarr/Show.Name.S01E01/
                    #
                    # IMPORTANT: We must create the folder on the local filesystem first,
                    # as QNAP Download Station requires the destination folder to exist.
                    # The local /downloads path maps to plex/downloads on QNAP via the bind mount.
                    category = self._category_mapping.get(torrent_hash, "")

                    # Log the lookup attempt for debugging
                    logger.debug(
                        f"Category lookup for {folder_name}: "
                        f"torrent_hash={torrent_hash}, "
                        f"category_mapping={category}, "
                        f"save_path={save_path}"
                    )

                    # If category not in mapping, try to extract from save_path
                    # save_path is like /downloads/tv-sonarr, so strip download_path prefix
                    if not category and save_path and save_path != self.download_path:
                        # Extract category from save_path (e.g., /downloads/tv-sonarr -> tv-sonarr)
                        relative_path = save_path.replace(self.download_path, "").strip("/")
                        if relative_path:
                            category = relative_path.split("/")[0]
                            logger.info(f"Extracted category from save_path: {category}")
                            # Store it for future lookups
                            self._category_mapping[torrent_hash] = category

                    # If still no category, try looking up via torrent cache
                    # This handles cases where the mapping was lost but cache has it
                    if not category and torrent_hash in self._torrents_cache:
                        cached = self._torrents_cache[torrent_hash]
                        if cached.category:
                            category = cached.category
                            logger.info(f"Found category from cache: {category}")
                            self._category_mapping[torrent_hash] = category

                    # Try looking up by folder name -> hash mapping
                    # Use normalized name for lookups (Seedr may change + to spaces, etc.)
                    normalized_name = normalize_folder_name(folder_name)
                    if not category and normalized_name in self._name_to_hash_mapping:
                        original_hash = self._name_to_hash_mapping[normalized_name]
                        original_category = self._category_mapping.get(original_hash, "")
                        if original_category:
                            category = original_category
                            logger.info(f"Found category via name mapping: {folder_name} -> {original_hash} -> {category}")
                            self._category_mapping[torrent_hash] = category

                    if not category:
                        logger.warning(
                            f"No category found for {folder_name} (hash={torrent_hash}). "
                            f"Files will go to root downloads folder. "
                            f"normalized_name='{normalized_name}', "
                            f"name_mappings={list(self._name_to_hash_mapping.keys())[:5]}..."
                        )

                    # Build local path and create directory (with path traversal protection)
                    safe_folder_name = sanitize_path_component(folder_name)
                    safe_category = sanitize_path_component(category) if category else ""
                    if safe_category:
                        local_dest = safe_join_path(self.download_path, safe_category, safe_folder_name)
                        dest_folder = f"{self._qnap_dest_folder}/{safe_category}/{safe_folder_name}" if self._qnap_dest_folder else ""
                    else:
                        local_dest = safe_join_path(self.download_path, safe_folder_name)
                        dest_folder = f"{self._qnap_dest_folder}/{safe_folder_name}" if self._qnap_dest_folder else ""

                    # Create the destination folder on the local filesystem
                    # This is required because QNAP needs the folder to exist before downloading
                    await asyncio.to_thread(os.makedirs, local_dest, exist_ok=True)
                    # Set permissive permissions so all users can read/write
                    try:
                        await asyncio.to_thread(os.chmod, local_dest, 0o777)
                        # Also set permissions on parent category folder if it exists
                        if safe_category:
                            category_folder = safe_join_path(self.download_path, safe_category)
                            await asyncio.to_thread(os.chmod, category_folder, 0o777)
                    except OSError as e:
                        logger.warning(f"Could not set permissions on {local_dest}: {e}")
                    logger.info(f"Created local destination folder: {local_dest}")

                    logger.info(f"QNAP destination folder: {dest_folder} (category={category}, torrent={folder_name})")

                    # Track successful file additions
                    successful_files = []

                    for idx, file in enumerate(folder.files):
                        try:
                            # Check if file is marked as lost/unavailable
                            if hasattr(file, 'is_lost') and file.is_lost:
                                logger.warning(f"File {file.name} is marked as lost/unavailable, skipping")
                                failed_files.append(file.name)
                                continue

                            folder_file_id = getattr(file, 'folder_file_id', None)
                            if not folder_file_id:
                                logger.error(f"File {file.name} has no folder_file_id, skipping")
                                failed_files.append(file.name)
                                continue

                            # Get download URL from Seedr
                            await self._rate_limiter.acquire_or_raise()
                            file_info = await self._retry_handler.with_retry(
                                operation=lambda fid=folder_file_id: self._client.fetch_file(str(fid)),
                                operation_id=f"fetch_file_{idx}",
                                max_attempts=1,
                                should_retry=lambda e: self._is_file_fetch_retryable(e),
                            )

                            if not file_info or not hasattr(file_info, 'url') or not file_info.url:
                                logger.warning(f"File {file.name} has no download URL, may not be ready")
                                failed_files.append(file.name)
                                continue

                            download_url = file_info.url

                            # Add download task to QNAP
                            # Returns (success, task_id) - task_id may be None even on success
                            success, task_id = await self._qnap_client.add_download(
                                url=download_url,
                                temp_folder=self._qnap_temp_folder,
                                dest_folder=dest_folder,
                            )

                            if success:
                                successful_files.append({
                                    "file_name": file.name,
                                    "size": file.size,
                                    "task_id": task_id,  # May be None
                                })
                                logger.info(f"Added QNAP download for {file.name}")
                            else:
                                logger.error(f"Failed to add QNAP task for {file.name}")
                                failed_files.append(file.name)

                        except Exception as e:
                            logger.error(f"Error adding QNAP download for {file.name}: {e}")
                            await self._record_error(torrent_hash, str(e))
                            failed_files.append(file.name)

                    if not successful_files:
                        logger.error(f"No QNAP downloads added for {folder_name}")
                        await self._record_error(torrent_hash, "No QNAP downloads added")
                        return

                    # QNAP Download Station doesn't always return task IDs, so we can't
                    # reliably monitor individual tasks. Instead, we mark this as handed
                    # off to QNAP and consider it successful. The files will appear in
                    # the destination folder when QNAP finishes downloading.
                    logger.info(
                        f"Added {len(successful_files)} files to QNAP Download Station for {folder_name}. "
                        f"QNAP will handle the downloads independently."
                    )

                    # Check if some files failed to be added to QNAP
                    if failed_files:
                        logger.warning(
                            f"Some files could not be added to QNAP for {folder_name}: "
                            f"{len(failed_files)} file(s) failed: {failed_files}"
                        )
                        # Still continue - the successful files will download

                    # Mark as handed off to QNAP - files will download in background
                    # NOTE: Do NOT add to _local_downloads here - wait until QNAP reports completion
                    # This ensures Sonarr sees the torrent as "downloading" until files are actually ready
                    self._download_progress[torrent_hash] = 0.5  # 50% = handed off, waiting for QNAP
                    logger.info(f"Handed off to QNAP Download Station: {folder_name}")

                    # Clear errors
                    self._error_counts.pop(torrent_hash, None)
                    self._last_errors.pop(torrent_hash, None)
                    self._download_retry_after.pop(torrent_hash, None)

                    # Store torrent info for when QNAP completes (needed for persistence after Seedr deletion)
                    # We'll persist as completed when QNAP reports completion in get_torrents()
                    pending_instance_id = self._instance_mapping.get(torrent_hash, "") or extract_instance_id(category)
                    async with self._qnap_lock:
                        self._qnap_pending_completion[torrent_hash] = {
                            "folder_id": folder_id,
                            "folder_name": folder_name,
                            "total_size": total_size,
                            "category": category,
                            "instance_id": pending_instance_id,
                            "save_path": save_path,
                            "content_path": local_dest,
                        }

                    # Persist to database so we can recover on restart
                    if self._state_manager:
                        await self._state_manager.save_qnap_pending(
                            torrent_hash=torrent_hash,
                            folder_id=folder_id,
                            folder_name=folder_name,
                            total_size=total_size,
                            category=category,
                            instance_id=pending_instance_id,
                            save_path=save_path,
                            content_path=local_dest,
                        )

                    logger.info(
                        f"Stored pending completion info - "
                        f"save_path: {save_path}, content_path: {local_dest}, "
                        f"category: {category}, instance_id: {pending_instance_id}"
                    )

                    # IMPORTANT: Do NOT delete from Seedr yet!
                    # QNAP downloads from Seedr URLs, so we must wait until QNAP completes.
                    # The deletion will happen when we detect QNAP has completed in get_torrents().
                    if failed_files:
                        logger.warning(f"Some files failed to add to QNAP: {len(failed_files)} failed file(s)")
                    else:
                        logger.info(f"All files added to QNAP. Will delete from Seedr when QNAP completes.")

            except asyncio.CancelledError:
                logger.info(f"QNAP download cancelled: {folder_name}")
                self._download_progress.pop(torrent_hash, None)
                self._active_downloads.pop(torrent_hash, None)
                raise
            except Exception as e:
                logger.error(f"Error in QNAP download for {folder_name}: {e}")
                self._download_progress[torrent_hash] = 0.0
                await self._record_error(torrent_hash, str(e))

    async def _download_folder_direct(self, torrent_hash: str, folder_id: str, folder_name: str, save_path: str):
        """Download all files from a Seedr folder directly via HTTP."""
        async with self._download_lock:
            try:
                with LogContext(torrent_hash=torrent_hash, torrent_name=folder_name):
                    logger.info(f"Starting download of {folder_name} to {save_path}")

                    dest_dir = os.path.join(save_path, folder_name)
                    await asyncio.to_thread(os.makedirs, dest_dir, exist_ok=True)
                    try:
                        await asyncio.to_thread(os.chmod, dest_dir, 0o777)
                    except OSError:
                        pass

                    # Get folder contents with retry and rate limiting
                    await self._rate_limiter.acquire_or_raise()
                    folder = await self._retry_handler.with_retry(
                        operation=lambda: self._client.list_contents(folder_id),
                        operation_id=f"list_folder_{folder_id}",
                        max_attempts=3,
                    )

                    total_size = sum(f.size for f in folder.files)
                    downloaded_size = 0
                    failed_files = []

                    for idx, file in enumerate(folder.files):
                        file_dest = os.path.join(dest_dir, file.name)

                        if os.path.exists(file_dest) and os.path.getsize(file_dest) == file.size:
                            downloaded_size += file.size
                            self._download_progress[torrent_hash] = downloaded_size / total_size if total_size > 0 else 1.0
                            continue

                        try:
                            # Check if file is marked as lost/unavailable
                            if hasattr(file, 'is_lost') and file.is_lost:
                                logger.warning(f"File {file.name} is marked as lost/unavailable, skipping")
                                failed_files.append(file.name)
                                continue

                            # Log file details for debugging
                            # IMPORTANT: Seedr API expects folder_file_id, NOT file_id!
                            # seedrcc's fetch_file(file_id) sends it as folder_file_id to the API
                            file_id = getattr(file, 'file_id', None)
                            folder_file_id = getattr(file, 'folder_file_id', None)
                            logger.info(
                                f"File: {file.name}, file_id={file_id}, folder_file_id={folder_file_id}, "
                                f"size={file.size}, is_lost={getattr(file, 'is_lost', 'N/A')}"
                            )

                            # The Seedr API uses folder_file_id - pass this to fetch_file()
                            if not folder_file_id:
                                logger.error(f"File {file.name} has no folder_file_id, skipping")
                                failed_files.append(file.name)
                                continue

                            await self._rate_limiter.acquire_or_raise()
                            file_info = await self._retry_handler.with_retry(
                                operation=lambda fid=folder_file_id: self._client.fetch_file(str(fid)),
                                operation_id=f"fetch_file_{idx}",
                                max_attempts=1,  # Don't retry 401s - they won't resolve by retrying
                                should_retry=lambda e: self._is_file_fetch_retryable(e),
                            )

                            if not file_info or not hasattr(file_info, 'url') or not file_info.url:
                                logger.warning(f"File {file.name} has no download URL, may not be ready")
                                failed_files.append(file.name)
                                continue

                            download_url = file_info.url

                            await self._download_file_with_progress(
                                download_url,
                                file_dest,
                                file.size,
                                torrent_hash,
                                lambda bytes_done: self._update_download_progress(
                                    torrent_hash,
                                    (downloaded_size + bytes_done) / total_size if total_size > 0 else 1.0
                                )
                            )

                            downloaded_size += file.size
                            self._download_progress[torrent_hash] = downloaded_size / total_size if total_size > 0 else 1.0

                            # Clear any previous errors on success
                            self._error_counts.pop(torrent_hash, None)
                            self._last_errors.pop(torrent_hash, None)

                        except Exception as e:
                            logger.error(f"Error downloading file {file.name}: {e}")
                            await self._record_error(torrent_hash, str(e))
                            failed_files.append(file.name)
                            continue

                    # Check if all files were downloaded successfully
                    if failed_files:
                        logger.error(
                            f"Download incomplete for {folder_name}: "
                            f"{len(failed_files)} file(s) failed: {failed_files}"
                        )
                        # Set cooldown before retry (exponential backoff: 60s, 120s, 240s, max 600s)
                        error_count = self._error_counts.get(torrent_hash, 1)
                        cooldown = min(60 * (2 ** (error_count - 1)), 600)
                        self._download_retry_after[torrent_hash] = datetime.now().timestamp() + cooldown
                        logger.info(f"Will retry download of {folder_name} in {cooldown}s")
                        # Don't mark as complete or delete from Seedr
                        return

                    # Download complete - all files succeeded
                    self._download_progress[torrent_hash] = 1.0
                    self._local_downloads.add(torrent_hash)
                    logger.info(f"Download complete: {folder_name}")

                    # Clear errors and retry cooldown on successful completion
                    self._error_counts.pop(torrent_hash, None)
                    self._last_errors.pop(torrent_hash, None)
                    self._download_retry_after.pop(torrent_hash, None)

                    # Persist local download
                    if self._state_manager:
                        await self._state_manager.mark_local_download(torrent_hash)

                    # Delete from Seedr after successful download
                    if self.delete_after_download:
                        try:
                            await self._client.delete_folder(folder_id)
                            logger.info(f"Deleted from Seedr: {folder_name}")

                            await self._update_storage_info()
                            await self._process_queue()

                        except Exception as e:
                            logger.warning(f"Failed to delete from Seedr: {e}")

            except asyncio.CancelledError:
                logger.info(f"Download cancelled: {folder_name}")
                # Clean up partial downloads
                dest_dir = os.path.join(save_path, folder_name)
                if os.path.exists(dest_dir):
                    try:
                        # Check if download was incomplete
                        if torrent_hash not in self._local_downloads:
                            import shutil
                            shutil.rmtree(dest_dir)
                            logger.info(f"Cleaned up partial download: {dest_dir}")
                    except Exception as cleanup_error:
                        logger.warning(f"Failed to clean up partial download: {cleanup_error}")
                # Clear progress tracking
                self._download_progress.pop(torrent_hash, None)
                self._active_downloads.pop(torrent_hash, None)
                raise
            except Exception as e:
                logger.error(f"Error downloading folder {folder_name}: {e}")
                self._download_progress[torrent_hash] = 0.0
                await self._record_error(torrent_hash, str(e))

    def _is_file_fetch_retryable(self, error: Exception) -> bool:
        """Check if a file fetch error should be retried.

        401 Unauthorized from fetch_file typically means the file isn't ready yet
        on Seedr's servers, so we should NOT retry immediately - the file needs
        time to process. Return False to fail fast and let the next poll retry.
        """
        error_str = str(error).lower()
        # 401/unauthorized means file not ready - don't retry, fail fast
        if '401' in error_str or 'unauthorized' in error_str or 'invalid json' in error_str:
            return False
        # Retry on transient network errors
        if any(x in error_str for x in ['timeout', 'connection', '503', '502', '504']):
            return True
        return False

    async def _record_error(self, torrent_hash: str, error: str):
        """Record an error for a torrent."""
        async with self._error_lock:
            self._error_counts[torrent_hash] = self._error_counts.get(torrent_hash, 0) + 1
            self._last_errors[torrent_hash] = error

            # Prune error tracking dicts if they grow too large
            if len(self._error_counts) > self._max_error_entries:
                # Keep entries for active torrents, prune the rest
                active_hashes = set(self._torrents_cache.keys())
                inactive = [h for h in self._error_counts if h not in active_hashes]
                # Remove oldest inactive entries (up to half)
                to_remove = inactive[:len(inactive) // 2] if inactive else []
                for h in to_remove:
                    self._error_counts.pop(h, None)
                    self._last_errors.pop(h, None)
                    self._download_retry_after.pop(h, None)
                if to_remove:
                    logger.debug(f"Pruned {len(to_remove)} stale error tracking entries")

        if self._state_manager:
            await self._state_manager.record_error(torrent_hash, error)

    def _update_download_progress(self, torrent_hash: str, progress: float):
        """Update download progress for a torrent."""
        self._download_progress[torrent_hash] = min(progress, 1.0)

    async def _download_file_with_progress(
        self,
        url: str,
        destination: str,
        expected_size: int,
        torrent_hash: str,
        progress_callback: Callable[[int], None]
    ):
        """Download a file from URL with progress tracking and retry."""
        timeout = aiohttp.ClientTimeout(total=3600, connect=30)

        async def do_download():
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        raise Exception(f"HTTP {response.status}: {response.reason}")

                    Path(destination).parent.mkdir(parents=True, exist_ok=True)

                    downloaded = 0
                    last_speed_update = asyncio.get_event_loop().time()
                    last_downloaded = 0

                    async with aiofiles.open(destination, 'wb') as f:
                        async for chunk in response.content.iter_chunked(65536):
                            await f.write(chunk)
                            downloaded += len(chunk)
                            progress_callback(downloaded)

                            now = asyncio.get_event_loop().time()
                            if now - last_speed_update >= 1.0:
                                speed = int((downloaded - last_downloaded) / (now - last_speed_update))
                                self._active_downloads[torrent_hash] = speed
                                last_speed_update = now
                                last_downloaded = downloaded

                    if expected_size > 0 and downloaded != expected_size:
                        logger.warning(
                            f"Size mismatch: expected {expected_size}, got {downloaded}"
                        )
                        # Delete incomplete file and raise error to trigger retry
                        try:
                            os.remove(destination)
                        except OSError:
                            pass
                        raise Exception(
                            f"Size mismatch: expected {expected_size} bytes, got {downloaded}"
                        )

        await self._retry_handler.with_retry(
            operation=do_download,
            operation_id=f"download_file_{torrent_hash}_{hash(url)}",
            max_attempts=self._retry_handler.config.download_max_attempts,
        )

    async def add_torrent(
        self,
        magnet_link: Optional[str] = None,
        torrent_file: Optional[bytes] = None,
        category: str = "",
    ) -> Optional[str]:
        """Add a torrent to Seedr or queue if storage is full."""
        if not self._client:
            await self.initialize()

        # Log incoming request for debugging category issues
        logger.info(
            f"add_torrent called: category='{category}', "
            f"has_magnet={magnet_link is not None}, "
            f"has_file={torrent_file is not None}"
        )

        instance_id = extract_instance_id(category)

        # Check circuit breaker
        if not await self._circuit_breaker.can_execute():
            logger.warning("Circuit breaker open, queuing torrent")
            name = self._extract_name_from_magnet(magnet_link) if magnet_link else "Unknown Torrent"
            return await self._add_to_queue(magnet_link, torrent_file, category, name, 0, instance_id)

        async with self._lock:
            try:
                await self._update_storage_info()

                name = "Unknown Torrent"
                estimated_size = 0

                if magnet_link:
                    parsed = urllib.parse.urlparse(magnet_link)
                    params = urllib.parse.parse_qs(parsed.query)
                    if 'dn' in params:
                        name = urllib.parse.unquote(params['dn'][0])
                    if 'xl' in params:
                        try:
                            estimated_size = int(params['xl'][0])
                        except ValueError:
                            pass

                check_size = estimated_size if estimated_size > 0 else 500 * 1024 * 1024
                available = self.get_available_storage()

                if len(self._torrent_queue) > 0:
                    logger.info(f"Queue not empty, adding to queue: {name}")
                    return await self._add_to_queue(
                        magnet_link, torrent_file, category, name, estimated_size, instance_id
                    )

                if available < check_size:
                    logger.warning(
                        f"Insufficient Seedr storage ({available / 1024 / 1024:.1f}MB available), "
                        f"queuing: {name}"
                    )
                    return await self._add_to_queue(
                        magnet_link, torrent_file, category, name, estimated_size, instance_id
                    )

                try:
                    async def do_add():
                        if magnet_link:
                            logger.debug(f"Adding magnet link: {magnet_link[:80]}...")
                            return await self._client.add_torrent(magnet_link=magnet_link)
                        elif torrent_file:
                            # seedrcc expects a file path, not bytes
                            # Write bytes to a temp file and pass the path
                            tf = torrent_file
                            if isinstance(tf, str):
                                logger.warning("torrent_file is string, encoding to bytes")
                                tf = tf.encode('latin-1')

                            # Create temp file with .torrent extension
                            with tempfile.NamedTemporaryFile(suffix='.torrent', delete=False) as tmp:
                                tmp.write(tf)
                                tmp_path = tmp.name

                            try:
                                logger.debug(f"Adding torrent file ({len(tf)} bytes) via temp file")
                                return await self._client.add_torrent(torrent_file=tmp_path)
                            finally:
                                # Clean up temp file
                                try:
                                    os.unlink(tmp_path)
                                except OSError:
                                    pass
                        else:
                            raise ValueError("Either magnet_link or torrent_file required")

                    result = await self._retry_handler.with_retry(
                        operation=do_add,
                        operation_id=f"add_torrent_{hash(magnet_link or str(torrent_file)[:100] if torrent_file else '')}",
                        max_attempts=self._retry_handler.config.seedr_api_max_attempts,
                    )
                    await self._circuit_breaker.record_success()

                except Exception as e:
                    await self._circuit_breaker.record_failure()
                    error_str = str(e).lower()

                    # Handle seedrcc parsing errors - usually means API returned success but lib can't parse
                    if 'missing' in error_str and 'positional argument' in error_str:
                        logger.warning(f"Seedr API response parsing failed: {e}")
                        # The torrent was likely added successfully - check Seedr for it
                        # First, refresh our knowledge of Seedr contents
                        try:
                            await asyncio.sleep(1)  # Brief delay for Seedr to register
                            contents = await self._client.list_contents()

                            # Search for the torrent by name in transfers (active) or folders (complete)
                            normalized_search = normalize_folder_name(name)
                            found_hash = None

                            for transfer in getattr(contents, 'transfers', []):
                                if normalize_folder_name(transfer.name) == normalized_search:
                                    found_hash = f"SEEDR{transfer.id:016X}".upper()
                                    logger.info(f"Found torrent in Seedr transfers after parse error: {name} -> {found_hash}")
                                    break

                            if not found_hash:
                                for folder in getattr(contents, 'folders', []):
                                    if normalize_folder_name(folder.name) == normalized_search:
                                        found_hash = f"SEEDR{folder.id:016X}".upper()
                                        logger.info(f"Found torrent in Seedr folders after parse error: {name} -> {found_hash}")
                                        break

                            if found_hash:
                                # Torrent exists! Save mappings and return success
                                if name and name != "Unknown Torrent":
                                    self._name_to_hash_mapping[normalized_search] = found_hash
                                if category:
                                    self._category_mapping[found_hash] = category
                                    self._instance_mapping[found_hash] = instance_id
                                    cat_path = safe_join_path(self.download_path, category)
                                    await asyncio.to_thread(os.makedirs, cat_path, exist_ok=True)
                                    try:
                                        await asyncio.to_thread(os.chmod, cat_path, 0o777)
                                    except OSError:
                                        pass
                                logger.info(f"Successfully added torrent (recovered from parse error): {name} -> {found_hash}")
                                return found_hash
                            else:
                                # Not found - might be processing, queue for later check
                                logger.warning(f"Torrent not yet visible in Seedr, queuing for retry: {name}")
                                return await self._add_to_queue(
                                    magnet_link, torrent_file, category, name, estimated_size, instance_id
                                )
                        except Exception as check_err:
                            logger.warning(f"Failed to verify torrent in Seedr: {check_err}, queuing")
                            return await self._add_to_queue(
                                magnet_link, torrent_file, category, name, estimated_size, instance_id
                            )

                    if 'space' in error_str or 'storage' in error_str or 'limit' in error_str or 'full' in error_str:
                        logger.warning(f"Seedr storage full, queuing: {name}")
                        return await self._add_to_queue(
                            magnet_link, torrent_file, category, name, estimated_size, instance_id
                        )
                    raise

                torrent_hash = None
                if hasattr(result, 'hash') and result.hash:
                    torrent_hash = result.hash.upper()
                elif hasattr(result, 'id'):
                    torrent_hash = f"SEEDR{result.id:016X}".upper()
                else:
                    torrent_hash = f"SEEDR{id(result):016X}".upper()

                name = getattr(result, 'name', name)
                normalized_name = normalize_folder_name(name) if name and name != "Unknown Torrent" else ""

                # Always track name → hash for folder transitions (even without category)
                # Use normalized name for consistent lookups (Seedr may change + to spaces, etc.)
                if normalized_name:
                    self._name_to_hash_mapping[normalized_name] = torrent_hash

                if category:
                    self._category_mapping[torrent_hash] = category
                    self._instance_mapping[torrent_hash] = instance_id
                    cat_path = safe_join_path(self.download_path, category)
                    await asyncio.to_thread(os.makedirs, cat_path, exist_ok=True)
                    try:
                        await asyncio.to_thread(os.chmod, cat_path, 0o777)
                    except OSError:
                        pass

                with LogContext(torrent_hash=torrent_hash, torrent_name=name, category=category):
                    logger.info(f"Added torrent: {name} (hash: {torrent_hash}, instance: {instance_id})")

                # Log activity and persist torrent early (for category recovery on hash transitions)
                if self._state_manager:
                    await self._state_manager.log_activity(
                        action="torrent_added",
                        torrent_hash=torrent_hash,
                        torrent_name=name,
                        category=category,
                        details=f"Added to Seedr (instance: {instance_id})",
                    )

                    # Persist name-to-hash mapping for robust category recovery across restarts
                    # Extract magnet info hash as stable identifier (survives Seedr hash changes)
                    magnet_hash = self._extract_info_hash_from_magnet(magnet_link) if magnet_link else None
                    if normalized_name:
                        await self._state_manager.add_name_hash_mapping(
                            normalized_name=normalized_name,
                            original_name=name,
                            torrent_hash=torrent_hash,
                            category=category,
                            instance_id=instance_id,
                            magnet_hash=magnet_hash,
                        )
                        logger.debug(f"Persisted name hash mapping: {normalized_name} -> {torrent_hash} (magnet: {magnet_hash})")

                    # Persist the torrent early so we can recover category via fuzzy name match
                    # if Seedr changes the folder name during processing
                    if category:
                        early_torrent = TorrentState(
                            hash=torrent_hash,
                            seedr_id=str(getattr(result, 'id', '')),
                            name=name,
                            size=0,  # Size not known yet
                            category=category,
                            instance_id=instance_id,
                            phase=TorrentPhase.FETCHING_METADATA,
                            seedr_progress=0.0,
                            local_progress=0.0,
                            added_on=int(datetime.now().timestamp()),
                            save_path=cat_path,
                        )
                        await self._state_manager.add_torrent(early_torrent)
                        logger.debug(f"Persisted torrent early for category recovery: {name} -> {category}")

                return torrent_hash

            except Exception as e:
                logger.error(f"Error adding torrent: {e}")
                raise

    def _extract_name_from_magnet(self, magnet_link: str) -> str:
        """Extract torrent name from magnet link."""
        try:
            parsed = urllib.parse.urlparse(magnet_link)
            params = urllib.parse.parse_qs(parsed.query)
            if 'dn' in params:
                return urllib.parse.unquote(params['dn'][0])
        except Exception:
            pass
        return "Unknown Torrent"

    def _extract_info_hash_from_magnet(self, magnet_link: str) -> Optional[str]:
        """
        Extract btih (BitTorrent info hash) from magnet link as a stable identifier.
        This hash remains constant even when Seedr changes the torrent hash on completion.
        """
        try:
            parsed = urllib.parse.urlparse(magnet_link)
            params = urllib.parse.parse_qs(parsed.query)
            if 'xt' in params:
                for xt in params['xt']:
                    # Format: urn:btih:HASH
                    if xt.lower().startswith('urn:btih:'):
                        info_hash = xt[9:].upper()
                        # Handle base32 encoded hashes (40 hex chars or 32 base32 chars)
                        if len(info_hash) == 32:
                            # Base32 encoded, convert to hex
                            import base64
                            try:
                                decoded = base64.b32decode(info_hash)
                                info_hash = decoded.hex().upper()
                            except Exception:
                                pass
                        return info_hash
        except Exception:
            pass
        return None

    async def set_category_mapping(self, torrent_hash: str, category: str, instance_id: str = ""):
        """Thread-safe method to set category and instance mapping for a torrent."""
        async with self._mapping_lock:
            # Normalize hash to uppercase for consistent lookups
            normalized_hash = torrent_hash.upper()
            self._category_mapping[normalized_hash] = category
            self._instance_mapping[normalized_hash] = instance_id or extract_instance_id(category)

    async def get_category_mapping(self, torrent_hash: str) -> tuple[str, str]:
        """Thread-safe method to get category and instance mapping for a torrent."""
        async with self._mapping_lock:
            normalized_hash = torrent_hash.upper()
            category = self._category_mapping.get(normalized_hash, "")
            instance_id = self._instance_mapping.get(normalized_hash, "")
            return category, instance_id

    async def remove_mapping(self, torrent_hash: str):
        """Thread-safe method to remove category and instance mapping for a torrent."""
        async with self._mapping_lock:
            normalized_hash = torrent_hash.upper()
            self._category_mapping.pop(normalized_hash, None)
            self._instance_mapping.pop(normalized_hash, None)

    async def _add_to_queue(
        self,
        magnet_link: Optional[str],
        torrent_file: Optional[bytes],
        category: str,
        name: str,
        estimated_size: int,
        instance_id: str = "",
    ) -> str:
        """Add a torrent to the queue."""
        async with self._queue_lock:
            self._queue_counter += 1
            queue_id = f"{self._queue_counter:08d}"

            if not instance_id:
                instance_id = extract_instance_id(category)

            queued = QueuedTorrent(
                id=queue_id,
                magnet_link=magnet_link,
                torrent_file=torrent_file,
                category=category,
                instance_id=instance_id,
                name=name,
                estimated_size=estimated_size,
            )

            self._torrent_queue.append(queued)

            queue_hash = f"QUEUE{queue_id}".upper()
            if category:
                self._category_mapping[queue_hash] = category
                self._instance_mapping[queue_hash] = instance_id

            logger.info(
                f"Queued torrent #{queue_id}: {name} "
                f"(queue size: {len(self._torrent_queue)}, instance: {instance_id})"
            )

            # Persist to state manager
            if self._state_manager:
                from .state import QueuedTorrentState
                await self._state_manager.add_to_queue(QueuedTorrentState(
                    id=queue_id,
                    magnet_link=magnet_link,
                    torrent_file=torrent_file,
                    category=category,
                    instance_id=instance_id,
                    name=name,
                    estimated_size=estimated_size,
                    added_time=queued.added_time,
                ))

            return queue_hash

    async def _process_queue(self):
        """Process queued torrents if storage is available."""
        if self._queue_processing:
            return

        self._queue_processing = True
        try:
            async with self._queue_lock:
                while self._torrent_queue:
                    await self._update_storage_info()
                    available = self.get_available_storage()

                    queued = self._torrent_queue[0]
                    check_size = queued.estimated_size if queued.estimated_size > 0 else 500 * 1024 * 1024

                    if available < check_size:
                        logger.debug(f"Not enough storage for queue item: {queued.name}")
                        break

                    self._torrent_queue.popleft()
                    queue_hash = f"QUEUE{queued.id}".upper()

                    # Remove from state manager
                    if self._state_manager:
                        await self._state_manager.remove_from_queue(queued.id)

                    try:
                        logger.info(f"Processing queued torrent: {queued.name}")

                        if queued.magnet_link:
                            result = await self._client.add_torrent(magnet_link=queued.magnet_link)
                        elif queued.torrent_file:
                            # seedrcc expects a file path, not bytes
                            tf = queued.torrent_file
                            if isinstance(tf, str):
                                tf = tf.encode('latin-1')

                            # Write to temp file and pass path
                            with tempfile.NamedTemporaryFile(suffix='.torrent', delete=False) as tmp:
                                tmp.write(tf)
                                tmp_path = tmp.name

                            try:
                                result = await self._client.add_torrent(torrent_file=tmp_path)
                            finally:
                                try:
                                    os.unlink(tmp_path)
                                except OSError:
                                    pass
                        else:
                            logger.error(f"Queued torrent has no magnet or file: {queued.id}")
                            continue

                        real_hash = None
                        if hasattr(result, 'hash') and result.hash:
                            real_hash = result.hash.upper()
                        elif hasattr(result, 'id'):
                            real_hash = f"SEEDR{result.id:016X}".upper()

                        if real_hash:
                            # Atomic update of hash mapping and category mappings
                            async with self._mapping_lock:
                                self._hash_mapping[queue_hash] = real_hash
                                if queued.category:
                                    self._category_mapping[real_hash] = queued.category
                                    self._instance_mapping[real_hash] = queued.instance_id

                            logger.info(f"Hash mapping: {queue_hash} -> {real_hash}")

                            # Persist hash mapping
                            if self._state_manager:
                                try:
                                    await self._state_manager.add_hash_mapping(queue_hash, real_hash)
                                except Exception as persist_err:
                                    logger.warning(f"Failed to persist hash mapping: {persist_err}")

                        if queued.category:
                            cat_path = safe_join_path(self.download_path, queued.category)
                            await asyncio.to_thread(os.makedirs, cat_path, exist_ok=True)
                            try:
                                await asyncio.to_thread(os.chmod, cat_path, 0o777)
                            except OSError:
                                pass

                        self._torrents_cache.pop(queue_hash, None)

                        logger.info(f"Queued torrent added to Seedr: {queued.name} -> {real_hash}")

                    except Exception as e:
                        error_str = str(e).lower()
                        queued.retry_count += 1
                        queued.last_error = str(e)

                        if 'space' in error_str or 'storage' in error_str or 'limit' in error_str:
                            self._torrent_queue.appendleft(queued)
                            logger.warning(f"Still not enough storage, re-queued: {queued.name}")
                            break
                        elif 'duplicate' in error_str or 'already' in error_str or 'exists' in error_str:
                            # Torrent already exists in Seedr - this is fine, just skip
                            logger.info(f"Torrent already in Seedr (duplicate): {queued.name}")
                            # Don't re-queue, treat as success
                        elif 'missing' in error_str and 'positional argument' in error_str:
                            # seedrcc parsing error - API returned success but lib can't parse
                            # Check if the torrent was actually added to Seedr
                            logger.warning(f"Seedr API response parsing failed for {queued.name}, checking if added...")
                            try:
                                await asyncio.sleep(1)
                                contents = await self._client.list_contents()
                                normalized_search = normalize_folder_name(queued.name)
                                found = False

                                for transfer in getattr(contents, 'transfers', []):
                                    if normalize_folder_name(transfer.name) == normalized_search:
                                        found_hash = f"SEEDR{transfer.id:016X}".upper()
                                        logger.info(f"Queue item found in Seedr transfers: {queued.name} -> {found_hash}")
                                        # Save mappings
                                        self._name_to_hash_mapping[normalized_search] = found_hash
                                        if queued.category:
                                            self._category_mapping[found_hash] = queued.category
                                            self._instance_mapping[found_hash] = queued.instance_id
                                        found = True
                                        break

                                if not found:
                                    for folder in getattr(contents, 'folders', []):
                                        if normalize_folder_name(folder.name) == normalized_search:
                                            found_hash = f"SEEDR{folder.id:016X}".upper()
                                            logger.info(f"Queue item found in Seedr folders: {queued.name} -> {found_hash}")
                                            self._name_to_hash_mapping[normalized_search] = found_hash
                                            if queued.category:
                                                self._category_mapping[found_hash] = queued.category
                                                self._instance_mapping[found_hash] = queued.instance_id
                                            found = True
                                            break

                                if not found and queued.retry_count < 2:
                                    # Not found yet, might still be processing
                                    logger.warning(f"Queue item not yet visible in Seedr, will retry: {queued.name}")
                                    self._torrent_queue.append(queued)
                                elif found:
                                    logger.info(f"Queue item successfully added to Seedr (recovered): {queued.name}")
                            except Exception as check_err:
                                logger.warning(f"Failed to verify queue item in Seedr: {check_err}")
                                if queued.retry_count < 2:
                                    self._torrent_queue.append(queued)
                        else:
                            logger.error(f"Failed to add queued torrent: {e}")
                            # Re-queue if under max retries
                            if queued.retry_count < self._retry_handler.config.queue_process_max_attempts:
                                self._torrent_queue.append(queued)
                                logger.info(f"Re-queued for retry ({queued.retry_count}): {queued.name}")

                    await asyncio.sleep(1)

        finally:
            self._queue_processing = False

    async def delete_torrent(
        self, torrent_hash: str, delete_files: bool = False
    ) -> bool:
        """Delete a torrent from Seedr or queue."""
        if not self._client:
            await self.initialize()

        async with self._lock:
            try:
                torrent_hash = torrent_hash.upper()

                # Check if it's a queued torrent
                if torrent_hash.startswith("QUEUE"):
                    queue_id = torrent_hash.replace("QUEUE", "")
                    async with self._queue_lock:
                        for queued in list(self._torrent_queue):
                            if queued.id == queue_id:
                                self._torrent_queue.remove(queued)
                                self._category_mapping.pop(torrent_hash, None)
                                self._instance_mapping.pop(torrent_hash, None)
                                self._torrents_cache.pop(torrent_hash, None)

                                if self._state_manager:
                                    await self._state_manager.remove_from_queue(queue_id)

                                logger.info(f"Removed from queue: {queued.name}")
                                return True

                    if torrent_hash in self._hash_mapping:
                        real_hash = self._hash_mapping[torrent_hash]
                        torrent_hash_to_delete = real_hash
                    else:
                        return False
                else:
                    torrent_hash_to_delete = torrent_hash

                real_hash = self._hash_mapping.get(torrent_hash, torrent_hash)
                torrent_hash_to_delete = real_hash

                if torrent_hash_to_delete in self._download_tasks:
                    self._download_tasks[torrent_hash_to_delete].cancel()
                    del self._download_tasks[torrent_hash_to_delete]

                torrent = self._torrents_cache.get(torrent_hash_to_delete)
                if not torrent:
                    logger.warning(f"Torrent not found in cache: {torrent_hash_to_delete}")
                    return False

                # Always try to delete from Seedr - if already deleted, error is handled gracefully
                try:
                    if torrent.state in [TorrentState.COMPLETED, TorrentState.DOWNLOADING_LOCAL, TorrentState.COMPLETED_LOCAL]:
                        await self._client.delete_folder(torrent.id)
                    else:
                        await self._client.delete_torrent(torrent.id)

                    await self._update_storage_info()
                    await self._process_queue()
                    logger.info(f"Deleted from Seedr: {torrent.name}")

                except Exception as e:
                    logger.debug(f"Could not delete from Seedr (may already be deleted): {e}")

                if delete_files and torrent.content_path:
                    try:
                        import shutil
                        if os.path.exists(torrent.content_path):
                            shutil.rmtree(torrent.content_path)
                            logger.info(f"Deleted local files: {torrent.content_path}")
                    except Exception as e:
                        logger.warning(f"Could not delete local files: {e}")

                # Clean up state
                self._torrents_cache.pop(torrent_hash_to_delete, None)
                self._torrents_cache.pop(torrent_hash, None)
                self._category_mapping.pop(torrent_hash_to_delete, None)
                self._category_mapping.pop(torrent_hash, None)
                self._instance_mapping.pop(torrent_hash_to_delete, None)
                self._instance_mapping.pop(torrent_hash, None)
                self._download_progress.pop(torrent_hash_to_delete, None)
                self._local_downloads.discard(torrent_hash_to_delete)
                self._error_counts.pop(torrent_hash_to_delete, None)
                self._last_errors.pop(torrent_hash_to_delete, None)
                async with self._qnap_lock:
                    self._qnap_pending_completion.pop(torrent_hash_to_delete, None)
                self._download_retry_after.pop(torrent_hash_to_delete, None)

                # Clean up name-to-hash mapping to prevent memory leak
                if torrent.name:
                    normalized_name = normalize_folder_name(torrent.name)
                    self._name_to_hash_mapping.pop(normalized_name, None)

                if torrent_hash in self._hash_mapping:
                    del self._hash_mapping[torrent_hash]

                keys_to_remove = [k for k, v in self._hash_mapping.items() if v == torrent_hash_to_delete]
                for k in keys_to_remove:
                    del self._hash_mapping[k]
                    self._torrents_cache.pop(k, None)

                # Update state manager
                if self._state_manager:
                    await self._state_manager.delete_torrent(torrent_hash_to_delete)
                    await self._state_manager.delete_qnap_pending(torrent_hash_to_delete)

                logger.info(f"Deleted torrent: {torrent.name}")
                return True

            except Exception as e:
                logger.error(f"Error deleting torrent: {e}")
                return False

    async def get_torrent(self, torrent_hash: str) -> Optional[SeedrTorrent]:
        """Get a specific torrent by hash."""
        await self.get_torrents()

        real_hash = self._hash_mapping.get(torrent_hash.upper(), torrent_hash.upper())
        torrent = self._torrents_cache.get(real_hash)

        if torrent and real_hash != torrent_hash.upper():
            from dataclasses import replace
            torrent = replace(torrent, hash=torrent_hash.upper())

        return torrent

    async def get_torrent_files(self, torrent_hash: str) -> list[dict]:
        """Get files for a specific torrent."""
        if not self._client:
            await self.initialize()

        real_hash = self._hash_mapping.get(torrent_hash.upper(), torrent_hash.upper())
        torrent = self._torrents_cache.get(real_hash)

        if not torrent:
            return []

        if torrent.is_queued:
            return []

        if torrent.state not in [TorrentState.COMPLETED, TorrentState.DOWNLOADING_LOCAL, TorrentState.COMPLETED_LOCAL]:
            return []

        try:
            folder = await self._client.list_contents(str(torrent.id))
            files = []

            for idx, file in enumerate(folder.files):
                local_path = os.path.join(torrent.content_path, file.name) if torrent.content_path else ""
                local_exists = os.path.exists(local_path) if local_path else False

                files.append({
                    "index": idx,
                    "name": file.name,
                    "size": file.size,
                    "progress": 1.0 if local_exists else torrent.local_progress,
                    "priority": 1,
                    "is_seed": False,
                    "piece_range": [0, 0],
                    "availability": 1.0,
                })

            return files
        except Exception as e:
            logger.error(f"Error getting torrent files: {e}")
            return []

    async def download_file(self, file_id: str, destination: str) -> bool:
        """Download a file from Seedr to local filesystem."""
        if not self._client:
            await self.initialize()

        try:
            file_info = await self._client.fetch_file(file_id)
            download_url = file_info.url

            async with aiohttp.ClientSession() as session:
                async with session.get(download_url) as response:
                    if response.status == 200:
                        Path(destination).parent.mkdir(parents=True, exist_ok=True)
                        async with aiofiles.open(destination, 'wb') as f:
                            async for chunk in response.content.iter_chunked(8192):
                                await f.write(chunk)
                        return True
            return False
        except Exception as e:
            logger.error(f"Error downloading file: {e}")
            return False

    async def get_settings(self) -> dict:
        """Get account settings."""
        if not self._client:
            await self.initialize()

        try:
            settings = await self._client.get_settings()
            await self._update_storage_info()

            return {
                "username": settings.account.username,
                "email": settings.account.email,
                "space_used": self._storage_used,
                "space_max": self._storage_max,
                "space_available": self.get_available_storage(),
                "queue_size": len(self._torrent_queue),
            }
        except Exception as e:
            logger.error(f"Error getting settings: {e}")
            return {}

    async def test_connection(self) -> tuple[bool, str]:
        """Test the connection to Seedr."""
        try:
            if not self._client:
                await self.initialize()

            settings = await self._client.get_settings()
            await self._update_storage_info()

            storage_pct = (self._storage_used / self._storage_max * 100) if self._storage_max > 0 else 0
            queue_info = f", {len(self._torrent_queue)} queued" if self._torrent_queue else ""
            circuit_info = f", circuit: {self._circuit_breaker.state.value}"

            return True, f"Connected as {settings.account.username} ({storage_pct:.1f}% storage used{queue_info}{circuit_info})"
        except Exception as e:
            return False, str(e)

    async def force_download(self, torrent_hash: str) -> bool:
        """Force start downloading a torrent to local storage."""
        torrent_hash = torrent_hash.upper()

        real_hash = self._hash_mapping.get(torrent_hash, torrent_hash)

        torrent = self._torrents_cache.get(real_hash)
        if not torrent:
            return False

        if torrent.is_queued:
            return False

        if real_hash in self._download_tasks:
            return True

        if real_hash in self._local_downloads:
            return True

        self._start_download_task(
            real_hash,
            int(torrent.id),
            torrent.name,
            torrent.save_path
        )
        return True

    def get_queue_info(self) -> list[dict]:
        """Get information about queued torrents."""
        return [
            {
                "id": q.id,
                "name": q.name,
                "category": q.category,
                "instance_id": q.instance_id,
                "estimated_size": q.estimated_size,
                "added_time": q.added_time,
                "position": i + 1,
                "retry_count": q.retry_count,
                "last_error": q.last_error,
            }
            for i, q in enumerate(self._torrent_queue)
        ]

    async def clear_queue(self) -> int:
        """Clear all queued torrents. Returns count of removed items."""
        async with self._queue_lock:
            count = len(self._torrent_queue)

            for queued in self._torrent_queue:
                queue_hash = f"QUEUE{queued.id}".upper()
                self._category_mapping.pop(queue_hash, None)
                self._instance_mapping.pop(queue_hash, None)
                self._torrents_cache.pop(queue_hash, None)

            self._torrent_queue.clear()

            if self._state_manager:
                await self._state_manager.clear_queue()

            logger.info(f"Cleared {count} items from queue")
            return count

    def get_error_info(self, torrent_hash: str) -> Optional[dict]:
        """Get error information for a torrent."""
        torrent_hash = torrent_hash.upper()
        real_hash = self._hash_mapping.get(torrent_hash, torrent_hash)

        if real_hash not in self._error_counts and real_hash not in self._last_errors:
            return None

        return {
            "hash": real_hash,
            "error_count": self._error_counts.get(real_hash, 0),
            "last_error": self._last_errors.get(real_hash),
        }

    def get_stats(self) -> dict:
        """Get client statistics."""
        now = datetime.now().timestamp()
        cooldowns = {
            h: int(t - now) for h, t in self._download_retry_after.items() if t > now
        }
        cache_age = now - self._last_successful_api_call if self._last_successful_api_call else None
        return {
            "torrents_cached": len(self._torrents_cache),
            "queue_size": len(self._torrent_queue),
            "active_downloads": len(self._download_tasks),
            "active_download_hashes": list(self._download_tasks.keys()),
            "local_downloads": len(self._local_downloads),
            "hash_mappings": len(self._hash_mapping),
            "storage_used_mb": self._storage_used / 1024 / 1024,
            "storage_max_mb": self._storage_max / 1024 / 1024,
            "storage_available_mb": self.get_available_storage() / 1024 / 1024,
            "cache_age_seconds": cache_age,
            "cache_stale": cache_age > self._cache_max_age if cache_age else False,
            "circuit_breaker": self._circuit_breaker.get_stats(),
            "rate_limiter": self._rate_limiter.get_stats(),
            "retry": self._retry_handler.get_stats(),
            "errors": {
                "total_torrents_with_errors": len(self._error_counts),
                "total_error_count": sum(self._error_counts.values()),
                "error_hashes": list(self._error_counts.keys()),
            },
            "cooldowns": cooldowns,
            "download_progress": dict(self._download_progress),
        }
