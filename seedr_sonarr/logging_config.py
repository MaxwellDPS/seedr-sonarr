"""
Structured Logging Configuration for Seedr-Sonarr Proxy
Provides JSON logging, log rotation, activity log buffer, and context filtering.
"""

import json
import logging
import logging.handlers
import sys
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any
import threading


class ContextFilter(logging.Filter):
    """
    Add context fields to log records.
    Allows setting per-request or per-operation context.
    """

    _local = threading.local()

    def __init__(self):
        super().__init__()

    @classmethod
    def set_context(cls, **kwargs) -> None:
        """Set context fields for subsequent log messages in this thread."""
        if not hasattr(cls._local, "context"):
            cls._local.context = {}
        cls._local.context.update(kwargs)

    @classmethod
    def clear_context(cls, *keys) -> None:
        """Clear specific context fields or all if no keys specified."""
        if not hasattr(cls._local, "context"):
            return
        if keys:
            for key in keys:
                cls._local.context.pop(key, None)
        else:
            cls._local.context.clear()

    @classmethod
    def get_context(cls) -> Dict[str, Any]:
        """Get current context."""
        if not hasattr(cls._local, "context"):
            return {}
        return dict(cls._local.context)

    def filter(self, record: logging.LogRecord) -> bool:
        """Add context fields to the log record."""
        if hasattr(self._local, "context"):
            for key, value in self._local.context.items():
                setattr(record, key, value)
        return True


