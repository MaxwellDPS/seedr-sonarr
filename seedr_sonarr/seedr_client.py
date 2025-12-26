"""
Seedr Client Wrapper
Provides a clean interface to the Seedr API using seedrcc library.
Includes automatic downloading, storage management, and queue system.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional, Callable
from collections import deque
import os
import aiohttp
import aiofiles
import urllib.parse

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
    added_time: float = 0.0
    name: str = "Queued Torrent"
    estimated_size: int = 0  # Estimated size in bytes (if known)
    
    def __post_init__(self):
        if not self.added_time:
            self.added_time = datetime.now().timestamp()


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


class SeedrClientWrapper:
    """
    Wrapper around seedrcc that provides simplified interface for the proxy.
    Handles authentication, token refresh, API calls, storage management, and queuing.
    """

    def __init__(
        self,
        email: Optional[str] = None,
        password: Optional[str] = None,
        token: Optional[str] = None,
        download_path: str = "/downloads",
        auto_download: bool = True,
        delete_after_download: bool = True,
        storage_buffer_mb: int = 100,  # Keep 100MB buffer in Seedr
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
        
        # Caches and state
        self._torrents_cache: dict[str, SeedrTorrent] = {}
        self._category_mapping: dict[str, str] = {}  # torrent_hash -> category
        self._download_tasks: dict[str, asyncio.Task] = {}  # torrent_hash -> download task
        self._download_progress: dict[str, float] = {}  # torrent_hash -> local download progress
        self._local_downloads: set[str] = set()  # Set of torrent hashes fully downloaded locally
        self._active_downloads: dict[str, int] = {}  # torrent_hash -> bytes downloaded per second
        
        # Queue system for storage management
        self._torrent_queue: deque[QueuedTorrent] = deque()
        self._queue_counter: int = 0
        self._queue_processing: bool = False
        
        # Storage info cache
        self._storage_used: int = 0
        self._storage_max: int = 0
        self._last_storage_check: float = 0

    async def initialize(self):
        """Initialize the Seedr client."""
        try:
            from seedrcc import AsyncSeedr, Token

            if self._token:
                # Try to load from existing token
                try:
                    token_obj = Token.from_json(self._token)
                    self._client = AsyncSeedr(token=token_obj)
                except Exception:
                    # Token might be just the access token string
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
            
            logger.info(f"Seedr client initialized. Storage: {self._storage_used / 1024 / 1024:.1f}MB / {self._storage_max / 1024 / 1024:.1f}MB")
            return True
        except ImportError:
            logger.error("seedrcc library not installed. Run: pip install seedrcc")
            raise
        except Exception as e:
            logger.error(f"Failed to initialize Seedr client: {e}")
            raise

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
            logger.debug(f"Storage updated: {self._storage_used / 1024 / 1024:.1f}MB / {self._storage_max / 1024 / 1024:.1f}MB")
        except Exception as e:
            logger.warning(f"Failed to update storage info: {e}")

    def get_available_storage(self) -> int:
        """Get available storage in bytes (with buffer)."""
        available = self._storage_max - self._storage_used - self.storage_buffer_bytes
        return max(0, available)

    def _parse_progress(self, progress_str: str) -> float:
        """Parse progress string to float (0.0 to 1.0)."""
        try:
            # Progress might be "50" or "50%" or "50.5"
            progress = float(str(progress_str).replace('%', '').strip())
            return progress / 100.0 if progress > 1 else progress
        except (ValueError, TypeError):
            return 0.0

    def _get_save_path(self, category: str = "") -> str:
        """Get the save path for a category."""
        if category:
            return os.path.join(self.download_path, category)
        return self.download_path

    async def get_torrents(self) -> list[SeedrTorrent]:
        """Get all torrents from Seedr, including queued ones."""
        if not self._client:
            await self.initialize()

        async with self._lock:
            try:
                torrents = []

                # Update storage info periodically (every 60 seconds)
                if datetime.now().timestamp() - self._last_storage_check > 60:
                    await self._update_storage_info()

                # Get root folder contents - includes both active torrents and completed folders
                contents = await self._client.list_contents()

                # Process active torrents (downloading in Seedr)
                for transfer in contents.torrents:
                    torrent_hash = transfer.hash.upper() if transfer.hash else f"SEEDR{transfer.id}"
                    
                    # Parse progress
                    progress = self._parse_progress(transfer.progress)
                    
                    # Determine state based on progress and stopped flag
                    if getattr(transfer, 'stopped', 0):
                        state = TorrentState.PAUSED
                    elif progress >= 1.0:
                        state = TorrentState.COMPLETED
                    elif progress > 0:
                        state = TorrentState.DOWNLOADING
                    else:
                        state = TorrentState.QUEUED

                    size = transfer.size or 0
                    category = self._category_mapping.get(torrent_hash, "")
                    
                    torrent = SeedrTorrent(
                        id=str(transfer.id),
                        hash=torrent_hash,
                        name=transfer.name,
                        size=size,
                        progress=progress,
                        state=state,
                        download_speed=getattr(transfer, 'download_rate', 0) or 0,
                        upload_speed=getattr(transfer, 'upload_rate', 0) or 0,
                        seeders=getattr(transfer, 'seeders', 0) or 0,
                        leechers=getattr(transfer, 'leechers', 0) or 0,
                        added_on=int(datetime.now().timestamp()),
                        save_path=self._get_save_path(category),
                        category=category,
                        downloaded=int(size * progress),
                        completed=int(size * progress),
                    )
                    torrents.append(torrent)
                    self._torrents_cache[torrent_hash] = torrent

                # Process completed folders (finished downloads in Seedr)
                for folder in contents.folders:
                    # Generate a consistent hash from folder ID
                    torrent_hash = f"SEEDR{folder.id:016X}".upper()
                    
                    last_update = folder.last_update
                    timestamp = int(last_update.timestamp()) if last_update else int(datetime.now().timestamp())
                    
                    category = self._category_mapping.get(torrent_hash, "")
                    save_path = self._get_save_path(category)
                    content_path = os.path.join(save_path, folder.name)
                    
                    # Check if already downloaded locally
                    is_local = torrent_hash in self._local_downloads
                    local_progress = self._download_progress.get(torrent_hash, 0.0)
                    
                    # Determine state
                    if is_local:
                        state = TorrentState.COMPLETED_LOCAL
                        local_progress = 1.0
                    elif torrent_hash in self._download_tasks:
                        state = TorrentState.DOWNLOADING_LOCAL
                    else:
                        state = TorrentState.COMPLETED
                    
                    # Calculate effective progress (Seedr progress + local download progress)
                    # Seedr download is 50%, local download is 50%
                    if is_local:
                        effective_progress = 1.0
                    elif torrent_hash in self._download_tasks:
                        effective_progress = 0.5 + (local_progress * 0.5)
                    else:
                        effective_progress = 0.5  # Seedr download complete, local not started
                    
                    # Use local download speed if actively downloading
                    download_speed = 0
                    if torrent_hash in self._active_downloads:
                        download_speed = self._active_downloads.get(torrent_hash, 0)
                    
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
                        downloaded=int(folder.size * effective_progress) if self.auto_download else folder.size,
                        completed=int(folder.size * effective_progress) if self.auto_download else folder.size,
                        local_progress=local_progress,
                        is_local=is_local,
                    )
                    torrents.append(torrent)
                    self._torrents_cache[torrent_hash] = torrent
                    
                    # Start auto-download if enabled and not already downloading/downloaded
                    if (self.auto_download and 
                        not is_local and 
                        torrent_hash not in self._download_tasks):
                        self._start_download_task(torrent_hash, folder.id, folder.name, save_path)

                # Add queued torrents to the list
                for queued in self._torrent_queue:
                    queue_hash = f"QUEUE{queued.id}".upper()
                    category = queued.category
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
                        is_queued=True,
                    )
                    torrents.append(torrent)
                    self._torrents_cache[queue_hash] = torrent

                return torrents

            except Exception as e:
                logger.error(f"Error getting torrents: {e}")
                return []

    def _start_download_task(self, torrent_hash: str, folder_id: int, folder_name: str, save_path: str):
        """Start a background task to download a folder from Seedr."""
        if torrent_hash in self._download_tasks:
            return
        
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
                logger.info(f"Starting download of {folder_name} to {save_path}")
                
                # Create destination directory
                dest_dir = os.path.join(save_path, folder_name)
                os.makedirs(dest_dir, exist_ok=True)
                
                # Get folder contents
                folder = await self._client.list_contents(folder_id)
                
                # Calculate total size
                total_size = sum(f.size for f in folder.files)
                downloaded_size = 0
                
                # Download each file
                for file in folder.files:
                    file_dest = os.path.join(dest_dir, file.name)
                    
                    # Skip if already exists and same size
                    if os.path.exists(file_dest) and os.path.getsize(file_dest) == file.size:
                        downloaded_size += file.size
                        self._download_progress[torrent_hash] = downloaded_size / total_size if total_size > 0 else 1.0
                        continue
                    
                    # Get download URL
                    try:
                        file_info = await self._client.fetch_file(str(file.id))
                        download_url = file_info.url
                        
                        # Download the file with progress tracking
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
                        
                    except Exception as e:
                        logger.error(f"Error downloading file {file.name}: {e}")
                        continue
                
                # Download complete
                self._download_progress[torrent_hash] = 1.0
                self._local_downloads.add(torrent_hash)
                logger.info(f"Download complete: {folder_name}")
                
                # Delete from Seedr after successful download (default behavior)
                if self.delete_after_download:
                    try:
                        await self._client.delete_folder(folder_id)
                        logger.info(f"Deleted from Seedr: {folder_name}")
                        
                        # Update storage info and process queue
                        await self._update_storage_info()
                        await self._process_queue()
                        
                    except Exception as e:
                        logger.warning(f"Failed to delete from Seedr: {e}")
                
            except asyncio.CancelledError:
                logger.info(f"Download cancelled: {folder_name}")
                raise
            except Exception as e:
                logger.error(f"Error downloading folder {folder_name}: {e}")
                self._download_progress[torrent_hash] = 0.0

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
        """Download a file from URL with progress tracking."""
        timeout = aiohttp.ClientTimeout(total=3600, connect=30)  # 1 hour timeout
        
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as response:
                if response.status != 200:
                    raise Exception(f"HTTP {response.status}: {response.reason}")
                
                # Create parent directories
                Path(destination).parent.mkdir(parents=True, exist_ok=True)
                
                # Download with progress
                downloaded = 0
                last_speed_update = asyncio.get_event_loop().time()
                last_downloaded = 0
                
                async with aiofiles.open(destination, 'wb') as f:
                    async for chunk in response.content.iter_chunked(65536):  # 64KB chunks
                        await f.write(chunk)
                        downloaded += len(chunk)
                        progress_callback(downloaded)
                        
                        # Update speed calculation every second
                        now = asyncio.get_event_loop().time()
                        if now - last_speed_update >= 1.0:
                            speed = int((downloaded - last_downloaded) / (now - last_speed_update))
                            self._active_downloads[torrent_hash] = speed
                            last_speed_update = now
                            last_downloaded = downloaded
                
                # Verify size
                if expected_size > 0 and downloaded != expected_size:
                    logger.warning(f"Size mismatch: expected {expected_size}, got {downloaded}")

    async def add_torrent(
        self,
        magnet_link: Optional[str] = None,
        torrent_file: Optional[bytes] = None,
        category: str = "",
    ) -> Optional[str]:
        """
        Add a torrent to Seedr or queue if storage is full.
        Returns the torrent hash if successful.
        """
        if not self._client:
            await self.initialize()

        async with self._lock:
            try:
                # Update storage info
                await self._update_storage_info()
                
                # Extract name from magnet link if possible
                name = "Unknown Torrent"
                estimated_size = 0
                
                if magnet_link:
                    # Try to extract name from magnet link
                    parsed = urllib.parse.urlparse(magnet_link)
                    params = urllib.parse.parse_qs(parsed.query)
                    if 'dn' in params:
                        name = urllib.parse.unquote(params['dn'][0])
                    # Try to get size hint from xl parameter
                    if 'xl' in params:
                        try:
                            estimated_size = int(params['xl'][0])
                        except ValueError:
                            pass
                
                # Check if we have enough storage (use 500MB as default estimate if unknown)
                check_size = estimated_size if estimated_size > 0 else 500 * 1024 * 1024
                available = self.get_available_storage()
                
                # If queue has items, add to queue to maintain order
                if len(self._torrent_queue) > 0:
                    logger.info(f"Queue not empty, adding to queue: {name}")
                    return await self._add_to_queue(magnet_link, torrent_file, category, name, estimated_size)
                
                # Check if we have storage space
                if available < check_size:
                    logger.warning(f"Insufficient Seedr storage ({available / 1024 / 1024:.1f}MB available), queuing: {name}")
                    return await self._add_to_queue(magnet_link, torrent_file, category, name, estimated_size)
                
                # Try to add to Seedr
                try:
                    if magnet_link:
                        result = await self._client.add_torrent(magnet_link=magnet_link)
                    elif torrent_file:
                        result = await self._client.add_torrent(torrent_file=torrent_file)
                    else:
                        raise ValueError("Either magnet_link or torrent_file required")
                except Exception as e:
                    error_str = str(e).lower()
                    # Check if it's a storage error
                    if 'space' in error_str or 'storage' in error_str or 'limit' in error_str or 'full' in error_str:
                        logger.warning(f"Seedr storage full, queuing: {name}")
                        return await self._add_to_queue(magnet_link, torrent_file, category, name, estimated_size)
                    raise

                # Get the transfer info from result
                torrent_hash = None
                if hasattr(result, 'hash') and result.hash:
                    torrent_hash = result.hash.upper()
                elif hasattr(result, 'id'):
                    torrent_hash = f"SEEDR{result.id}"
                else:
                    torrent_hash = f"SEEDR{id(result)}"

                name = getattr(result, 'name', name)
                
                # Store category mapping
                if category:
                    self._category_mapping[torrent_hash] = category
                    # Create category directory
                    cat_path = os.path.join(self.download_path, category)
                    os.makedirs(cat_path, exist_ok=True)

                logger.info(f"Added torrent: {name} (hash: {torrent_hash})")
                return torrent_hash

            except Exception as e:
                logger.error(f"Error adding torrent: {e}")
                raise

    async def _add_to_queue(
        self,
        magnet_link: Optional[str],
        torrent_file: Optional[bytes],
        category: str,
        name: str,
        estimated_size: int,
    ) -> str:
        """Add a torrent to the queue."""
        async with self._queue_lock:
            self._queue_counter += 1
            queue_id = f"{self._queue_counter:08d}"
            
            queued = QueuedTorrent(
                id=queue_id,
                magnet_link=magnet_link,
                torrent_file=torrent_file,
                category=category,
                name=name,
                estimated_size=estimated_size,
            )
            
            self._torrent_queue.append(queued)
            logger.info(f"Queued torrent #{queue_id}: {name} (queue size: {len(self._torrent_queue)})")
            
            # Store category mapping for queued item
            queue_hash = f"QUEUE{queue_id}".upper()
            if category:
                self._category_mapping[queue_hash] = category
            
            return queue_hash

    async def _process_queue(self):
        """Process queued torrents if storage is available."""
        if self._queue_processing:
            return
        
        self._queue_processing = True
        try:
            async with self._queue_lock:
                while self._torrent_queue:
                    # Update storage info
                    await self._update_storage_info()
                    available = self.get_available_storage()
                    
                    # Check next item in queue
                    queued = self._torrent_queue[0]
                    check_size = queued.estimated_size if queued.estimated_size > 0 else 500 * 1024 * 1024
                    
                    if available < check_size:
                        logger.debug(f"Not enough storage for queue item: {queued.name}")
                        break
                    
                    # Remove from queue and try to add
                    self._torrent_queue.popleft()
                    queue_hash = f"QUEUE{queued.id}".upper()
                    
                    try:
                        logger.info(f"Processing queued torrent: {queued.name}")
                        
                        if queued.magnet_link:
                            result = await self._client.add_torrent(magnet_link=queued.magnet_link)
                        elif queued.torrent_file:
                            result = await self._client.add_torrent(torrent_file=queued.torrent_file)
                        else:
                            logger.error(f"Queued torrent has no magnet or file: {queued.id}")
                            continue
                        
                        # Get the new hash
                        torrent_hash = None
                        if hasattr(result, 'hash') and result.hash:
                            torrent_hash = result.hash.upper()
                        elif hasattr(result, 'id'):
                            torrent_hash = f"SEEDR{result.id}"
                        
                        # Transfer category mapping
                        if queued.category:
                            self._category_mapping[torrent_hash] = queued.category
                            cat_path = os.path.join(self.download_path, queued.category)
                            os.makedirs(cat_path, exist_ok=True)
                        
                        # Clean up queue hash mapping
                        self._category_mapping.pop(queue_hash, None)
                        self._torrents_cache.pop(queue_hash, None)
                        
                        logger.info(f"Queued torrent added to Seedr: {queued.name} -> {torrent_hash}")
                        
                    except Exception as e:
                        error_str = str(e).lower()
                        if 'space' in error_str or 'storage' in error_str or 'limit' in error_str:
                            # Put back in queue
                            self._torrent_queue.appendleft(queued)
                            logger.warning(f"Still not enough storage, re-queued: {queued.name}")
                            break
                        else:
                            logger.error(f"Failed to add queued torrent: {e}")
                    
                    # Small delay between queue items
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
                # Check if it's a queued torrent
                if torrent_hash.startswith("QUEUE"):
                    queue_id = torrent_hash.replace("QUEUE", "")
                    async with self._queue_lock:
                        for i, queued in enumerate(list(self._torrent_queue)):
                            if queued.id == queue_id:
                                del self._torrent_queue[i]
                                self._category_mapping.pop(torrent_hash, None)
                                self._torrents_cache.pop(torrent_hash, None)
                                logger.info(f"Removed from queue: {queued.name}")
                                return True
                    return False
                
                # Cancel any active download
                if torrent_hash in self._download_tasks:
                    self._download_tasks[torrent_hash].cancel()
                    del self._download_tasks[torrent_hash]
                
                # Find the torrent by hash
                torrent = self._torrents_cache.get(torrent_hash)
                if not torrent:
                    logger.warning(f"Torrent not found in cache: {torrent_hash}")
                    return False

                # Delete from Seedr (if still there and not already locally downloaded and deleted)
                if torrent_hash not in self._local_downloads or not self.delete_after_download:
                    try:
                        if torrent.state in [TorrentState.COMPLETED, TorrentState.DOWNLOADING_LOCAL, TorrentState.COMPLETED_LOCAL]:
                            await self._client.delete_folder(torrent.id)
                        else:
                            await self._client.delete_torrent(torrent.id)
                        
                        # Update storage and process queue
                        await self._update_storage_info()
                        await self._process_queue()
                        
                    except Exception as e:
                        logger.warning(f"Could not delete from Seedr (may already be deleted): {e}")
                
                # Delete local files if requested
                if delete_files and torrent.content_path:
                    try:
                        import shutil
                        if os.path.exists(torrent.content_path):
                            shutil.rmtree(torrent.content_path)
                            logger.info(f"Deleted local files: {torrent.content_path}")
                    except Exception as e:
                        logger.warning(f"Could not delete local files: {e}")
                
                # Clean up local cache
                self._torrents_cache.pop(torrent_hash, None)
                self._category_mapping.pop(torrent_hash, None)
                self._download_progress.pop(torrent_hash, None)
                self._local_downloads.discard(torrent_hash)

                logger.info(f"Deleted torrent: {torrent.name}")
                return True

            except Exception as e:
                logger.error(f"Error deleting torrent: {e}")
                return False

    async def get_torrent(self, torrent_hash: str) -> Optional[SeedrTorrent]:
        """Get a specific torrent by hash."""
        # Refresh cache
        await self.get_torrents()
        return self._torrents_cache.get(torrent_hash)

    async def get_torrent_files(self, torrent_hash: str) -> list[dict]:
        """Get files for a specific torrent."""
        if not self._client:
            await self.initialize()

        torrent = self._torrents_cache.get(torrent_hash)
        if not torrent:
            return []

        # Queued torrents have no files yet
        if torrent.is_queued:
            return []

        # Only completed folders have files we can list
        if torrent.state not in [TorrentState.COMPLETED, TorrentState.DOWNLOADING_LOCAL, TorrentState.COMPLETED_LOCAL]:
            return []

        try:
            folder = await self._client.list_contents(str(torrent.id))
            files = []
            
            for idx, file in enumerate(folder.files):
                # Check if file exists locally
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

    async def download_file(
        self, file_id: str, destination: str
    ) -> bool:
        """Download a file from Seedr to local filesystem."""
        if not self._client:
            await self.initialize()

        try:
            # Get download link
            file_info = await self._client.fetch_file(file_id)
            download_url = file_info.url

            # Download the file
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
            
            return True, f"Connected as {settings.account.username} ({storage_pct:.1f}% storage used{queue_info})"
        except Exception as e:
            return False, str(e)

    async def force_download(self, torrent_hash: str) -> bool:
        """Force start downloading a torrent to local storage."""
        torrent = self._torrents_cache.get(torrent_hash)
        if not torrent:
            return False
        
        if torrent.is_queued:
            return False  # Can't download queued torrents
        
        if torrent_hash in self._download_tasks:
            return True  # Already downloading
        
        if torrent_hash in self._local_downloads:
            return True  # Already downloaded
        
        self._start_download_task(
            torrent_hash, 
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
                "estimated_size": q.estimated_size,
                "added_time": q.added_time,
                "position": i + 1,
            }
            for i, q in enumerate(self._torrent_queue)
        ]

    async def clear_queue(self) -> int:
        """Clear all queued torrents. Returns count of removed items."""
        async with self._queue_lock:
            count = len(self._torrent_queue)
            
            # Clean up category mappings
            for queued in self._torrent_queue:
                queue_hash = f"QUEUE{queued.id}".upper()
                self._category_mapping.pop(queue_hash, None)
                self._torrents_cache.pop(queue_hash, None)
            
            self._torrent_queue.clear()
            logger.info(f"Cleared {count} items from queue")
            return count