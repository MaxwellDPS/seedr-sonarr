"""
Custom exception hierarchy for Seedr-Sonarr.
Provides specific exception types for better error handling and debugging.
"""


class SeedrSonarrError(Exception):
    """Base exception for all Seedr-Sonarr errors."""

    def __init__(self, message: str, details: str | None = None):
        super().__init__(message)
        self.message = message
        self.details = details

    def __str__(self) -> str:
        if self.details:
            return f"{self.message}: {self.details}"
        return self.message


# Configuration errors
class ConfigurationError(SeedrSonarrError):
    """Raised when there's a configuration problem."""

    pass


class MissingCredentialsError(ConfigurationError):
    """Raised when required credentials are missing."""

    pass


# Seedr client errors
class SeedrClientError(SeedrSonarrError):
    """Base exception for Seedr API errors."""

    pass


class SeedrConnectionError(SeedrClientError):
    """Raised when connection to Seedr fails."""

    pass


class SeedrAuthenticationError(SeedrClientError):
    """Raised when authentication to Seedr fails."""

    pass


class StorageFullError(SeedrClientError):
    """Raised when Seedr storage is full."""

    def __init__(
        self,
        message: str = "Seedr storage is full",
        available_bytes: int = 0,
        required_bytes: int = 0,
    ):
        super().__init__(message)
        self.available_bytes = available_bytes
        self.required_bytes = required_bytes


class TorrentNotFoundError(SeedrClientError):
    """Raised when a torrent is not found."""

    def __init__(self, torrent_hash: str, message: str | None = None):
        super().__init__(message or f"Torrent not found: {torrent_hash}")
        self.torrent_hash = torrent_hash


class TorrentAddError(SeedrClientError):
    """Raised when adding a torrent fails."""

    pass


class TorrentDeleteError(SeedrClientError):
    """Raised when deleting a torrent fails."""

    pass


# Download errors
class DownloadError(SeedrSonarrError):
    """Base exception for download errors."""

    pass


class DownloadTimeoutError(DownloadError):
    """Raised when a download times out."""

    pass


class DownloadIncompleteError(DownloadError):
    """Raised when a download is incomplete."""

    def __init__(
        self,
        message: str,
        expected_size: int = 0,
        actual_size: int = 0,
        failed_files: list[str] | None = None,
    ):
        super().__init__(message)
        self.expected_size = expected_size
        self.actual_size = actual_size
        self.failed_files = failed_files or []


class FileNotReadyError(DownloadError):
    """Raised when a file is not ready for download on Seedr."""

    pass


# QNAP errors
class QnapError(SeedrSonarrError):
    """Base exception for QNAP Download Station errors."""

    pass


class QnapConnectionError(QnapError):
    """Raised when connection to QNAP fails."""

    pass


class QnapAuthenticationError(QnapError):
    """Raised when QNAP authentication fails."""

    pass


class QnapTaskError(QnapError):
    """Raised when a QNAP task operation fails."""

    pass


# Authentication errors (for the qBittorrent API emulation)
class AuthenticationError(SeedrSonarrError):
    """Raised when API authentication fails."""

    pass


class SessionExpiredError(AuthenticationError):
    """Raised when the session has expired."""

    pass


class InvalidCredentialsError(AuthenticationError):
    """Raised when credentials are invalid."""

    pass


# Validation errors
class ValidationError(SeedrSonarrError):
    """Raised when input validation fails."""

    pass


class InvalidHashError(ValidationError):
    """Raised when a torrent hash is invalid."""

    def __init__(self, torrent_hash: str, message: str | None = None):
        super().__init__(message or f"Invalid torrent hash: {torrent_hash}")
        self.torrent_hash = torrent_hash


class InvalidCategoryError(ValidationError):
    """Raised when a category name is invalid."""

    def __init__(self, category: str, message: str | None = None):
        super().__init__(message or f"Invalid category: {category}")
        self.category = category


class PathTraversalError(ValidationError):
    """Raised when a path traversal attack is detected."""

    def __init__(self, attempted_path: str, base_path: str):
        super().__init__(
            f"Path traversal detected: {attempted_path} escapes {base_path}"
        )
        self.attempted_path = attempted_path
        self.base_path = base_path


# Resilience errors (moved from retry.py for centralized exception handling)
class CircuitOpenError(SeedrSonarrError):
    """Raised when circuit breaker is open."""

    def __init__(self, message: str, name: str | None = None, reset_timeout: float | None = None):
        # Support both old-style (message only) and new-style (name, reset_timeout)
        super().__init__(message)
        self.name = name
        self.reset_timeout = reset_timeout


class RateLimitTimeoutError(SeedrSonarrError):
    """Raised when rate limiter times out waiting for a token."""

    def __init__(self, message: str, timeout: float | None = None):
        # Support both old-style (message only) and new-style (message, timeout)
        super().__init__(message)
        self.timeout = timeout


# Persistence errors
class PersistenceError(SeedrSonarrError):
    """Base exception for persistence/database errors."""

    pass


class DatabaseConnectionError(PersistenceError):
    """Raised when database connection fails."""

    pass


class DatabaseMigrationError(PersistenceError):
    """Raised when database migration fails."""

    pass
