"""
Persistence Layer for Seedr-Sonarr Proxy
SQLite-based state persistence for torrents, queue, and configuration.
"""

import asyncio
import aiosqlite
import base64
import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Set

logger = logging.getLogger(__name__)


@dataclass
class PersistedTorrent:
    """Torrent state for persistence."""
    hash: str
    seedr_id: Optional[str]
    name: str
    size: int
    category: str
    instance_id: str
    state: str
    phase: str
    seedr_progress: float
    local_progress: float
    added_on: int
    save_path: str
    content_path: Optional[str] = None
    error_count: int = 0
    last_error: Optional[str] = None
    last_error_time: Optional[float] = None
    updated_at: float = 0.0

    def __post_init__(self):
        if not self.updated_at:
            self.updated_at = datetime.now().timestamp()


@dataclass
class PersistedQueuedTorrent:
    """Queued torrent for persistence."""
    id: str
    magnet_link: Optional[str]
    torrent_file_b64: Optional[str]
    category: str
    instance_id: str
    name: str
    estimated_size: int
    added_time: float
    retry_count: int = 0
    last_retry: Optional[float] = None


@dataclass
class PersistedCategory:
    """Category for persistence."""
    name: str
    save_path: str
    instance_id: str


@dataclass
class ActivityLogEntry:
    """Activity log entry."""
    id: Optional[int]
    timestamp: float
    torrent_hash: Optional[str]
    torrent_name: Optional[str]
    category: Optional[str]
    action: str
    details: Optional[str]
    level: str = "INFO"


@dataclass
class PersistedQnapPending:
    """QNAP pending download for persistence."""
    torrent_hash: str
    folder_id: str
    folder_name: str
    total_size: int
    category: str
    instance_id: str
    save_path: str
    content_path: str
    created_at: float = 0.0

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().timestamp()


# SQL Schema
SCHEMA = """
CREATE TABLE IF NOT EXISTS torrents (
    hash TEXT PRIMARY KEY,
    seedr_id TEXT,
    name TEXT NOT NULL,
    size INTEGER NOT NULL,
    category TEXT DEFAULT '',
    instance_id TEXT DEFAULT '',
    state TEXT NOT NULL,
    phase TEXT NOT NULL,
    seedr_progress REAL DEFAULT 0.0,
    local_progress REAL DEFAULT 0.0,
    added_on INTEGER NOT NULL,
    save_path TEXT NOT NULL,
    content_path TEXT,
    error_count INTEGER DEFAULT 0,
    last_error TEXT,
    last_error_time REAL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS queued_torrents (
    id TEXT PRIMARY KEY,
    magnet_link TEXT,
    torrent_file_b64 TEXT,
    category TEXT DEFAULT '',
    instance_id TEXT DEFAULT '',
    name TEXT NOT NULL,
    estimated_size INTEGER DEFAULT 0,
    added_time REAL NOT NULL,
    retry_count INTEGER DEFAULT 0,
    last_retry REAL
);

CREATE TABLE IF NOT EXISTS categories (
    name TEXT PRIMARY KEY,
    save_path TEXT NOT NULL,
    instance_id TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS hash_mappings (
    queue_hash TEXT PRIMARY KEY,
    real_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS local_downloads (
    hash TEXT PRIMARY KEY,
    completed_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    torrent_hash TEXT,
    torrent_name TEXT,
    category TEXT,
    action TEXT NOT NULL,
    details TEXT,
    level TEXT DEFAULT 'INFO'
);

CREATE TABLE IF NOT EXISTS qnap_pending (
    torrent_hash TEXT PRIMARY KEY,
    folder_id TEXT NOT NULL,
    folder_name TEXT NOT NULL,
    total_size INTEGER NOT NULL,
    category TEXT DEFAULT '',
    instance_id TEXT DEFAULT '',
    save_path TEXT NOT NULL,
    content_path TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_activity_timestamp ON activity_log(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_torrents_instance ON torrents(instance_id);
CREATE INDEX IF NOT EXISTS idx_categories_instance ON categories(instance_id);
CREATE INDEX IF NOT EXISTS idx_torrents_state ON torrents(state);
CREATE INDEX IF NOT EXISTS idx_queued_added ON queued_torrents(added_time);
"""


