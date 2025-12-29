"""
Seedr Client Wrapper
Provides a clean interface to the Seedr API using seedrcc library.
Includes automatic downloading, storage management, queue system,
retry logic, state persistence, and multi-instance support.
"""

import asyncio
import logging
import os
import urllib.parse
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional, Callable, TYPE_CHECKING

import aiofiles
import aiohttp

from .logging_config import LogContext
from .retry import RetryHandler, CircuitBreaker, RetryConfig, CircuitBreakerConfig, RateLimiter
from .state import TorrentPhase, extract_instance_id

if TYPE_CHECKING:
    from .state import StateManager

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
    ):
        self.email = email
        self.password = password
        self._token = token
        self.download_path = download_path
        self.auto_download = auto_download
        self.delete_after_download = delete_after_download
        self.storage_buffer_mb = storage_buffer_mb
        self.storage_buffer_bytes = storage_buffer_mb * 1024 * 1024

        self._client = None
        self._lock = asyncio.Lock()
        self._download_lock = asyncio.Lock()
        self._queue_lock = asyncio.Lock()
        self._mapping_lock = asyncio.Lock()  # Lock for category/instance mappings

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

        # Queue system for storage management
        self._torrent_queue: deque[QueuedTorrent] = deque()
        self._queue_counter: int = 0
        self._queue_processing: bool = False

        # Hash mapping: queue_hash -> real_hash (for Sonarr tracking)
        self._hash_mapping: dict[str, str] = {}

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
            # Restore local downloads
            for torrent in await self._state_manager.get_torrents():
                if torrent.phase == TorrentPhase.COMPLETED:
                    self._local_downloads.add(torrent.hash)
                if torrent.category:
                    self._category_mapping[torrent.hash] = torrent.category
                    self._instance_mapping[torrent.hash] = torrent.instance_id

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

            logger.info(
                f"Restored state: {len(self._local_downloads)} local downloads, "
                f"{len(self._torrent_queue)} queued torrents"
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
            return os.path.join(self.download_path, category)
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

                # Get root folder contents with retry and rate limiting
                await self._rate_limiter.acquire()
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

                    effective_progress = seedr_progress * 0.5

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
                        download_speed=getattr(transfer, 'download_rate', 0) or 0,
                        upload_speed=getattr(transfer, 'upload_rate', 0) or 0,
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

                    category = self._category_mapping.get(torrent_hash, "")
                    instance_id = self._instance_mapping.get(torrent_hash, extract_instance_id(category))
                    save_path = self._get_save_path(category)
                    content_path = os.path.join(save_path, folder.name)

                    is_local = torrent_hash in self._local_downloads
                    local_progress = self._download_progress.get(torrent_hash, 0.0)
                    is_downloading = torrent_hash in self._download_tasks

                    if is_local:
                        state = TorrentState.COMPLETED_LOCAL
                        local_progress = 1.0
                    elif is_downloading:
                        state = TorrentState.DOWNLOADING_LOCAL
                    else:
                        state = TorrentState.COMPLETED

                    if is_local:
                        effective_progress = 1.0
                    elif is_downloading:
                        effective_progress = 0.5 + (local_progress * 0.5)
                    else:
                        effective_progress = 0.5

                    download_speed = 0
                    if torrent_hash in self._active_downloads:
                        download_speed = self._active_downloads.get(torrent_hash, 0)

                    phase = self._determine_phase(
                        is_queued=False,
                        seedr_progress=1.0,
                        is_downloading_local=is_downloading,
                        is_local=is_local,
                        has_error=torrent_hash in self._last_errors,
                    )

                    torrent = SeedrTorrent(
                        id=str(folder.id),
                        hash=torrent_hash,
                        name=folder.name,
                        size=folder.size,
                        progress=effective_progress if self.auto_download else 1.0,
                        state=state,
                        download_speed=download_speed,
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
                    if self.auto_download and not is_local and torrent_hash not in self._download_tasks:
                        self._start_download_task(torrent_hash, folder.id, folder.name, save_path)

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

    async def _download_folder(self, torrent_hash: str, folder_id: str, folder_name: str, save_path: str):
        """Download all files from a Seedr folder to local storage."""
        async with self._download_lock:
            try:
                with LogContext(torrent_hash=torrent_hash, torrent_name=folder_name):
                    logger.info(f"Starting download of {folder_name} to {save_path}")

                    dest_dir = os.path.join(save_path, folder_name)
                    os.makedirs(dest_dir, exist_ok=True)

                    # Get folder contents with retry and rate limiting
                    await self._rate_limiter.acquire()
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

                            await self._rate_limiter.acquire()
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
                            self._record_error(torrent_hash, str(e))
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
                self._record_error(torrent_hash, str(e))

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

    def _record_error(self, torrent_hash: str, error: str):
        """Record an error for a torrent."""
        self._error_counts[torrent_hash] = self._error_counts.get(torrent_hash, 0) + 1
        self._last_errors[torrent_hash] = error

        if self._state_manager:
            asyncio.create_task(self._state_manager.record_error(torrent_hash, error))

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
                            # Ensure torrent_file is bytes
                            tf = torrent_file
                            if isinstance(tf, str):
                                logger.warning("torrent_file is string, encoding to bytes")
                                tf = tf.encode('latin-1')
                            logger.debug(f"Adding torrent file ({len(tf)} bytes)")
                            return await self._client.add_torrent(torrent_file=tf)
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

                    # Handle seedrcc parsing errors - usually means API returned an error response
                    if 'missing' in error_str and 'positional argument' in error_str:
                        logger.error(f"Seedr API returned unexpected response (likely error or duplicate): {e}")
                        # This often means the torrent already exists or was rejected
                        # Queue it for retry later
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
                    torrent_hash = f"SEEDR{result.id}"
                else:
                    torrent_hash = f"SEEDR{id(result)}"

                name = getattr(result, 'name', name)

                if category:
                    self._category_mapping[torrent_hash] = category
                    self._instance_mapping[torrent_hash] = instance_id
                    cat_path = os.path.join(self.download_path, category)
                    os.makedirs(cat_path, exist_ok=True)

                with LogContext(torrent_hash=torrent_hash, torrent_name=name, category=category):
                    logger.info(f"Added torrent: {name} (hash: {torrent_hash}, instance: {instance_id})")

                # Log activity
                if self._state_manager:
                    await self._state_manager.log_activity(
                        action="torrent_added",
                        torrent_hash=torrent_hash,
                        torrent_name=name,
                        category=category,
                        details=f"Added to Seedr (instance: {instance_id})",
                    )

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

    async def set_category_mapping(self, torrent_hash: str, category: str, instance_id: str = ""):
        """Thread-safe method to set category and instance mapping for a torrent."""
        async with self._mapping_lock:
            self._category_mapping[torrent_hash] = category
            self._instance_mapping[torrent_hash] = instance_id or extract_instance_id(category)

    async def get_category_mapping(self, torrent_hash: str) -> tuple[str, str]:
        """Thread-safe method to get category and instance mapping for a torrent."""
        async with self._mapping_lock:
            category = self._category_mapping.get(torrent_hash, "")
            instance_id = self._instance_mapping.get(torrent_hash, "")
            return category, instance_id

    async def remove_mapping(self, torrent_hash: str):
        """Thread-safe method to remove category and instance mapping for a torrent."""
        async with self._mapping_lock:
            self._category_mapping.pop(torrent_hash, None)
            self._instance_mapping.pop(torrent_hash, None)

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
                            # Ensure torrent_file is bytes
                            tf = queued.torrent_file
                            if isinstance(tf, str):
                                tf = tf.encode('latin-1')
                            result = await self._client.add_torrent(torrent_file=tf)
                        else:
                            logger.error(f"Queued torrent has no magnet or file: {queued.id}")
                            continue

                        real_hash = None
                        if hasattr(result, 'hash') and result.hash:
                            real_hash = result.hash.upper()
                        elif hasattr(result, 'id'):
                            real_hash = f"SEEDR{result.id}"

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
                            cat_path = os.path.join(self.download_path, queued.category)
                            os.makedirs(cat_path, exist_ok=True)

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
                        elif 'missing' in error_str and 'positional argument' in error_str:
                            # seedrcc parsing error - API returned unexpected response
                            logger.warning(f"Seedr API returned unexpected response for {queued.name}, will retry later")
                            if queued.retry_count < self._retry_handler.config.queue_process_max_attempts:
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

                if torrent_hash_to_delete not in self._local_downloads or not self.delete_after_download:
                    try:
                        if torrent.state in [TorrentState.COMPLETED, TorrentState.DOWNLOADING_LOCAL, TorrentState.COMPLETED_LOCAL]:
                            await self._client.delete_folder(torrent.id)
                        else:
                            await self._client.delete_torrent(torrent.id)

                        await self._update_storage_info()
                        await self._process_queue()

                    except Exception as e:
                        logger.warning(f"Could not delete from Seedr (may already be deleted): {e}")

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

                if torrent_hash in self._hash_mapping:
                    del self._hash_mapping[torrent_hash]

                keys_to_remove = [k for k, v in self._hash_mapping.items() if v == torrent_hash_to_delete]
                for k in keys_to_remove:
                    del self._hash_mapping[k]
                    self._torrents_cache.pop(k, None)

                # Update state manager
                if self._state_manager:
                    await self._state_manager.delete_torrent(torrent_hash_to_delete)

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
