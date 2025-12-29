"""
qBittorrent API Emulation Layer
Implements the qBittorrent Web API that Sonarr/Radarr expects.
Supports multiple instances via category-based routing.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Optional
import secrets

from fastapi import FastAPI, Form, HTTPException, Request, Response, UploadFile, File
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel
from pydantic_settings import BaseSettings

from .seedr_client import SeedrClientWrapper, TorrentState
from .persistence import PersistenceManager
from .state import StateManager, TorrentPhase, extract_instance_id, CategoryState
from .retry import RetryConfig, CircuitBreakerConfig
from .logging_config import setup_logging, ActivityLogHandler

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """Application settings loaded from environment."""

    # Seedr credentials
    seedr_email: str = ""
    seedr_password: str = ""
    seedr_token: Optional[str] = None

    # Server settings
    host: str = "0.0.0.0"
    port: int = 8080
    username: str = "admin"
    password: str = "adminadmin"

    # Download settings
    download_path: str = "/downloads"
    temp_path: str = "/downloads/temp"

    # Auto-download from Seedr to local storage
    auto_download: bool = True
    delete_after_download: bool = True

    # QNAP Download Station settings (optional - if enabled, uses QNAP for local downloads)
    qnap_enabled: bool = False
    qnap_host: str = ""
    qnap_port: int = 8080
    qnap_username: str = ""
    qnap_password: str = ""
    qnap_use_https: bool = False
    qnap_verify_ssl: bool = True
    qnap_temp_folder: str = "Download"  # Temp folder on QNAP for in-progress downloads
    qnap_dest_folder: str = ""  # Final destination folder on QNAP (e.g., "Multimedia/TV")

    # Persistence settings
    config_path: str = "/config"  # Path for persistent config/state data
    state_file: str = "seedr_state.db"  # Filename only, will be joined with config_path
    persist_state: bool = True

    # Default categories to create on startup (comma-separated)
    default_categories: str = "radarr,tv-sonarr"

    # Retry settings
    retry_max_attempts: int = 3
    retry_initial_delay: float = 1.0
    retry_max_delay: float = 60.0

    # Circuit breaker settings
    circuit_failure_threshold: int = 5
    circuit_reset_timeout: float = 60.0

    # Logging settings
    log_level: str = "INFO"
    log_file: Optional[str] = None
    log_format: str = "text"  # "text" or "json"
    log_max_size_mb: int = 10
    log_backup_count: int = 5
    activity_log_size: int = 1000

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


# Global instances
settings = Settings()
seedr_client: Optional[SeedrClientWrapper] = None
state_manager: Optional[StateManager] = None
persistence_manager: Optional[PersistenceManager] = None
activity_log_handler: Optional[ActivityLogHandler] = None
qnap_client = None  # Optional QNAP Download Station client
sessions: dict[str, datetime] = {}
SESSION_TIMEOUT = timedelta(hours=24)
SESSION_CLEANUP_INTERVAL = 300  # Clean up expired sessions every 5 minutes
QUEUE_PROCESS_INTERVAL = 60  # Process queue every 60 seconds
_session_cleanup_task: Optional[asyncio.Task] = None
_queue_process_task: Optional[asyncio.Task] = None
created_categories: dict[str, dict] = {}  # name -> {savePath, instance_id}


async def _cleanup_expired_sessions():
    """Periodically clean up expired sessions to prevent memory leaks."""
    while True:
        try:
            await asyncio.sleep(SESSION_CLEANUP_INTERVAL)
            now = datetime.now()
            expired = [sid for sid, expiry in sessions.items() if now > expiry]
            for sid in expired:
                sessions.pop(sid, None)
            if expired:
                logger.debug(f"Cleaned up {len(expired)} expired sessions")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"Error in session cleanup: {e}")


async def _periodic_queue_processor():
    """Periodically process the torrent queue to add queued items to Seedr."""
    while True:
        try:
            await asyncio.sleep(QUEUE_PROCESS_INTERVAL)
            if seedr_client and seedr_client._torrent_queue:
                queue_size = len(seedr_client._torrent_queue)
                logger.debug(f"Periodic queue check: {queue_size} torrents in queue")
                await seedr_client._process_queue()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"Error in periodic queue processor: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    global seedr_client, state_manager, persistence_manager, activity_log_handler, qnap_client, _session_cleanup_task, _queue_process_task

    # Setup logging first
    activity_log_handler = setup_logging(
        log_level=settings.log_level,
        log_file=settings.log_file,
        log_format=settings.log_format,
        max_file_size_mb=settings.log_max_size_mb,
        backup_count=settings.log_backup_count,
        activity_log_size=settings.activity_log_size,
    )

    logger.info("Starting Seedr-Sonarr proxy with full lifecycle management...")

    # Create download and config directories with permissive permissions
    try:
        os.makedirs(settings.download_path, exist_ok=True)
        os.makedirs(settings.temp_path, exist_ok=True)
        # Set permissive permissions so all users can read/write
        try:
            os.chmod(settings.download_path, 0o777)
            os.chmod(settings.temp_path, 0o777)
        except OSError:
            pass  # May fail if not owner
        logger.info(f"Download path: {settings.download_path}")
    except OSError as e:
        logger.warning(f"Could not create download directories: {e}")

    # Ensure config path exists and is writable
    config_path = settings.config_path
    try:
        os.makedirs(config_path, exist_ok=True)
        # Test if we can write to the config path
        test_file = os.path.join(config_path, ".write_test")
        with open(test_file, "w") as f:
            f.write("test")
        os.remove(test_file)
        logger.info(f"Config path: {config_path}")
    except OSError as e:
        logger.warning(f"Config path {config_path} not writable: {e}, falling back to /tmp")
        config_path = "/tmp"
        os.makedirs(config_path, exist_ok=True)

    # Initialize persistence - use config_path for state file
    if settings.persist_state:
        state_file_path = os.path.join(config_path, settings.state_file)
        try:
            persistence_manager = PersistenceManager(state_file_path)
            await persistence_manager.initialize()
            logger.info(f"Persistence enabled: {state_file_path}")
        except Exception as e:
            logger.error(f"Failed to initialize persistence at {state_file_path}: {e}")
            logger.warning("Continuing without persistence")
            persistence_manager = None

    # Initialize state manager
    state_manager = StateManager(
        persistence=persistence_manager,
        persist_enabled=settings.persist_state,
    )
    await state_manager.initialize()

    # Load persisted categories
    categories = await state_manager.get_categories()
    for name, cat in categories.items():
        created_categories[name] = {
            "savePath": cat.save_path,
            "instance_id": cat.instance_id,
        }
    logger.info(f"Loaded {len(created_categories)} categories from persistence")

    # Auto-create default categories if they don't exist
    if settings.default_categories:
        for cat_name in settings.default_categories.split(","):
            cat_name = cat_name.strip()
            if cat_name and cat_name not in created_categories:
                instance_id = extract_instance_id(cat_name)
                save_path = os.path.join(settings.download_path, cat_name)
                created_categories[cat_name] = {
                    "savePath": save_path,
                    "instance_id": instance_id,
                }
                # Create the directory with permissive permissions
                try:
                    os.makedirs(save_path, exist_ok=True)
                    os.chmod(save_path, 0o777)
                except OSError as e:
                    logger.warning(f"Could not create category directory {save_path}: {e}")
                # Persist the category
                if state_manager:
                    await state_manager.add_category(CategoryState(
                        name=cat_name,
                        save_path=save_path,
                        instance_id=instance_id,
                    ))
                logger.info(f"Auto-created default category: {cat_name} -> {save_path}")

    # Create retry and circuit breaker configs
    retry_config = RetryConfig(
        max_attempts=settings.retry_max_attempts,
        initial_delay=settings.retry_initial_delay,
        max_delay=settings.retry_max_delay,
    )

    circuit_config = CircuitBreakerConfig(
        failure_threshold=settings.circuit_failure_threshold,
        reset_timeout=settings.circuit_reset_timeout,
    )

    # Initialize QNAP Download Station client if enabled
    if settings.qnap_enabled and settings.qnap_host:
        try:
            from .qnap_client import QnapDownloadStationClient

            qnap_client = QnapDownloadStationClient(
                host=settings.qnap_host,
                port=settings.qnap_port,
                username=settings.qnap_username,
                password=settings.qnap_password,
                use_https=settings.qnap_use_https,
                verify_ssl=settings.qnap_verify_ssl,
            )
            success, message = await qnap_client.test_connection()
            if success:
                logger.info(f"QNAP Download Station connected: {message}")
            else:
                logger.error(f"QNAP Download Station connection failed: {message}")
                qnap_client = None
        except Exception as e:
            logger.error(f"Failed to initialize QNAP client: {e}")
            qnap_client = None

    # Initialize Seedr client
    seedr_client = SeedrClientWrapper(
        email=settings.seedr_email,
        password=settings.seedr_password,
        token=settings.seedr_token,
        download_path=settings.download_path,
        auto_download=settings.auto_download,
        delete_after_download=settings.delete_after_download,
        state_manager=state_manager,
        retry_config=retry_config,
        circuit_config=circuit_config,
        qnap_client=qnap_client,
        qnap_temp_folder=settings.qnap_temp_folder,
        qnap_dest_folder=settings.qnap_dest_folder,
    )

    try:
        await seedr_client.initialize()
        logger.info("Seedr client connected successfully")

        # Process any queued torrents on startup
        if seedr_client._torrent_queue:
            logger.info(f"Processing {len(seedr_client._torrent_queue)} queued torrents on startup...")
            await seedr_client._process_queue()
            logger.info(f"Queue processing complete. {len(seedr_client._torrent_queue)} torrents remaining in queue")
    except Exception as e:
        logger.error(f"Failed to initialize Seedr client: {e}")

    # Start background tasks
    _session_cleanup_task = asyncio.create_task(_cleanup_expired_sessions())
    _queue_process_task = asyncio.create_task(_periodic_queue_processor())

    yield

    # Cancel background tasks
    if _queue_process_task:
        _queue_process_task.cancel()
        try:
            await _queue_process_task
        except asyncio.CancelledError:
            pass

    if _session_cleanup_task:
        _session_cleanup_task.cancel()
        try:
            await _session_cleanup_task
        except asyncio.CancelledError:
            pass

    # Shutdown
    if qnap_client:
        await qnap_client.close()
    if seedr_client:
        await seedr_client.close()
    if state_manager:
        await state_manager.close()
    logger.info("Seedr-Sonarr proxy stopped")


app = FastAPI(
    title="Seedr-Sonarr Proxy",
    description="qBittorrent API emulation for Seedr.cc with full lifecycle management",
    version="2.0.0",
    lifespan=lifespan,
)


# =============================================================================
# Helper Functions
# =============================================================================


def validate_session(request: Request) -> bool:
    """Validate the session cookie."""
    sid = request.cookies.get("SID")
    if not sid:
        return False

    if sid not in sessions:
        return False

    if datetime.now() > sessions[sid]:
        del sessions[sid]
        return False

    sessions[sid] = datetime.now() + SESSION_TIMEOUT
    return True


def create_session() -> str:
    """Create a new session."""
    sid = secrets.token_hex(16)
    sessions[sid] = datetime.now() + SESSION_TIMEOUT
    return sid


def state_to_qbittorrent(state: TorrentState) -> str:
    """Convert internal state to qBittorrent state string."""
    return state.value


def sanitize_error_message(error: Exception) -> str:
    """Sanitize error messages to avoid leaking sensitive information."""
    error_str = str(error)
    # List of patterns that might contain sensitive info
    sensitive_patterns = [
        "token",
        "password",
        "secret",
        "key",
        "auth",
        "credential",
        "bearer",
    ]
    error_lower = error_str.lower()
    for pattern in sensitive_patterns:
        if pattern in error_lower:
            return "An internal error occurred. Check server logs for details."
    # Truncate very long error messages
    if len(error_str) > 200:
        return error_str[:200] + "..."
    return error_str


def phase_to_description(phase: TorrentPhase) -> str:
    """Get human-readable description of a phase."""
    descriptions = {
        TorrentPhase.QUEUED_STORAGE: "Waiting for Seedr storage",
        TorrentPhase.FETCHING_METADATA: "Fetching metadata",
        TorrentPhase.DOWNLOADING_TO_SEEDR: "Downloading to Seedr",
        TorrentPhase.SEEDR_PROCESSING: "Seedr processing",
        TorrentPhase.SEEDR_COMPLETE: "Ready in Seedr",
        TorrentPhase.QUEUED_LOCAL: "Waiting for local download",
        TorrentPhase.DOWNLOADING_TO_LOCAL: "Downloading to local",
        TorrentPhase.COMPLETED: "Completed",
        TorrentPhase.ERROR: "Error",
        TorrentPhase.PAUSED: "Paused",
        TorrentPhase.DELETED: "Deleted",
    }
    return descriptions.get(phase, "Unknown")


# =============================================================================
# Authentication Endpoints
# =============================================================================


@app.get("/api/v2/auth/login")
@app.post("/api/v2/auth/login")
async def auth_login(
    request: Request,
    response: Response,
    username: Optional[str] = Form(None),
    password: Optional[str] = Form(None),
):
    """Authenticate and create a session."""
    if not username:
        username = request.query_params.get("username", "")
    if not password:
        password = request.query_params.get("password", "")

    if username == settings.username and password == settings.password:
        sid = create_session()
        response = PlainTextResponse("Ok.")
        response.set_cookie(key="SID", value=sid, httponly=True)
        logger.info(f"User authenticated: {username}")
        return response

    logger.warning(f"Failed authentication attempt for: {username}")
    return PlainTextResponse("Fails.")


@app.get("/api/v2/auth/logout")
@app.post("/api/v2/auth/logout")
async def auth_logout(request: Request):
    """Logout and invalidate session."""
    sid = request.cookies.get("SID")
    if sid and sid in sessions:
        del sessions[sid]
    return PlainTextResponse("Ok.")


# =============================================================================
# Application Endpoints
# =============================================================================


@app.get("/api/v2/app/version")
async def app_version(request: Request):
    """Return qBittorrent version (emulated)."""
    if not validate_session(request):
        raise HTTPException(status_code=403, detail="Forbidden")
    return PlainTextResponse("v4.6.0")


@app.get("/api/v2/app/webapiVersion")
async def app_webapi_version(request: Request):
    """Return WebAPI version."""
    if not validate_session(request):
        raise HTTPException(status_code=403, detail="Forbidden")
    return PlainTextResponse("2.9.3")


@app.get("/api/v2/app/buildInfo")
async def app_build_info(request: Request):
    """Return build information."""
    if not validate_session(request):
        raise HTTPException(status_code=403, detail="Forbidden")
    return JSONResponse({
        "qt": "6.5.0",
        "libtorrent": "2.0.9.0",
        "boost": "1.82.0",
        "openssl": "3.0.9",
        "zlib": "1.2.13",
        "bitness": 64,
    })


@app.get("/api/v2/app/preferences")
async def app_preferences(request: Request):
    """Return application preferences."""
    if not validate_session(request):
        raise HTTPException(status_code=403, detail="Forbidden")
    return JSONResponse({
        "save_path": settings.download_path,
        "temp_path_enabled": True,
        "temp_path": settings.temp_path,
        "preallocate_all": False,
        "incomplete_files_ext": False,
        "auto_tmm_enabled": False,
        "torrent_changed_tmm_enabled": True,
        "save_path_changed_tmm_enabled": False,
        "category_changed_tmm_enabled": False,
        "use_subcategories": True,
        "export_dir": "",
        "export_dir_fin": "",
        "queueing_enabled": True,
        "max_active_downloads": 5,
        "max_active_torrents": 10,
        "max_active_uploads": 5,
    })


@app.get("/api/v2/app/defaultSavePath")
async def app_default_save_path(request: Request):
    """Return default save path."""
    if not validate_session(request):
        raise HTTPException(status_code=403, detail="Forbidden")
    return PlainTextResponse(settings.download_path)


# =============================================================================
# Transfer Info Endpoints
# =============================================================================


@app.get("/api/v2/transfer/info")
async def transfer_info(request: Request):
    """Return global transfer info."""
    if not validate_session(request):
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        torrents = await seedr_client.get_torrents()
        dl_speed = sum(t.download_speed for t in torrents)
        up_speed = sum(t.upload_speed for t in torrents)

        return JSONResponse({
            "dl_info_speed": dl_speed,
            "dl_info_data": sum(t.downloaded for t in torrents),
            "up_info_speed": up_speed,
            "up_info_data": sum(t.uploaded for t in torrents),
            "dl_rate_limit": 0,
            "up_rate_limit": 0,
            "dht_nodes": 0,
            "connection_status": "connected",
        })
    except Exception as e:
        logger.error(f"Error getting transfer info: {e}")
        raise HTTPException(status_code=500, detail=sanitize_error_message(e)) from e


# =============================================================================
# Torrent Endpoints
# =============================================================================


@app.get("/api/v2/torrents/info")
async def torrents_info(
    request: Request,
    filter: Optional[str] = None,
    category: Optional[str] = None,
    tag: Optional[str] = None,
    sort: Optional[str] = None,
    reverse: bool = False,
    limit: Optional[int] = None,
    offset: int = 0,
    hashes: Optional[str] = None,
):
    """Get info about torrents with multi-instance support."""
    if not validate_session(request):
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        torrents = await seedr_client.get_torrents()
        result = []

        hash_filter = set()
        if hashes:
            hash_filter = set(h.upper() for h in hashes.split("|"))

        for torrent in torrents:
            if hash_filter and torrent.hash not in hash_filter:
                continue

            if category and torrent.category != category:
                continue

            if filter:
                if filter == "downloading" and torrent.state not in [
                    TorrentState.DOWNLOADING,
                    TorrentState.QUEUED,
                    TorrentState.METADATA,
                ]:
                    continue
                elif filter == "completed" and torrent.state != TorrentState.COMPLETED:
                    continue
                elif filter == "paused" and torrent.state not in [
                    TorrentState.PAUSED,
                ]:
                    continue
                elif filter == "active" and torrent.download_speed == 0:
                    continue
                elif filter == "seeding" and torrent.state not in [
                    TorrentState.SEEDING,
                    TorrentState.UPLOADING,
                ]:
                    continue

            result.append({
                "hash": torrent.hash,
                "name": torrent.name,
                "size": torrent.size,
                "progress": torrent.progress,
                "dlspeed": torrent.download_speed,
                "upspeed": torrent.upload_speed,
                "priority": 0,
                "num_seeds": torrent.seeders,
                "num_complete": torrent.seeders,
                "num_leechs": torrent.leechers,
                "num_incomplete": torrent.leechers,
                "ratio": torrent.ratio,
                "eta": torrent.eta,
                "state": state_to_qbittorrent(torrent.state),
                "seq_dl": False,
                "f_l_piece_prio": False,
                "category": torrent.category,
                "tags": torrent.tags,
                "super_seeding": False,
                "force_start": False,
                "save_path": torrent.save_path,
                "content_path": torrent.content_path or os.path.join(
                    torrent.save_path, torrent.name
                ),
                "added_on": torrent.added_on,
                "completion_on": torrent.completion_on,
                "tracker": "",
                "dl_limit": -1,
                "up_limit": -1,
                "downloaded": torrent.downloaded,
                "uploaded": torrent.uploaded,
                "downloaded_session": torrent.downloaded,
                "uploaded_session": torrent.uploaded,
                "amount_left": int(torrent.size * (1 - torrent.progress)),
                "completed": torrent.completed,
                "max_ratio": -1,
                "max_seeding_time": -1,
                "ratio_limit": -1,
                "seeding_time_limit": -1,
                "seen_complete": torrent.completion_on,
                "auto_tmm": False,
                "time_active": 0,
                "availability": 1.0,
                "total_size": torrent.size,
                "magnet_uri": "",
                "infohash_v1": torrent.hash.lower(),
                "infohash_v2": "",
            })

        if sort:
            reverse_sort = reverse
            result.sort(key=lambda x: x.get(sort, 0), reverse=reverse_sort)

        if limit:
            result = result[offset : offset + limit]

        return JSONResponse(result)

    except Exception as e:
        logger.error(f"Error getting torrents: {e}")
        raise HTTPException(status_code=500, detail=sanitize_error_message(e)) from e


@app.get("/api/v2/torrents/properties")
async def torrents_properties(request: Request, hash: str):
    """Get properties of a specific torrent."""
    if not validate_session(request):
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        torrent = await seedr_client.get_torrent(hash.upper())
        if not torrent:
            raise HTTPException(status_code=404, detail="Torrent not found")

        return JSONResponse({
            "hash": torrent.hash,
            "name": torrent.name,
            "save_path": torrent.save_path,
            "content_path": torrent.content_path or os.path.join(
                torrent.save_path, torrent.name
            ),
            "creation_date": torrent.added_on,
            "addition_date": torrent.added_on,
            "completion_date": torrent.completion_on,
            "comment": "",
            "created_by": "",
            "dl_limit": -1,
            "dl_speed": torrent.download_speed,
            "dl_speed_avg": torrent.download_speed,
            "eta": torrent.eta,
            "last_seen": int(datetime.now().timestamp()),
            "nb_connections": 0,
            "nb_connections_limit": 100,
            "peers": 0,
            "peers_total": 0,
            "piece_size": 0,
            "pieces_have": 0,
            "pieces_num": 0,
            "reannounce": 0,
            "seeds": torrent.seeders,
            "seeds_total": torrent.seeders,
            "seeding_time": 0,
            "share_ratio": torrent.ratio,
            "time_active": 0,
            "total_downloaded": torrent.downloaded,
            "total_downloaded_session": torrent.downloaded,
            "total_size": torrent.size,
            "total_uploaded": torrent.uploaded,
            "total_uploaded_session": torrent.uploaded,
            "total_wasted": 0,
            "up_limit": -1,
            "up_speed": torrent.upload_speed,
            "up_speed_avg": torrent.upload_speed,
        })

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting torrent properties: {e}")
        raise HTTPException(status_code=500, detail=sanitize_error_message(e)) from e


@app.get("/api/v2/torrents/files")
async def torrents_files(request: Request, hash: str):
    """Get files list for a torrent."""
    if not validate_session(request):
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        files = await seedr_client.get_torrent_files(hash.upper())
        return JSONResponse(files)
    except Exception as e:
        logger.error(f"Error getting torrent files: {e}")
        raise HTTPException(status_code=500, detail=sanitize_error_message(e)) from e


@app.post("/api/v2/torrents/add")
async def torrents_add(
    request: Request,
    urls: Optional[str] = Form(None),
    torrents: Optional[UploadFile] = File(None),
    category: Optional[str] = Form(""),
    tags: Optional[str] = Form(""),
    savepath: Optional[str] = Form(None),
    cookie: Optional[str] = Form(None),
    rename: Optional[str] = Form(None),
    paused: Optional[str] = Form("false"),
    stopCondition: Optional[str] = Form(None),
    contentLayout: Optional[str] = Form(None),
    dlLimit: Optional[int] = Form(None),
    upLimit: Optional[int] = Form(None),
    ratioLimit: Optional[float] = Form(None),
    seedingTimeLimit: Optional[int] = Form(None),
    autoTMM: Optional[str] = Form(None),
    sequentialDownload: Optional[str] = Form(None),
    firstLastPiecePrio: Optional[str] = Form(None),
    addToTopOfQueue: Optional[str] = Form(None),
):
    """Add new torrent(s) with instance tracking."""
    if not validate_session(request):
        raise HTTPException(status_code=403, detail="Forbidden")

    # Log all incoming parameters for debugging
    logger.info(
        f"torrents/add request: category='{category}', savepath='{savepath}', "
        f"tags='{tags}', has_urls={urls is not None}, has_torrents={torrents is not None}"
    )

    try:
        added_hashes = []
        instance_id = extract_instance_id(category or "")

        if urls:
            for url in urls.split("\n"):
                url = url.strip()
                if not url:
                    continue

                logger.info(f"Adding torrent from {instance_id}: {url[:50]}...")
                torrent_hash = await seedr_client.add_torrent(
                    magnet_link=url,
                    category=category or "",
                )
                if torrent_hash:
                    added_hashes.append(torrent_hash)

        if torrents:
            content = await torrents.read()
            if content:
                logger.info(f"Adding torrent file from {instance_id}: {torrents.filename}")
                torrent_hash = await seedr_client.add_torrent(
                    torrent_file=content,
                    category=category or "",
                )
                if torrent_hash:
                    added_hashes.append(torrent_hash)

        if added_hashes:
            logger.info(f"Successfully added {len(added_hashes)} torrent(s) for {instance_id}")
            return PlainTextResponse("Ok.")
        else:
            raise HTTPException(status_code=400, detail="No torrents added")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error adding torrent: {e}")
        raise HTTPException(status_code=500, detail=sanitize_error_message(e)) from e


@app.post("/api/v2/torrents/delete")
async def torrents_delete(
    request: Request,
    hashes: str = Form(...),
    deleteFiles: str = Form("false"),
):
    """Delete torrent(s)."""
    if not validate_session(request):
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        delete_files = deleteFiles.lower() == "true"
        hash_list = [h.strip().upper() for h in hashes.split("|") if h.strip()]

        for torrent_hash in hash_list:
            if torrent_hash.lower() == "all":
                torrents = await seedr_client.get_torrents()
                for t in torrents:
                    await seedr_client.delete_torrent(t.hash, delete_files)
            else:
                await seedr_client.delete_torrent(torrent_hash, delete_files)

        return PlainTextResponse("Ok.")

    except Exception as e:
        logger.error(f"Error deleting torrents: {e}")
        raise HTTPException(status_code=500, detail=sanitize_error_message(e)) from e


@app.post("/api/v2/torrents/pause")
async def torrents_pause(request: Request, hashes: str = Form(...)):
    """Pause torrent(s) - No-op for Seedr."""
    if not validate_session(request):
        raise HTTPException(status_code=403, detail="Forbidden")
    logger.debug(f"Pause requested for: {hashes} (no-op)")
    return PlainTextResponse("Ok.")


@app.post("/api/v2/torrents/resume")
async def torrents_resume(request: Request, hashes: str = Form(...)):
    """Resume torrent(s) - No-op for Seedr."""
    if not validate_session(request):
        raise HTTPException(status_code=403, detail="Forbidden")
    logger.debug(f"Resume requested for: {hashes} (no-op)")
    return PlainTextResponse("Ok.")


@app.post("/api/v2/torrents/setCategory")
async def torrents_set_category(
    request: Request, hashes: str = Form(...), category: str = Form("")
):
    """Set category for torrent(s)."""
    if not validate_session(request):
        raise HTTPException(status_code=403, detail="Forbidden")

    instance_id = extract_instance_id(category)

    for h in hashes.split("|"):
        h = h.strip().upper()
        if h:
            await seedr_client.set_category_mapping(h, category, instance_id)

    return PlainTextResponse("Ok.")


@app.post("/api/v2/torrents/setForceStart")
async def torrents_set_force_start(
    request: Request, hashes: str = Form(...), value: str = Form("true")
):
    """Set force start - No-op for Seedr."""
    if not validate_session(request):
        raise HTTPException(status_code=403, detail="Forbidden")
    return PlainTextResponse("Ok.")


@app.post("/api/v2/torrents/setSuperSeeding")
async def torrents_set_super_seeding(
    request: Request, hashes: str = Form(...), value: str = Form("true")
):
    """Set super seeding - No-op for Seedr."""
    if not validate_session(request):
        raise HTTPException(status_code=403, detail="Forbidden")
    return PlainTextResponse("Ok.")


@app.post("/api/v2/torrents/recheck")
async def torrents_recheck(request: Request, hashes: str = Form(...)):
    """Recheck torrent(s) - No-op for Seedr."""
    if not validate_session(request):
        raise HTTPException(status_code=403, detail="Forbidden")
    return PlainTextResponse("Ok.")


@app.post("/api/v2/torrents/reannounce")
async def torrents_reannounce(request: Request, hashes: str = Form(...)):
    """Reannounce torrent(s) - No-op for Seedr."""
    if not validate_session(request):
        raise HTTPException(status_code=403, detail="Forbidden")
    return PlainTextResponse("Ok.")


@app.post("/api/v2/torrents/rename")
async def torrents_rename(
    request: Request, hash: str = Form(...), name: str = Form(...)
):
    """Rename torrent - Not implemented."""
    if not validate_session(request):
        raise HTTPException(status_code=403, detail="Forbidden")
    return PlainTextResponse("Ok.")


@app.post("/api/v2/torrents/setLocation")
async def torrents_set_location(
    request: Request, hashes: str = Form(...), location: str = Form(...)
):
    """Set save location - No-op for Seedr."""
    if not validate_session(request):
        raise HTTPException(status_code=403, detail="Forbidden")
    return PlainTextResponse("Ok.")


@app.post("/api/v2/torrents/topPrio")
@app.post("/api/v2/torrents/bottomPrio")
@app.post("/api/v2/torrents/increasePrio")
@app.post("/api/v2/torrents/decreasePrio")
async def torrents_priority(request: Request, hashes: str = Form(...)):
    """Set priority - No-op for Seedr."""
    if not validate_session(request):
        raise HTTPException(status_code=403, detail="Forbidden")
    return PlainTextResponse("Ok.")


# =============================================================================
# Categories Endpoints
# =============================================================================


@app.get("/api/v2/torrents/categories")
async def torrents_categories(request: Request):
    """Get all categories with instance tracking."""
    if not validate_session(request):
        raise HTTPException(status_code=403, detail="Forbidden")

    result = {}

    for cat_name, cat_info in created_categories.items():
        result[cat_name] = {
            "name": cat_name,
            "savePath": cat_info.get("savePath", os.path.join(settings.download_path, cat_name)),
        }

    if seedr_client:
        for cat in set(seedr_client._category_mapping.values()):
            if cat and cat not in result:
                result[cat] = {
                    "name": cat,
                    "savePath": os.path.join(settings.download_path, cat),
                }

    return JSONResponse(result)


@app.post("/api/v2/torrents/createCategory")
async def torrents_create_category(
    request: Request, category: str = Form(...), savePath: str = Form("")
):
    """Create a category with instance tracking."""
    if not validate_session(request):
        raise HTTPException(status_code=403, detail="Forbidden")

    instance_id = extract_instance_id(category)
    save_path = savePath if savePath else os.path.join(settings.download_path, category)

    created_categories[category] = {
        "savePath": save_path,
        "instance_id": instance_id,
    }

    # Persist category
    if state_manager:
        await state_manager.add_category(CategoryState(
            name=category,
            save_path=save_path,
            instance_id=instance_id,
        ))

    try:
        os.makedirs(save_path, exist_ok=True)
        os.chmod(save_path, 0o777)
        logger.info(f"Created category: {category} -> {save_path} (instance: {instance_id})")
    except OSError as e:
        logger.warning(f"Could not create category directory {save_path}: {e}")

    return PlainTextResponse("Ok.")


@app.post("/api/v2/torrents/removeCategories")
async def torrents_remove_categories(
    request: Request, categories: str = Form(...)
):
    """Remove categories."""
    if not validate_session(request):
        raise HTTPException(status_code=403, detail="Forbidden")

    for cat in categories.split("\n"):
        cat = cat.strip()
        if cat and cat in created_categories:
            del created_categories[cat]
            if state_manager:
                await state_manager.delete_category(cat)
            logger.info(f"Removed category: {cat}")

    return PlainTextResponse("Ok.")


@app.post("/api/v2/torrents/editCategory")
async def torrents_edit_category(
    request: Request, category: str = Form(...), savePath: str = Form("")
):
    """Edit a category."""
    if not validate_session(request):
        raise HTTPException(status_code=403, detail="Forbidden")

    instance_id = extract_instance_id(category)
    save_path = savePath if savePath else os.path.join(settings.download_path, category)

    created_categories[category] = {
        "savePath": save_path,
        "instance_id": instance_id,
    }

    if state_manager:
        await state_manager.add_category(CategoryState(
            name=category,
            save_path=save_path,
            instance_id=instance_id,
        ))

    try:
        os.makedirs(save_path, exist_ok=True)
        os.chmod(save_path, 0o777)
        logger.info(f"Edited category: {category} -> {save_path}")
    except OSError as e:
        logger.warning(f"Could not create category directory {save_path}: {e}")

    return PlainTextResponse("Ok.")


# =============================================================================
# Tags Endpoints
# =============================================================================


@app.get("/api/v2/torrents/tags")
async def torrents_tags(request: Request):
    """Get all tags."""
    if not validate_session(request):
        raise HTTPException(status_code=403, detail="Forbidden")
    return JSONResponse([])


@app.post("/api/v2/torrents/addTags")
async def torrents_add_tags(
    request: Request, hashes: str = Form(...), tags: str = Form("")
):
    """Add tags - Not fully implemented."""
    if not validate_session(request):
        raise HTTPException(status_code=403, detail="Forbidden")
    return PlainTextResponse("Ok.")


@app.post("/api/v2/torrents/removeTags")
async def torrents_remove_tags(
    request: Request, hashes: str = Form(...), tags: str = Form("")
):
    """Remove tags - Not fully implemented."""
    if not validate_session(request):
        raise HTTPException(status_code=403, detail="Forbidden")
    return PlainTextResponse("Ok.")


@app.post("/api/v2/torrents/createTags")
async def torrents_create_tags(request: Request, tags: str = Form(...)):
    """Create tags - Not fully implemented."""
    if not validate_session(request):
        raise HTTPException(status_code=403, detail="Forbidden")
    return PlainTextResponse("Ok.")


@app.post("/api/v2/torrents/deleteTags")
async def torrents_delete_tags(request: Request, tags: str = Form(...)):
    """Delete tags - Not fully implemented."""
    if not validate_session(request):
        raise HTTPException(status_code=403, detail="Forbidden")
    return PlainTextResponse("Ok.")


# =============================================================================
# Sync Endpoint
# =============================================================================


@app.get("/api/v2/sync/maindata")
async def sync_maindata(request: Request, rid: int = 0):
    """Get sync data with enhanced phase and instance info."""
    if not validate_session(request):
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        torrents = await seedr_client.get_torrents()

        torrents_dict = {}
        for torrent in torrents:
            torrents_dict[torrent.hash] = {
                "hash": torrent.hash,
                "name": torrent.name,
                "size": torrent.size,
                "progress": torrent.progress,
                "dlspeed": torrent.download_speed,
                "upspeed": torrent.upload_speed,
                "priority": 0,
                "num_seeds": torrent.seeders,
                "num_leechs": torrent.leechers,
                "ratio": torrent.ratio,
                "eta": torrent.eta,
                "state": state_to_qbittorrent(torrent.state),
                "category": torrent.category,
                "tags": torrent.tags,
                "save_path": torrent.save_path,
                "content_path": torrent.content_path or os.path.join(
                    torrent.save_path, torrent.name
                ),
                "added_on": torrent.added_on,
                "completion_on": torrent.completion_on,
                "downloaded": torrent.downloaded,
                "uploaded": torrent.uploaded,
                "completed": torrent.completed,
                "amount_left": int(torrent.size * (1 - torrent.progress)),
                "total_size": torrent.size,
            }

        categories = {}
        for cat_name, cat_info in created_categories.items():
            categories[cat_name] = {
                "name": cat_name,
                "savePath": cat_info.get("savePath", os.path.join(settings.download_path, cat_name)),
            }

        for cat in set(seedr_client._category_mapping.values()):
            if cat and cat not in categories:
                categories[cat] = {
                    "name": cat,
                    "savePath": os.path.join(settings.download_path, cat),
                }

        return JSONResponse({
            "rid": rid + 1,
            "full_update": True,
            "torrents": torrents_dict,
            "torrents_removed": [],
            "categories": categories,
            "categories_removed": [],
            "tags": [],
            "tags_removed": [],
            "server_state": {
                "dl_info_speed": sum(t.download_speed for t in torrents),
                "dl_info_data": sum(t.downloaded for t in torrents),
                "up_info_speed": sum(t.upload_speed for t in torrents),
                "up_info_data": sum(t.uploaded for t in torrents),
                "dl_rate_limit": 0,
                "up_rate_limit": 0,
                "dht_nodes": 0,
                "connection_status": "connected",
                "queueing": True,
                "use_alt_speed_limits": False,
                "refresh_interval": 1500,
                "free_space_on_disk": 1000000000000,
            },
        })

    except Exception as e:
        logger.error(f"Error in sync maindata: {e}")
        raise HTTPException(status_code=500, detail=sanitize_error_message(e)) from e


# =============================================================================
# Health Check Endpoint
# =============================================================================


@app.get("/health")
@app.get("/api/health")
async def health_check():
    """Health check endpoint with enhanced status."""
    try:
        if seedr_client:
            success, message = await seedr_client.test_connection()

            stats = seedr_client.get_stats()

            return JSONResponse({
                "status": "healthy" if success else "unhealthy",
                "seedr_connected": success,
                "message": message,
                "auto_download": settings.auto_download,
                "delete_after_download": settings.delete_after_download,
                "persist_state": settings.persist_state,
                "active_downloads": stats.get("active_downloads", 0),
                "completed_downloads": stats.get("local_downloads", 0),
                "queue_size": stats.get("queue_size", 0),
                "circuit_breaker": stats.get("circuit_breaker", {}).get("state", "unknown"),
            })
    except Exception as e:
        return JSONResponse({
            "status": "unhealthy",
            "seedr_connected": False,
            "message": str(e),
        }, status_code=500)

    return JSONResponse({
        "status": "unhealthy",
        "seedr_connected": False,
        "message": "Client not initialized",
    }, status_code=500)


# =============================================================================
# Download Management Endpoints
# =============================================================================


@app.get("/api/seedr/downloads")
async def get_download_status(request: Request):
    """Get status of all downloads with enhanced phase tracking."""
    if not validate_session(request):
        raise HTTPException(status_code=403, detail="Forbidden")

    if not seedr_client:
        raise HTTPException(status_code=500, detail="Client not initialized")

    downloads = []

    for torrent_hash, torrent in seedr_client._torrents_cache.items():
        if torrent_hash.startswith("QUEUE") and torrent_hash in seedr_client._hash_mapping:
            continue

        is_downloading = torrent_hash in seedr_client._download_tasks
        is_completed = torrent_hash in seedr_client._local_downloads
        local_progress = seedr_client._download_progress.get(torrent_hash, 0.0)
        speed = seedr_client._active_downloads.get(torrent_hash, 0)

        if torrent.is_queued:
            seedr_progress = 0.0
        elif torrent.state in [TorrentState.COMPLETED, TorrentState.DOWNLOADING_LOCAL, TorrentState.COMPLETED_LOCAL]:
            seedr_progress = 1.0
        else:
            seedr_progress = min(torrent.progress * 2, 1.0)

        downloads.append({
            "hash": torrent_hash,
            "name": torrent.name,
            "size": torrent.size,
            "phase": torrent.phase.value,
            "phase_description": phase_to_description(torrent.phase),
            "instance_id": torrent.instance_id,
            "seedr_progress": seedr_progress,
            "local_progress": local_progress,
            "combined_progress": torrent.progress,
            "local_speed": speed,
            "is_downloading_locally": is_downloading,
            "is_completed_locally": is_completed,
            "save_path": torrent.save_path,
            "content_path": torrent.content_path,
            "category": torrent.category,
            "error_count": torrent.error_count,
            "last_error": torrent.last_error,
        })

    return JSONResponse({
        "auto_download_enabled": settings.auto_download,
        "delete_after_download": settings.delete_after_download,
        "persist_state": settings.persist_state,
        "downloads": downloads,
    })


@app.post("/api/seedr/downloads/start")
async def start_download(request: Request, hashes: str = Form(...)):
    """Force start downloading specific torrents."""
    if not validate_session(request):
        raise HTTPException(status_code=403, detail="Forbidden")

    if not seedr_client:
        raise HTTPException(status_code=500, detail="Client not initialized")

    hash_list = [h.strip().upper() for h in hashes.split("|") if h.strip()]

    started = []
    failed = []

    for torrent_hash in hash_list:
        success = await seedr_client.force_download(torrent_hash)
        if success:
            started.append(torrent_hash)
        else:
            failed.append(torrent_hash)

    return JSONResponse({
        "started": started,
        "failed": failed,
    })


@app.post("/api/seedr/downloads/stop")
async def stop_download(request: Request, hashes: str = Form(...)):
    """Stop downloading specific torrents."""
    if not validate_session(request):
        raise HTTPException(status_code=403, detail="Forbidden")

    if not seedr_client:
        raise HTTPException(status_code=500, detail="Client not initialized")

    hash_list = [h.strip().upper() for h in hashes.split("|") if h.strip()]

    stopped = []

    for torrent_hash in hash_list:
        if torrent_hash in seedr_client._download_tasks:
            seedr_client._download_tasks[torrent_hash].cancel()
            stopped.append(torrent_hash)

    return JSONResponse({
        "stopped": stopped,
    })


@app.get("/api/seedr/settings")
async def get_seedr_settings(request: Request):
    """Get Seedr account settings and usage."""
    if not validate_session(request):
        raise HTTPException(status_code=403, detail="Forbidden")

    if not seedr_client:
        raise HTTPException(status_code=500, detail="Client not initialized")

    try:
        seedr_settings = await seedr_client.get_settings()
        return JSONResponse({
            "seedr": seedr_settings,
            "proxy": {
                "auto_download": settings.auto_download,
                "delete_after_download": settings.delete_after_download,
                "download_path": settings.download_path,
                "persist_state": settings.persist_state,
                "state_file": settings.state_file,
            }
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=sanitize_error_message(e)) from e


# =============================================================================
# Queue Management Endpoints
# =============================================================================


@app.get("/api/seedr/queue")
async def get_queue(request: Request):
    """Get the queue of torrents with instance info."""
    if not validate_session(request):
        raise HTTPException(status_code=403, detail="Forbidden")

    if not seedr_client:
        raise HTTPException(status_code=500, detail="Client not initialized")

    queue_info = seedr_client.get_queue_info()
    available_storage = seedr_client.get_available_storage()

    return JSONResponse({
        "queue_size": len(queue_info),
        "available_storage_mb": available_storage / 1024 / 1024,
        "storage_used_mb": seedr_client._storage_used / 1024 / 1024,
        "storage_max_mb": seedr_client._storage_max / 1024 / 1024,
        "queue": queue_info,
    })


@app.post("/api/seedr/queue/clear")
async def clear_queue(request: Request):
    """Clear all queued torrents."""
    if not validate_session(request):
        raise HTTPException(status_code=403, detail="Forbidden")

    if not seedr_client:
        raise HTTPException(status_code=500, detail="Client not initialized")

    count = await seedr_client.clear_queue()

    return JSONResponse({
        "cleared": count,
        "message": f"Cleared {count} torrents from queue",
    })


@app.post("/api/seedr/queue/process")
async def process_queue(request: Request):
    """Force process the queue."""
    if not validate_session(request):
        raise HTTPException(status_code=403, detail="Forbidden")

    if not seedr_client:
        raise HTTPException(status_code=500, detail="Client not initialized")

    initial_size = len(seedr_client._torrent_queue)
    await seedr_client._process_queue()
    final_size = len(seedr_client._torrent_queue)

    return JSONResponse({
        "processed": initial_size - final_size,
        "remaining": final_size,
        "message": f"Processed {initial_size - final_size} torrents, {final_size} remaining in queue",
    })


@app.get("/api/seedr/storage")
async def get_storage(request: Request):
    """Get Seedr storage information."""
    if not validate_session(request):
        raise HTTPException(status_code=403, detail="Forbidden")

    if not seedr_client:
        raise HTTPException(status_code=500, detail="Client not initialized")

    await seedr_client._update_storage_info()

    return JSONResponse({
        "used_bytes": seedr_client._storage_used,
        "max_bytes": seedr_client._storage_max,
        "available_bytes": seedr_client.get_available_storage(),
        "used_mb": seedr_client._storage_used / 1024 / 1024,
        "max_mb": seedr_client._storage_max / 1024 / 1024,
        "available_mb": seedr_client.get_available_storage() / 1024 / 1024,
        "used_percent": (seedr_client._storage_used / seedr_client._storage_max * 100) if seedr_client._storage_max > 0 else 0,
        "buffer_mb": seedr_client.storage_buffer_mb,
    })


# =============================================================================
# Activity Log Endpoint
# =============================================================================


@app.get("/api/seedr/logs")
async def get_activity_logs(
    request: Request,
    limit: int = 100,
    level: Optional[str] = None,
    torrent_hash: Optional[str] = None,
    instance_id: Optional[str] = None,
):
    """Get activity logs for debugging and monitoring."""
    if not validate_session(request):
        raise HTTPException(status_code=403, detail="Forbidden")

    if not activity_log_handler:
        return JSONResponse({"count": 0, "logs": []})

    logs = activity_log_handler.get_logs(
        limit=limit,
        level=level,
        torrent_hash=torrent_hash,
        instance_id=instance_id,
    )

    return JSONResponse({
        "count": len(logs),
        "logs": logs,
    })


# =============================================================================
# State/Stats Endpoint
# =============================================================================


@app.get("/api/seedr/state")
async def get_state(request: Request):
    """Get current state summary and statistics."""
    if not validate_session(request):
        raise HTTPException(status_code=403, detail="Forbidden")

    if not seedr_client:
        raise HTTPException(status_code=500, detail="Client not initialized")

    client_stats = seedr_client.get_stats()

    state_stats = {}
    if state_manager:
        state_stats = await state_manager.get_stats()

    return JSONResponse({
        "client": client_stats,
        "state_manager": state_stats,
        "persistence_enabled": settings.persist_state,
        "state_file": settings.state_file if settings.persist_state else None,
    })


# =============================================================================
# Main entry point
# =============================================================================


def main():
    """Run the server."""
    import uvicorn
    uvicorn.run(
        "seedr_sonarr.server:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
