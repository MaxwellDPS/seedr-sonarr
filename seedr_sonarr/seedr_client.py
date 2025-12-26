"""
Seedr Client Wrapper
Provides a clean interface to the Seedr API using seedrcc library.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional
import os
import aiohttp
import aiofiles

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


class SeedrClientWrapper:
    """
    Wrapper around seedrcc that provides simplified interface for the proxy.
    Handles authentication, token refresh, and API calls.
    """

    def __init__(
        self,
        email: Optional[str] = None,
        password: Optional[str] = None,
        token: Optional[str] = None,
        download_path: str = "/downloads",
    ):
        self.email = email
        self.password = password
        self._token = token
        self.download_path = download_path
        self._client = None
        self._lock = asyncio.Lock()
        self._torrents_cache: dict[str, SeedrTorrent] = {}
        self._category_mapping: dict[str, str] = {}  # torrent_hash -> category

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

            logger.info("Seedr client initialized successfully")
            return True
        except ImportError:
            logger.error("seedrcc library not installed. Run: pip install seedrcc")
            raise
        except Exception as e:
            logger.error(f"Failed to initialize Seedr client: {e}")
            raise

    async def close(self):
        """Close the client connection."""
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

    def _parse_progress(self, progress_str: str) -> float:
        """Parse progress string to float (0.0 to 1.0)."""
        try:
            # Progress might be "50" or "50%" or "50.5"
            progress = float(str(progress_str).replace('%', '').strip())
            return progress / 100.0 if progress > 1 else progress
        except (ValueError, TypeError):
            return 0.0

    async def get_torrents(self) -> list[SeedrTorrent]:
        """Get all torrents from Seedr."""
        if not self._client:
            await self.initialize()

        async with self._lock:
            try:
                torrents = []

                # Get root folder contents - includes both active torrents and completed folders
                contents = await self._client.list_contents()

                # Process active torrents (downloading)
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
                        save_path=self.download_path,
                        category=self._category_mapping.get(torrent_hash, ""),
                        downloaded=int(size * progress),
                        completed=int(size * progress),
                    )
                    torrents.append(torrent)
                    self._torrents_cache[torrent_hash] = torrent

                # Process completed folders (finished downloads)
                for folder in contents.folders:
                    # Generate a consistent hash from folder ID
                    torrent_hash = f"SEEDR{folder.id:016X}".upper()
                    
                    last_update = folder.last_update
                    timestamp = int(last_update.timestamp()) if last_update else int(datetime.now().timestamp())
                    
                    torrent = SeedrTorrent(
                        id=str(folder.id),
                        hash=torrent_hash,
                        name=folder.name,
                        size=folder.size,
                        progress=1.0,
                        state=TorrentState.COMPLETED,
                        added_on=timestamp,
                        completion_on=timestamp,
                        save_path=self.download_path,
                        content_path=os.path.join(self.download_path, folder.name),
                        category=self._category_mapping.get(torrent_hash, ""),
                        downloaded=folder.size,
                        completed=folder.size,
                    )
                    torrents.append(torrent)
                    self._torrents_cache[torrent_hash] = torrent

                return torrents

            except Exception as e:
                logger.error(f"Error getting torrents: {e}")
                return []

    async def add_torrent(
        self,
        magnet_link: Optional[str] = None,
        torrent_file: Optional[bytes] = None,
        category: str = "",
    ) -> Optional[str]:
        """
        Add a torrent to Seedr.
        Returns the torrent hash if successful.
        """
        if not self._client:
            await self.initialize()

        async with self._lock:
            try:
                if magnet_link:
                    result = await self._client.add_torrent(magnet_link=magnet_link)
                elif torrent_file:
                    result = await self._client.add_torrent(torrent_file=torrent_file)
                else:
                    raise ValueError("Either magnet_link or torrent_file required")

                # Get the transfer info from result
                torrent_hash = None
                if hasattr(result, 'hash') and result.hash:
                    torrent_hash = result.hash.upper()
                elif hasattr(result, 'id'):
                    torrent_hash = f"SEEDR{result.id}"
                else:
                    torrent_hash = f"SEEDR{id(result)}"

                name = getattr(result, 'name', f"torrent_{torrent_hash}")
                
                # Store category mapping
                if category:
                    self._category_mapping[torrent_hash] = category

                logger.info(f"Added torrent: {name} (hash: {torrent_hash})")
                return torrent_hash

            except Exception as e:
                logger.error(f"Error adding torrent: {e}")
                raise

    async def delete_torrent(
        self, torrent_hash: str, delete_files: bool = False
    ) -> bool:
        """Delete a torrent from Seedr."""
        if not self._client:
            await self.initialize()

        async with self._lock:
            try:
                # Find the torrent by hash
                torrent = self._torrents_cache.get(torrent_hash)
                if not torrent:
                    logger.warning(f"Torrent not found in cache: {torrent_hash}")
                    return False

                # Determine if it's an active torrent or completed folder
                if torrent.state == TorrentState.COMPLETED:
                    await self._client.delete_folder(torrent.id)
                else:
                    await self._client.delete_torrent(torrent.id)
                
                # Clean up local cache
                self._torrents_cache.pop(torrent_hash, None)
                self._category_mapping.pop(torrent_hash, None)

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

        # Only completed folders have files we can list
        if torrent.state != TorrentState.COMPLETED:
            return []

        try:
            folder = await self._client.list_contents(str(torrent.id))
            files = []
            
            for idx, file in enumerate(folder.files):
                files.append({
                    "index": idx,
                    "name": file.name,
                    "size": file.size,
                    "progress": 1.0,
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
            usage = await self._client.get_memory_bandwidth()
            
            return {
                "username": settings.account.username,
                "email": settings.account.email,
                "space_used": getattr(usage, 'space_used', 0) or 0,
                "space_max": getattr(usage, 'space_max', 0) or 0,
                "bandwidth_used": getattr(usage, 'bandwidth_used', 0) or 0,
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
            return True, f"Connected as {settings.account.username}"
        except Exception as e:
            return False, str(e)