class PersistenceManager:
    """
    Manages state persistence to SQLite.
    Provides async CRUD operations for all state types.
    """

    def __init__(self, db_path: str = "seedr_state.db"):
        self.db_path = db_path
        self._lock = asyncio.Lock()
        self._initialized = False

    async def initialize(self) -> None:
        """Create database and tables if they don't exist."""
        async with self._lock:
            if self._initialized:
                return

            # Ensure directory exists
            db_dir = Path(self.db_path).parent
            if db_dir and str(db_dir) != ".":
                db_dir.mkdir(parents=True, exist_ok=True)

            async with aiosqlite.connect(self.db_path) as db:
                await db.executescript(SCHEMA)
                await db.commit()

            self._initialized = True
            logger.info(f"Persistence initialized: {self.db_path}")

    async def close(self) -> None:
        """Close the persistence manager."""
        self._initialized = False

    # -------------------------------------------------------------------------
    # Torrent Operations
    # -------------------------------------------------------------------------

    async def save_torrent(self, torrent: PersistedTorrent) -> None:
        """Save or update a torrent."""
        torrent.updated_at = datetime.now().timestamp()

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT OR REPLACE INTO torrents
                (hash, seedr_id, name, size, category, instance_id, state, phase,
                 seedr_progress, local_progress, added_on, save_path, content_path,
                 error_count, last_error, last_error_time, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                torrent.hash, torrent.seedr_id, torrent.name, torrent.size,
                torrent.category, torrent.instance_id, torrent.state, torrent.phase,
                torrent.seedr_progress, torrent.local_progress, torrent.added_on,
                torrent.save_path, torrent.content_path, torrent.error_count,
                torrent.last_error, torrent.last_error_time, torrent.updated_at
            ))
            await db.commit()

    async def get_torrents(self) -> List[PersistedTorrent]:
        """Get all persisted torrents."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM torrents") as cursor:
                rows = await cursor.fetchall()
                return [PersistedTorrent(
                    hash=row["hash"],
                    seedr_id=row["seedr_id"],
                    name=row["name"],
                    size=row["size"],
                    category=row["category"],
                    instance_id=row["instance_id"],
                    state=row["state"],
                    phase=row["phase"],
                    seedr_progress=row["seedr_progress"],
                    local_progress=row["local_progress"],
                    added_on=row["added_on"],
                    save_path=row["save_path"],
                    content_path=row["content_path"],
                    error_count=row["error_count"],
                    last_error=row["last_error"],
                    last_error_time=row["last_error_time"],
                    updated_at=row["updated_at"],
                ) for row in rows]

    async def get_torrent(self, hash: str) -> Optional[PersistedTorrent]:
        """Get a specific torrent by hash."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM torrents WHERE hash = ?", (hash,)
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                return PersistedTorrent(
                    hash=row["hash"],
                    seedr_id=row["seedr_id"],
                    name=row["name"],
                    size=row["size"],
                    category=row["category"],
                    instance_id=row["instance_id"],
                    state=row["state"],
                    phase=row["phase"],
                    seedr_progress=row["seedr_progress"],
                    local_progress=row["local_progress"],
                    added_on=row["added_on"],
                    save_path=row["save_path"],
                    content_path=row["content_path"],
                    error_count=row["error_count"],
                    last_error=row["last_error"],
                    last_error_time=row["last_error_time"],
                    updated_at=row["updated_at"],
                )

    async def delete_torrent(self, hash: str) -> None:
        """Delete a torrent."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM torrents WHERE hash = ?", (hash,))
            await db.commit()

    async def update_torrent_progress(
        self, hash: str, seedr_progress: float = None, local_progress: float = None
    ) -> None:
        """Update torrent progress efficiently."""
        updates = []
        params = []

        if seedr_progress is not None:
            updates.append("seedr_progress = ?")
            params.append(seedr_progress)
        if local_progress is not None:
            updates.append("local_progress = ?")
            params.append(local_progress)

        if not updates:
            return

        updates.append("updated_at = ?")
        params.append(datetime.now().timestamp())
        params.append(hash)

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                f"UPDATE torrents SET {', '.join(updates)} WHERE hash = ?",
                params
            )
            await db.commit()

    async def update_torrent_error(
        self, hash: str, error: str, increment_count: bool = True
    ) -> None:
        """Update torrent error state."""
        now = datetime.now().timestamp()

        async with aiosqlite.connect(self.db_path) as db:
            if increment_count:
                await db.execute("""
                    UPDATE torrents
                    SET last_error = ?, last_error_time = ?,
                        error_count = error_count + 1, updated_at = ?
                    WHERE hash = ?
                """, (error, now, now, hash))
            else:
                await db.execute("""
                    UPDATE torrents
                    SET last_error = ?, last_error_time = ?, updated_at = ?
                    WHERE hash = ?
                """, (error, now, now, hash))
            await db.commit()

    async def clear_torrent_error(self, hash: str) -> None:
        """Clear torrent error state."""
        now = datetime.now().timestamp()

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                UPDATE torrents
                SET last_error = NULL, last_error_time = NULL,
                    error_count = 0, updated_at = ?
                WHERE hash = ?
            """, (now, hash))
            await db.commit()

    # -------------------------------------------------------------------------
    # Queue Operations
    # -------------------------------------------------------------------------

    async def save_queued(self, queued: PersistedQueuedTorrent) -> None:
        """Save a queued torrent."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT OR REPLACE INTO queued_torrents
                (id, magnet_link, torrent_file_b64, category, instance_id, name,
                 estimated_size, added_time, retry_count, last_retry)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                queued.id, queued.magnet_link, queued.torrent_file_b64,
                queued.category, queued.instance_id, queued.name,
                queued.estimated_size, queued.added_time, queued.retry_count,
                queued.last_retry
            ))
            await db.commit()

    async def get_queued(self) -> List[PersistedQueuedTorrent]:
        """Get all queued torrents ordered by added time."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM queued_torrents ORDER BY added_time ASC"
            ) as cursor:
                rows = await cursor.fetchall()
                return [PersistedQueuedTorrent(
                    id=row["id"],
                    magnet_link=row["magnet_link"],
                    torrent_file_b64=row["torrent_file_b64"],
                    category=row["category"],
                    instance_id=row["instance_id"],
                    name=row["name"],
                    estimated_size=row["estimated_size"],
                    added_time=row["added_time"],
                    retry_count=row["retry_count"],
                    last_retry=row["last_retry"],
                ) for row in rows]

    async def delete_queued(self, id: str) -> None:
        """Delete a queued torrent."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM queued_torrents WHERE id = ?", (id,))
            await db.commit()

    async def clear_queue(self) -> int:
        """Clear all queued torrents. Returns count deleted."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM queued_torrents") as cursor:
                row = await cursor.fetchone()
                count = row[0] if row else 0
            await db.execute("DELETE FROM queued_torrents")
            await db.commit()
            return count

    async def update_queued_retry(self, id: str) -> None:
        """Increment retry count for a queued torrent."""
        now = datetime.now().timestamp()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                UPDATE queued_torrents
                SET retry_count = retry_count + 1, last_retry = ?
                WHERE id = ?
            """, (now, id))
            await db.commit()

    # -------------------------------------------------------------------------
    # Category Operations
    # -------------------------------------------------------------------------

    async def save_category(
        self, name: str, save_path: str, instance_id: str = ""
    ) -> None:
        """Save or update a category."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT OR REPLACE INTO categories (name, save_path, instance_id)
                VALUES (?, ?, ?)
            """, (name, save_path, instance_id))
            await db.commit()

    async def get_categories(self) -> Dict[str, PersistedCategory]:
        """Get all categories."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM categories") as cursor:
                rows = await cursor.fetchall()
                return {
                    row["name"]: PersistedCategory(
                        name=row["name"],
                        save_path=row["save_path"],
                        instance_id=row["instance_id"],
                    )
                    for row in rows
                }

    async def delete_category(self, name: str) -> None:
        """Delete a category."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM categories WHERE name = ?", (name,))
            await db.commit()

    # -------------------------------------------------------------------------
    # Hash Mapping Operations
    # -------------------------------------------------------------------------

    async def save_hash_mapping(self, queue_hash: str, real_hash: str) -> None:
        """Save a queue hash to real hash mapping."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT OR REPLACE INTO hash_mappings (queue_hash, real_hash)
                VALUES (?, ?)
            """, (queue_hash, real_hash))
            await db.commit()

    async def get_hash_mappings(self) -> Dict[str, str]:
        """Get all hash mappings."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT * FROM hash_mappings") as cursor:
                rows = await cursor.fetchall()
                return {row[0]: row[1] for row in rows}

    async def delete_hash_mapping(self, queue_hash: str) -> None:
        """Delete a hash mapping."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM hash_mappings WHERE queue_hash = ?", (queue_hash,)
            )
            await db.commit()

    async def delete_hash_mappings_by_real(self, real_hash: str) -> None:
        """Delete all mappings pointing to a real hash."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM hash_mappings WHERE real_hash = ?", (real_hash,)
            )
            await db.commit()

    # -------------------------------------------------------------------------
    # Local Downloads Operations
    # -------------------------------------------------------------------------

    async def mark_local_download(self, hash: str) -> None:
        """Mark a torrent as downloaded locally."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT OR REPLACE INTO local_downloads (hash, completed_at)
                VALUES (?, ?)
            """, (hash, datetime.now().timestamp()))
            await db.commit()

    async def get_local_downloads(self) -> Set[str]:
        """Get all locally downloaded torrent hashes."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT hash FROM local_downloads") as cursor:
                rows = await cursor.fetchall()
                return {row[0] for row in rows}

    async def delete_local_download(self, hash: str) -> None:
        """Remove a torrent from local downloads."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM local_downloads WHERE hash = ?", (hash,))
            await db.commit()

    # -------------------------------------------------------------------------
    # QNAP Pending Operations
    # -------------------------------------------------------------------------

    async def save_qnap_pending(self, pending: "PersistedQnapPending") -> None:
        """Save a QNAP pending download."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT OR REPLACE INTO qnap_pending
                (torrent_hash, folder_id, folder_name, total_size, category,
                 instance_id, save_path, content_path, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                pending.torrent_hash, pending.folder_id, pending.folder_name,
                pending.total_size, pending.category, pending.instance_id,
                pending.save_path, pending.content_path, pending.created_at
            ))
            await db.commit()

    async def get_qnap_pending(self) -> List["PersistedQnapPending"]:
        """Get all QNAP pending downloads."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM qnap_pending") as cursor:
                rows = await cursor.fetchall()
                return [PersistedQnapPending(
                    torrent_hash=row["torrent_hash"],
                    folder_id=row["folder_id"],
                    folder_name=row["folder_name"],
                    total_size=row["total_size"],
                    category=row["category"],
                    instance_id=row["instance_id"],
                    save_path=row["save_path"],
                    content_path=row["content_path"],
                    created_at=row["created_at"],
                ) for row in rows]

    async def delete_qnap_pending(self, torrent_hash: str) -> None:
        """Delete a QNAP pending download."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM qnap_pending WHERE torrent_hash = ?", (torrent_hash,)
            )
            await db.commit()

    async def clear_qnap_pending(self) -> int:
        """Clear all QNAP pending downloads. Returns count deleted."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM qnap_pending") as cursor:
                row = await cursor.fetchone()
                count = row[0] if row else 0
            await db.execute("DELETE FROM qnap_pending")
            await db.commit()
            return count

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
        """Log an activity entry."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO activity_log
                (timestamp, torrent_hash, torrent_name, category, action, details, level)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                datetime.now().timestamp(),
                torrent_hash, torrent_name, category, action, details, level
            ))
            await db.commit()

    async def get_activity_log(
        self,
        limit: int = 100,
        level: str = None,
        torrent_hash: str = None,
        since: float = None,
    ) -> List[ActivityLogEntry]:
        """Get activity log entries with optional filtering."""
        query = "SELECT * FROM activity_log WHERE 1=1"
        params = []

        if level:
            level_priority = {
                "DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50
            }
            min_priority = level_priority.get(level.upper(), 0)
            levels = [l for l, p in level_priority.items() if p >= min_priority]
            placeholders = ",".join("?" * len(levels))
            query += f" AND level IN ({placeholders})"
            params.extend(levels)

        if torrent_hash:
            query += " AND torrent_hash = ?"
            params.append(torrent_hash)

        if since:
            query += " AND timestamp >= ?"
            params.append(since)

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
                return [ActivityLogEntry(
                    id=row["id"],
                    timestamp=row["timestamp"],
                    torrent_hash=row["torrent_hash"],
                    torrent_name=row["torrent_name"],
                    category=row["category"],
                    action=row["action"],
                    details=row["details"],
                    level=row["level"],
                ) for row in rows]

    async def prune_activity_log(self, max_entries: int = 10000) -> int:
        """Prune old activity log entries. Returns count deleted."""
        async with aiosqlite.connect(self.db_path) as db:
            # Get count of entries to delete
            async with db.execute(
                "SELECT COUNT(*) FROM activity_log"
            ) as cursor:
                row = await cursor.fetchone()
                total = row[0] if row else 0

            if total <= max_entries:
                return 0

            to_delete = total - max_entries
            await db.execute("""
                DELETE FROM activity_log WHERE id IN (
                    SELECT id FROM activity_log ORDER BY timestamp ASC LIMIT ?
                )
            """, (to_delete,))
            await db.commit()
            return to_delete

    # -------------------------------------------------------------------------
    # Utility Operations
    # -------------------------------------------------------------------------

    async def get_stats(self) -> Dict:
        """Get database statistics."""
        async with aiosqlite.connect(self.db_path) as db:
            stats = {}

            for table in ["torrents", "queued_torrents", "categories",
                         "hash_mappings", "local_downloads", "qnap_pending", "activity_log"]:
                async with db.execute(f"SELECT COUNT(*) FROM {table}") as cursor:
                    row = await cursor.fetchone()
                    stats[table] = row[0] if row else 0

            return stats

    async def vacuum(self) -> None:
        """Optimize the database."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("VACUUM")
