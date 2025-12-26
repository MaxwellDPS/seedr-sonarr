"""
qBittorrent API Emulation Layer
Implements the qBittorrent Web API that Sonarr/Radarr expects.
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

    # Logging
    log_level: str = "INFO"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


# Global instances
settings = Settings()
seedr_client: Optional[SeedrClientWrapper] = None
sessions: dict[str, datetime] = {}
SESSION_TIMEOUT = timedelta(hours=24)
# Store created categories: name -> savePath
created_categories: dict[str, str] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    global seedr_client

    # Startup
    logging.basicConfig(level=getattr(logging, settings.log_level))
    logger.info("Starting Seedr-Sonarr proxy...")

    seedr_client = SeedrClientWrapper(
        email=settings.seedr_email,
        password=settings.seedr_password,
        token=settings.seedr_token,
        download_path=settings.download_path,
    )

    try:
        await seedr_client.initialize()
        logger.info("Seedr client connected successfully")
    except Exception as e:
        logger.error(f"Failed to initialize Seedr client: {e}")

    yield

    # Shutdown
    if seedr_client:
        await seedr_client.close()
    logger.info("Seedr-Sonarr proxy stopped")


app = FastAPI(
    title="Seedr-Sonarr Proxy",
    description="qBittorrent API emulation for Seedr.cc",
    version="1.0.0",
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

    # Extend session
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
    """
    Authenticate and create a session.
    qBittorrent API: POST /api/v2/auth/login
    """
    # Handle both form data and query params
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
    """
    Logout and invalidate session.
    qBittorrent API: POST /api/v2/auth/logout
    """
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
        raise HTTPException(status_code=500, detail=str(e))


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
    """
    Get info about torrents.
    This is the main endpoint Sonarr uses to monitor downloads.
    """
    if not validate_session(request):
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        torrents = await seedr_client.get_torrents()
        result = []

        # Parse hashes filter
        hash_filter = set()
        if hashes:
            hash_filter = set(h.upper() for h in hashes.split("|"))

        for torrent in torrents:
            # Apply filters
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

            # Format for qBittorrent API
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

        # Apply sorting
        if sort:
            reverse_sort = reverse
            result.sort(key=lambda x: x.get(sort, 0), reverse=reverse_sort)

        # Apply pagination
        if limit:
            result = result[offset : offset + limit]

        return JSONResponse(result)

    except Exception as e:
        logger.error(f"Error getting torrents: {e}")
        raise HTTPException(status_code=500, detail=str(e))


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
        raise HTTPException(status_code=500, detail=str(e))


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
        raise HTTPException(status_code=500, detail=str(e))


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
    """
    Add new torrent(s).
    This is the endpoint Sonarr uses to send downloads.
    """
    if not validate_session(request):
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        added_hashes = []

        # Handle magnet links / URLs
        if urls:
            for url in urls.split("\n"):
                url = url.strip()
                if not url:
                    continue
                
                logger.info(f"Adding torrent: {url[:50]}...")
                torrent_hash = await seedr_client.add_torrent(
                    magnet_link=url,
                    category=category or "",
                )
                if torrent_hash:
                    added_hashes.append(torrent_hash)

        # Handle torrent file upload
        if torrents:
            content = await torrents.read()
            if content:
                logger.info(f"Adding torrent file: {torrents.filename}")
                torrent_hash = await seedr_client.add_torrent(
                    torrent_file=content,
                    category=category or "",
                )
                if torrent_hash:
                    added_hashes.append(torrent_hash)

        if added_hashes:
            logger.info(f"Successfully added {len(added_hashes)} torrent(s)")
            return PlainTextResponse("Ok.")
        else:
            raise HTTPException(status_code=400, detail="No torrents added")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error adding torrent: {e}")
        raise HTTPException(status_code=500, detail=str(e))


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
                # Delete all torrents
                torrents = await seedr_client.get_torrents()
                for t in torrents:
                    await seedr_client.delete_torrent(t.hash, delete_files)
            else:
                await seedr_client.delete_torrent(torrent_hash, delete_files)

        return PlainTextResponse("Ok.")

    except Exception as e:
        logger.error(f"Error deleting torrents: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/v2/torrents/pause")
async def torrents_pause(request: Request, hashes: str = Form(...)):
    """Pause torrent(s) - No-op for Seedr."""
    if not validate_session(request):
        raise HTTPException(status_code=403, detail="Forbidden")
    # Seedr doesn't support pausing
    logger.debug(f"Pause requested for: {hashes} (no-op)")
    return PlainTextResponse("Ok.")


@app.post("/api/v2/torrents/resume")
async def torrents_resume(request: Request, hashes: str = Form(...)):
    """Resume torrent(s) - No-op for Seedr."""
    if not validate_session(request):
        raise HTTPException(status_code=403, detail="Forbidden")
    # Seedr doesn't support pausing/resuming
    logger.debug(f"Resume requested for: {hashes} (no-op)")
    return PlainTextResponse("Ok.")


@app.post("/api/v2/torrents/setCategory")
async def torrents_set_category(
    request: Request, hashes: str = Form(...), category: str = Form("")
):
    """Set category for torrent(s)."""
    if not validate_session(request):
        raise HTTPException(status_code=403, detail="Forbidden")
    
    for h in hashes.split("|"):
        h = h.strip().upper()
        if h:
            seedr_client._category_mapping[h] = category
    
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
    # Could implement this via Seedr rename API
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
    """Get all categories."""
    if not validate_session(request):
        raise HTTPException(status_code=403, detail="Forbidden")
    
    result = {}
    
    # Add explicitly created categories
    for cat_name, save_path in created_categories.items():
        result[cat_name] = {
            "name": cat_name,
            "savePath": save_path,
        }
    
    # Also add categories from torrents
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
    """Create a category."""
    if not validate_session(request):
        raise HTTPException(status_code=403, detail="Forbidden")
    
    # Store the category
    save_path = savePath if savePath else os.path.join(settings.download_path, category)
    created_categories[category] = save_path
    logger.info(f"Created category: {category} -> {save_path}")
    
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
            logger.info(f"Removed category: {cat}")
    
    return PlainTextResponse("Ok.")


@app.post("/api/v2/torrents/editCategory")
async def torrents_edit_category(
    request: Request, category: str = Form(...), savePath: str = Form("")
):
    """Edit a category."""
    if not validate_session(request):
        raise HTTPException(status_code=403, detail="Forbidden")
    
    save_path = savePath if savePath else os.path.join(settings.download_path, category)
    created_categories[category] = save_path
    logger.info(f"Edited category: {category} -> {save_path}")
    
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
# Sync Endpoint (Used by Sonarr for efficient updates)
# =============================================================================


@app.get("/api/v2/sync/maindata")
async def sync_maindata(request: Request, rid: int = 0):
    """
    Get sync data - this is used by Sonarr for efficient polling.
    Returns all data on first call, then only changes on subsequent calls.
    """
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

        # Get categories
        categories = {}
        
        # Add explicitly created categories
        for cat_name, save_path in created_categories.items():
            categories[cat_name] = {
                "name": cat_name,
                "savePath": save_path,
            }
        
        # Also add categories from torrents
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
                "free_space_on_disk": 1000000000000,  # 1TB
            },
        })

    except Exception as e:
        logger.error(f"Error in sync maindata: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Health Check Endpoint
# =============================================================================


@app.get("/health")
@app.get("/api/health")
async def health_check():
    """Health check endpoint."""
    try:
        if seedr_client:
            success, message = await seedr_client.test_connection()
            return JSONResponse({
                "status": "healthy" if success else "unhealthy",
                "seedr_connected": success,
                "message": message,
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