class JSONFormatter(logging.Formatter):
    """
    Format logs as JSON for structured logging.
    Includes timestamp, level, logger name, message, and context fields.
    """

    CONTEXT_FIELDS = [
        "torrent_hash",
        "torrent_name",
        "category",
        "instance_id",
        "phase",
        "operation",
        "error",
        "duration_ms",
        "size_bytes",
        "progress",
    ]

    def __init__(self, include_extra: bool = True):
        super().__init__()
        self.include_extra = include_extra

    def format(self, record: logging.LogRecord) -> str:
        """Format log record as JSON."""
        log_obj = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Add context fields if present
        for field in self.CONTEXT_FIELDS:
            if hasattr(record, field):
                value = getattr(record, field)
                if value is not None:
                    log_obj[field] = value

        # Add exception info
        if record.exc_info:
            log_obj["exception"] = self.formatException(record.exc_info)
            log_obj["exception_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None

        # Add extra fields if enabled
        if self.include_extra:
            for key, value in record.__dict__.items():
                if (
                    key not in log_obj
                    and key not in self.CONTEXT_FIELDS
                    and not key.startswith("_")
                    and key not in (
                        "name", "msg", "args", "created", "filename", "funcName",
                        "levelname", "levelno", "lineno", "module", "msecs",
                        "pathname", "process", "processName", "relativeCreated",
                        "stack_info", "exc_info", "exc_text", "thread", "threadName",
                        "message", "asctime",
                    )
                ):
                    try:
                        json.dumps(value)  # Check if serializable
                        log_obj[key] = value
                    except (TypeError, ValueError):
                        log_obj[key] = str(value)

        return json.dumps(log_obj, default=str)


class ColoredFormatter(logging.Formatter):
    """
    Colored console formatter for better readability.
    """

    COLORS = {
        "DEBUG": "\033[36m",     # Cyan
        "INFO": "\033[32m",      # Green
        "WARNING": "\033[33m",   # Yellow
        "ERROR": "\033[31m",     # Red
        "CRITICAL": "\033[35m",  # Magenta
    }
    RESET = "\033[0m"

    def __init__(self, use_colors: bool = True):
        super().__init__(
            fmt="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        self.use_colors = use_colors and sys.stdout.isatty()

    def format(self, record: logging.LogRecord) -> str:
        """Format with optional colors."""
        message = super().format(record)

        if self.use_colors:
            color = self.COLORS.get(record.levelname, "")
            if color:
                # Color the level name
                message = message.replace(
                    record.levelname,
                    f"{color}{record.levelname}{self.RESET}",
                    1
                )

        # Add context fields if present
        context_parts = []
        for field in ["torrent_name", "category", "phase"]:
            if hasattr(record, field):
                value = getattr(record, field)
                if value:
                    context_parts.append(f"{field}={value}")

        if context_parts:
            message += f" [{', '.join(context_parts)}]"

        return message


@dataclass
class ActivityLogEntry:
    """Entry in the activity log buffer."""
    timestamp: str
    level: str
    logger: str
    message: str
    torrent_hash: Optional[str] = None
    torrent_name: Optional[str] = None
    category: Optional[str] = None
    instance_id: Optional[str] = None
    phase: Optional[str] = None
    extra: Optional[Dict[str, Any]] = None


class ActivityLogHandler(logging.Handler):
    """
    Custom handler that stores logs in a ring buffer for API access.
    Allows querying recent logs without external log aggregation.
    """

    def __init__(self, max_entries: int = 1000, min_level: int = logging.INFO):
        super().__init__(level=min_level)
        self._buffer: deque = deque(maxlen=max_entries)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        """Store log entry in buffer."""
        try:
            entry = ActivityLogEntry(
                timestamp=datetime.utcnow().isoformat() + "Z",
                level=record.levelname,
                logger=record.name,
                message=record.getMessage(),
                torrent_hash=getattr(record, "torrent_hash", None),
                torrent_name=getattr(record, "torrent_name", None),
                category=getattr(record, "category", None),
                instance_id=getattr(record, "instance_id", None),
                phase=getattr(record, "phase", None),
            )

            with self._lock:
                self._buffer.append(entry)

        except Exception:
            self.handleError(record)

    def get_logs(
        self,
        limit: int = 100,
        level: str = None,
        torrent_hash: str = None,
        instance_id: str = None,
        since: str = None,
    ) -> List[Dict[str, Any]]:
        """
        Get filtered logs from the buffer.

        Args:
            limit: Maximum number of entries to return
            level: Minimum log level filter
            torrent_hash: Filter by torrent hash
            instance_id: Filter by instance ID
            since: ISO timestamp, return only entries after this time

        Returns:
            List of log entries as dictionaries
        """
        with self._lock:
            entries = list(self._buffer)

        # Apply filters
        if level:
            level_priority = {
                "DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50
            }
            min_priority = level_priority.get(level.upper(), 0)
            entries = [
                e for e in entries
                if level_priority.get(e.level, 0) >= min_priority
            ]

        if torrent_hash:
            entries = [e for e in entries if e.torrent_hash == torrent_hash]

        if instance_id:
            entries = [e for e in entries if e.instance_id == instance_id]

        if since:
            entries = [e for e in entries if e.timestamp >= since]

        # Return most recent entries up to limit
        entries = entries[-limit:]

        # Convert to dicts
        return [
            {
                "timestamp": e.timestamp,
                "level": e.level,
                "logger": e.logger,
                "message": e.message,
                "torrent_hash": e.torrent_hash,
                "torrent_name": e.torrent_name,
                "category": e.category,
                "instance_id": e.instance_id,
                "phase": e.phase,
            }
            for e in entries
        ]

    def clear(self) -> int:
        """Clear the log buffer. Returns count cleared."""
        with self._lock:
            count = len(self._buffer)
            self._buffer.clear()
            return count

    def get_stats(self) -> Dict[str, Any]:
        """Get buffer statistics."""
        with self._lock:
            entries = list(self._buffer)

        level_counts = {}
        for e in entries:
            level_counts[e.level] = level_counts.get(e.level, 0) + 1

        return {
            "buffer_size": len(entries),
            "max_size": self._buffer.maxlen,
            "by_level": level_counts,
        }


# Component-specific log levels
COMPONENT_LOG_LEVELS = {
    "seedr_sonarr": "INFO",
    "seedr_sonarr.server": "INFO",
    "seedr_sonarr.seedr_client": "INFO",
    "seedr_sonarr.persistence": "WARNING",
    "seedr_sonarr.state": "INFO",
    "seedr_sonarr.retry": "INFO",
    "aiohttp": "WARNING",
    "aiohttp.client": "WARNING",
    "aiosqlite": "WARNING",
    "uvicorn": "INFO",
    "uvicorn.access": "WARNING",
    "uvicorn.error": "INFO",
    "fastapi": "INFO",
}


def setup_logging(
    log_level: str = "INFO",
    log_file: Optional[str] = None,
    log_format: str = "text",
    max_file_size_mb: int = 10,
    backup_count: int = 5,
    use_colors: bool = True,
    activity_log_size: int = 1000,
) -> ActivityLogHandler:
    """
    Configure logging with optional file rotation and structured output.

    Args:
        log_level: Root log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_file: Path to log file (enables rotation if set)
        log_format: "text" for human-readable, "json" for structured
        max_file_size_mb: Maximum size of each log file before rotation
        backup_count: Number of rotated log files to keep
        use_colors: Use colored output in console (if terminal supports it)
        activity_log_size: Number of entries in the activity log buffer

    Returns:
        ActivityLogHandler for API access to recent logs
    """
    # Get root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    # Remove existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Create context filter
    context_filter = ContextFilter()

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.addFilter(context_filter)

    if log_format == "json":
        console_handler.setFormatter(JSONFormatter())
    else:
        console_handler.setFormatter(ColoredFormatter(use_colors=use_colors))

    root_logger.addHandler(console_handler)

    # File handler with rotation (if log_file specified)
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        file_handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=max_file_size_mb * 1024 * 1024,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.addFilter(context_filter)

        if log_format == "json":
            file_handler.setFormatter(JSONFormatter())
        else:
            file_handler.setFormatter(logging.Formatter(
                "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            ))

        root_logger.addHandler(file_handler)

    # Activity log handler for API access
    activity_handler = ActivityLogHandler(
        max_entries=activity_log_size,
        min_level=logging.INFO,
    )
    activity_handler.addFilter(context_filter)
    root_logger.addHandler(activity_handler)

    # Set component-specific log levels
    for logger_name, level in COMPONENT_LOG_LEVELS.items():
        logging.getLogger(logger_name).setLevel(getattr(logging, level))

    logger = logging.getLogger(__name__)
    logger.info(
        f"Logging configured: level={log_level}, format={log_format}, "
        f"file={log_file or 'none'}"
    )

    return activity_handler


def get_logger(name: str) -> logging.Logger:
    """Get a logger with the given name."""
    return logging.getLogger(name)


class LogContext:
    """
    Context manager for setting log context fields.

    Usage:
        with LogContext(torrent_hash="ABC123", torrent_name="Movie.mkv"):
            logger.info("Processing torrent")
    """

    def __init__(self, **kwargs):
        self.context = kwargs
        self.previous_context = {}

    def __enter__(self):
        self.previous_context = ContextFilter.get_context()
        ContextFilter.set_context(**self.context)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        ContextFilter.clear_context()
        if self.previous_context:
            ContextFilter.set_context(**self.previous_context)
        return False


def log_operation(
    logger: logging.Logger,
    operation: str,
    torrent_hash: str = None,
    torrent_name: str = None,
    category: str = None,
    level: int = logging.INFO,
    **extra,
) -> None:
    """
    Log an operation with context.

    Args:
        logger: Logger instance
        operation: Operation description
        torrent_hash: Hash of the torrent
        torrent_name: Name of the torrent
        category: Category
        level: Log level
        **extra: Additional context fields
    """
    with LogContext(
        torrent_hash=torrent_hash,
        torrent_name=torrent_name,
        category=category,
        operation=operation,
        **extra,
    ):
        logger.log(level, operation)
