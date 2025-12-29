"""
QNAP Download Station Client
Provides interface to QNAP Download Station API V4 for downloading files from Seedr.
"""

import asyncio
import base64
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Callable
from urllib.parse import urljoin

import aiohttp

logger = logging.getLogger(__name__)


class QnapTaskStatus(Enum):
    """QNAP Download Station task status codes."""
    WAITING = 1
    DOWNLOADING = 2
    PAUSED = 3
    COMPLETED = 5
    ERROR = 6
    SEEDING = 100
    CHECKING = 101


@dataclass
class QnapTask:
    """Represents a download task in QNAP Download Station."""
    id: str
    name: str
    source_url: str
    size: int
    downloaded: int
    progress: float
    status: QnapTaskStatus
    download_speed: int = 0
    error_message: Optional[str] = None
    destination: str = ""


class QnapDownloadStationClient:
    """
    Client for QNAP Download Station API V4.

    API Documentation: https://download.qnap.com/dev/download-station-addon-developers-guide_v4.pdf
    API Base Path: /downloadstation/V4/{App}/{Endpoint}
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        port: int = 8080,
        use_https: bool = False,
        verify_ssl: bool = True,
    ):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.use_https = use_https
        self.verify_ssl = verify_ssl

        self._session: Optional[aiohttp.ClientSession] = None
        self._sid: Optional[str] = None
        self._lock = asyncio.Lock()

        protocol = "https" if use_https else "http"
        self._base_url = f"{protocol}://{host}:{port}/downloadstation/V4"

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(ssl=self.verify_ssl if self.use_https else None)
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def _request(
        self,
        app: str,
        endpoint: str,
        params: Optional[dict] = None,
        require_auth: bool = True,
    ) -> dict:
        """Make a request to the QNAP Download Station API."""
        if require_auth and not self._sid:
            await self.login()

        session = await self._get_session()
        url = f"{self._base_url}/{app}/{endpoint}"

        data = params.copy() if params else {}
        if self._sid and require_auth:
            data["sid"] = self._sid

        try:
            async with session.post(url, data=data) as response:
                if response.status != 200:
                    raise Exception(f"HTTP {response.status}: {response.reason}")

                result = await response.json()

                # Check for API errors
                error_code = result.get("error", 0)
                if error_code > 0:
                    error_msg = result.get("reason", f"Error code: {error_code}")

                    # Handle session expiry
                    if error_code in [2, 4]:  # Common session error codes
                        self._sid = None
                        if require_auth:
                            await self.login()
                            return await self._request(app, endpoint, params, require_auth)

                    raise Exception(f"QNAP API error: {error_msg}")

                return result

        except aiohttp.ClientError as e:
            logger.error(f"QNAP request failed: {e}")
            raise

    async def login(self) -> bool:
        """Authenticate with QNAP Download Station."""
        async with self._lock:
            if self._sid:
                return True

            try:
                # Password must be base64 encoded
                encoded_password = base64.b64encode(self.password.encode()).decode()

                result = await self._request(
                    app="Misc",
                    endpoint="Login",
                    params={
                        "user": self.username,
                        "pass": encoded_password,
                    },
                    require_auth=False,
                )

                self._sid = result.get("sid")
                if not self._sid:
                    raise Exception("No session ID returned from login")

                logger.info(f"QNAP Download Station authenticated as {self.username}")
                return True

            except Exception as e:
                logger.error(f"QNAP login failed: {e}")
                raise

    async def logout(self):
        """Logout from QNAP Download Station."""
        if self._sid:
            try:
                await self._request("Misc", "Logout")
            except Exception as e:
                logger.warning(f"QNAP logout error: {e}")
            finally:
                self._sid = None

    async def close(self):
        """Close the client connection."""
        await self.logout()
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def add_download(
        self,
        url: str,
        temp_folder: str = "Download",
        dest_folder: str = "",
    ) -> Optional[str]:
        """
        Add a download task by URL.

        Args:
            url: The URL to download (HTTP/HTTPS/FTP/Magnet)
            temp_folder: Temporary folder for in-progress downloads (relative to share)
            dest_folder: Final destination folder after completion (relative to share)

        Returns:
            Task ID if successful, None otherwise
        """
        try:
            params = {
                "url": url,
                "temp": temp_folder,
            }
            if dest_folder:
                params["move"] = dest_folder

            result = await self._request("Task", "AddUrl", params)

            # The API may return task info or just success
            task_id = result.get("id") or result.get("task_id")

            logger.info(f"Added QNAP download task: {url[:50]}... -> {dest_folder or temp_folder}")
            return task_id

        except Exception as e:
            logger.error(f"Failed to add QNAP download: {e}")
            return None

    async def get_task_status(self, task_id: str) -> Optional[QnapTask]:
        """Get status of a specific download task."""
        try:
            result = await self._request("Task", "Detail", {"id": task_id})

            data = result.get("data", {})
            status_code = data.get("status", 0)

            try:
                status = QnapTaskStatus(status_code)
            except ValueError:
                status = QnapTaskStatus.ERROR

            total = data.get("size", 0)
            downloaded = data.get("have", 0)
            progress = (downloaded / total) if total > 0 else 0.0

            return QnapTask(
                id=task_id,
                name=data.get("source", ""),
                source_url=data.get("source", ""),
                size=total,
                downloaded=downloaded,
                progress=progress,
                status=status,
                download_speed=data.get("down_rate", 0),
                error_message=data.get("error"),
                destination=data.get("move", ""),
            )

        except Exception as e:
            logger.error(f"Failed to get QNAP task status: {e}")
            return None

    async def query_tasks(self, limit: int = 100) -> list[QnapTask]:
        """Get all download tasks."""
        try:
            result = await self._request("Task", "Query", {"limit": limit})

            tasks = []
            for item in result.get("data", []):
                status_code = item.get("status", 0)
                try:
                    status = QnapTaskStatus(status_code)
                except ValueError:
                    status = QnapTaskStatus.ERROR

                total = item.get("size", 0)
                downloaded = item.get("have", 0)
                progress = (downloaded / total) if total > 0 else 0.0

                tasks.append(QnapTask(
                    id=str(item.get("id", "")),
                    name=item.get("name", ""),
                    source_url=item.get("source", ""),
                    size=total,
                    downloaded=downloaded,
                    progress=progress,
                    status=status,
                    download_speed=item.get("down_rate", 0),
                    destination=item.get("move", ""),
                ))

            return tasks

        except Exception as e:
            logger.error(f"Failed to query QNAP tasks: {e}")
            return []

    async def remove_task(self, task_id: str) -> bool:
        """Remove a download task."""
        try:
            await self._request("Task", "Remove", {"id": task_id})
            logger.info(f"Removed QNAP task: {task_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to remove QNAP task: {e}")
            return False

    async def pause_task(self, task_id: str) -> bool:
        """Pause a download task."""
        try:
            await self._request("Task", "Pause", {"id": task_id})
            return True
        except Exception as e:
            logger.error(f"Failed to pause QNAP task: {e}")
            return False

    async def start_task(self, task_id: str) -> bool:
        """Start/resume a download task."""
        try:
            await self._request("Task", "Start", {"id": task_id})
            return True
        except Exception as e:
            logger.error(f"Failed to start QNAP task: {e}")
            return False

    async def get_environment(self) -> dict:
        """Get QNAP Download Station environment info."""
        try:
            result = await self._request("Misc", "Env")
            return result.get("data", {})
        except Exception as e:
            logger.error(f"Failed to get QNAP environment: {e}")
            return {}

    async def get_directories(self) -> list[str]:
        """Get available download directories."""
        try:
            result = await self._request("Misc", "Dir")
            return result.get("data", [])
        except Exception as e:
            logger.error(f"Failed to get QNAP directories: {e}")
            return []

    async def test_connection(self) -> tuple[bool, str]:
        """Test connection to QNAP Download Station."""
        try:
            await self.login()
            env = await self.get_environment()
            version = env.get("version", "unknown")
            return True, f"Connected to QNAP Download Station v{version}"
        except Exception as e:
            return False, str(e)

    async def wait_for_completion(
        self,
        task_id: str,
        poll_interval: float = 5.0,
        timeout: float = 3600.0,
        progress_callback: Optional[Callable[[float, int], None]] = None,
    ) -> bool:
        """
        Wait for a download task to complete.

        Args:
            task_id: The task ID to monitor
            poll_interval: How often to check status (seconds)
            timeout: Maximum time to wait (seconds)
            progress_callback: Optional callback(progress, speed) for updates

        Returns:
            True if completed successfully, False otherwise
        """
        import time
        start_time = time.time()

        while True:
            if time.time() - start_time > timeout:
                logger.error(f"QNAP task {task_id} timed out after {timeout}s")
                return False

            task = await self.get_task_status(task_id)
            if not task:
                logger.error(f"QNAP task {task_id} not found")
                return False

            if progress_callback:
                progress_callback(task.progress, task.download_speed)

            if task.status == QnapTaskStatus.COMPLETED:
                logger.info(f"QNAP task {task_id} completed")
                return True

            if task.status == QnapTaskStatus.ERROR:
                logger.error(f"QNAP task {task_id} failed: {task.error_message}")
                return False

            await asyncio.sleep(poll_interval)
