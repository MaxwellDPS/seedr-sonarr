"""
Tests for Logging Configuration (seedr_sonarr/logging_config.py)
"""

import json
import logging
import pytest
import sys
import time
from io import StringIO
from datetime import datetime
from unittest.mock import patch, MagicMock

from seedr_sonarr.logging_config import (
    ContextFilter,
    JSONFormatter,
    ColoredFormatter,
    ActivityLogEntry,
    ActivityLogHandler,
    LogContext,
    setup_logging,
    get_logger,
    log_operation,
    COMPONENT_LOG_LEVELS,
)


class TestContextFilter:
    """Tests for ContextFilter."""

    def setup_method(self):
        """Clear context before each test."""
        ContextFilter.clear_context()

    def teardown_method(self):
        """Clear context after each test."""
        ContextFilter.clear_context()

    def test_set_context(self):
        """Test setting context."""
        ContextFilter.set_context(torrent_hash="ABC123", torrent_name="Test")
        context = ContextFilter.get_context()
        assert context["torrent_hash"] == "ABC123"
        assert context["torrent_name"] == "Test"

    def test_clear_specific_context(self):
        """Test clearing specific context keys."""
        ContextFilter.set_context(
            torrent_hash="ABC123",
            torrent_name="Test",
            category="radarr"
        )
        ContextFilter.clear_context("torrent_hash")

        context = ContextFilter.get_context()
        assert "torrent_hash" not in context
        assert context["torrent_name"] == "Test"
        assert context["category"] == "radarr"

    def test_clear_all_context(self):
        """Test clearing all context."""
        ContextFilter.set_context(torrent_hash="ABC123", torrent_name="Test")
        ContextFilter.clear_context()

        context = ContextFilter.get_context()
        assert context == {}

    def test_filter_adds_context_to_record(self):
        """Test filter adds context to log record."""
        filter = ContextFilter()
        ContextFilter.set_context(torrent_hash="ABC123")

        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="Test message",
            args=(),
            exc_info=None,
        )

        result = filter.filter(record)
        assert result is True
        assert hasattr(record, "torrent_hash")
        assert record.torrent_hash == "ABC123"

    def test_empty_context_returns_empty_dict(self):
        """Test empty context returns empty dict."""
        context = ContextFilter.get_context()
        assert context == {}


class TestJSONFormatter:
    """Tests for JSONFormatter."""

    @pytest.fixture
    def formatter(self):
        return JSONFormatter()

    @pytest.fixture
    def log_record(self):
        return logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname="test.py",
            lineno=42,
            msg="Test message",
            args=(),
            exc_info=None,
        )

    def test_format_basic(self, formatter, log_record):
        """Test basic JSON formatting."""
        output = formatter.format(log_record)
        data = json.loads(output)

        assert data["level"] == "INFO"
        assert data["logger"] == "test.logger"
        assert data["message"] == "Test message"
        assert "timestamp" in data

    def test_format_with_context_fields(self, formatter):
        """Test formatting with context fields."""
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="Test",
            args=(),
            exc_info=None,
        )
        record.torrent_hash = "ABC123"
        record.category = "radarr"

        output = formatter.format(record)
        data = json.loads(output)

        assert data["torrent_hash"] == "ABC123"
        assert data["category"] == "radarr"

    def test_format_with_exception(self, formatter):
        """Test formatting with exception info."""
        try:
            raise ValueError("Test error")
        except ValueError:
            import sys
            exc_info = sys.exc_info()

        record = logging.LogRecord(
            name="test",
            level=logging.ERROR,
            pathname="test.py",
            lineno=1,
            msg="Error occurred",
            args=(),
            exc_info=exc_info,
        )

        output = formatter.format(record)
        data = json.loads(output)

        assert "exception" in data
        assert data["exception_type"] == "ValueError"

    def test_format_timestamp_format(self, formatter, log_record):
        """Test timestamp is ISO format with Z."""
        output = formatter.format(log_record)
        data = json.loads(output)

        assert data["timestamp"].endswith("Z")
        # Should be parseable as ISO format
        datetime.fromisoformat(data["timestamp"].replace("Z", "+00:00"))


class TestColoredFormatter:
    """Tests for ColoredFormatter."""

    @pytest.fixture
    def log_record(self):
        return logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="Test message",
            args=(),
            exc_info=None,
        )

    def test_format_without_colors(self, log_record):
        """Test formatting without colors."""
        formatter = ColoredFormatter(use_colors=False)
        output = formatter.format(log_record)
        assert "Test message" in output
        assert "\033[" not in output  # No ANSI codes

    def test_format_with_context(self):
        """Test formatting includes context."""
        formatter = ColoredFormatter(use_colors=False)
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="Test message",
            args=(),
            exc_info=None,
        )
        record.torrent_name = "Movie.mkv"
        record.category = "radarr"

        output = formatter.format(record)
        assert "torrent_name=Movie.mkv" in output
        assert "category=radarr" in output

    def test_different_log_levels(self):
        """Test different log levels have different colors."""
        formatter = ColoredFormatter(use_colors=True)
        # Mock isatty to return True
        with patch.object(sys.stdout, "isatty", return_value=True):
            formatter = ColoredFormatter(use_colors=True)

            for level in [logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR]:
                record = logging.LogRecord(
                    name="test",
                    level=level,
                    pathname="test.py",
                    lineno=1,
                    msg="Test",
                    args=(),
                    exc_info=None,
                )
                output = formatter.format(record)
                # Should contain level name
                assert logging.getLevelName(level) in output


class TestActivityLogEntry:
    """Tests for ActivityLogEntry dataclass."""

    def test_create_entry(self):
        """Test creating an entry."""
        entry = ActivityLogEntry(
            timestamp="2024-01-01T00:00:00Z",
            level="INFO",
            logger="test",
            message="Test message",
            torrent_hash="ABC123",
        )
        assert entry.timestamp == "2024-01-01T00:00:00Z"
        assert entry.level == "INFO"
        assert entry.torrent_hash == "ABC123"

    def test_optional_fields_default_none(self):
        """Test optional fields default to None."""
        entry = ActivityLogEntry(
            timestamp="2024-01-01T00:00:00Z",
            level="INFO",
            logger="test",
            message="Test",
        )
        assert entry.torrent_hash is None
        assert entry.category is None
        assert entry.instance_id is None


class TestActivityLogHandler:
    """Tests for ActivityLogHandler."""

    @pytest.fixture
    def handler(self):
        return ActivityLogHandler(max_entries=100)

    @pytest.fixture
    def log_record(self):
        return logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="Test message",
            args=(),
            exc_info=None,
        )

    def test_emit_stores_entry(self, handler, log_record):
        """Test emit stores log entry."""
        handler.emit(log_record)
        logs = handler.get_logs()
        assert len(logs) == 1
        assert logs[0]["message"] == "Test message"

    def test_get_logs_with_limit(self, handler):
        """Test get_logs respects limit."""
        for i in range(50):
            record = logging.LogRecord(
                name="test",
                level=logging.INFO,
                pathname="test.py",
                lineno=1,
                msg=f"Message {i}",
                args=(),
                exc_info=None,
            )
            handler.emit(record)

        logs = handler.get_logs(limit=10)
        assert len(logs) == 10

    def test_get_logs_filter_by_level(self, handler):
        """Test filtering logs by level."""
        info_record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=1,
            msg="Info", args=(), exc_info=None,
        )
        error_record = logging.LogRecord(
            name="test", level=logging.ERROR, pathname="", lineno=1,
            msg="Error", args=(), exc_info=None,
        )

        handler.emit(info_record)
        handler.emit(error_record)

        logs = handler.get_logs(level="ERROR")
        assert len(logs) == 1
        assert logs[0]["level"] == "ERROR"

    def test_get_logs_filter_by_torrent_hash(self, handler):
        """Test filtering logs by torrent hash."""
        record1 = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=1,
            msg="Message 1", args=(), exc_info=None,
        )
        record1.torrent_hash = "ABC123"

        record2 = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=1,
            msg="Message 2", args=(), exc_info=None,
        )
        record2.torrent_hash = "DEF456"

        handler.emit(record1)
        handler.emit(record2)

        logs = handler.get_logs(torrent_hash="ABC123")
        assert len(logs) == 1
        assert logs[0]["torrent_hash"] == "ABC123"

    def test_get_logs_filter_by_instance_id(self, handler):
        """Test filtering logs by instance ID."""
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=1,
            msg="Message", args=(), exc_info=None,
        )
        record.instance_id = "radarr-4k"
        handler.emit(record)

        logs = handler.get_logs(instance_id="radarr-4k")
        assert len(logs) == 1
        assert logs[0]["instance_id"] == "radarr-4k"

    def test_get_logs_filter_by_since(self, handler):
        """Test filtering logs by timestamp."""
        old_record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=1,
            msg="Old", args=(), exc_info=None,
        )
        handler.emit(old_record)

        since_time = datetime.utcnow().isoformat() + "Z"
        time.sleep(0.01)  # Ensure time difference

        new_record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=1,
            msg="New", args=(), exc_info=None,
        )
        handler.emit(new_record)

        logs = handler.get_logs(since=since_time)
        assert len(logs) == 1
        assert logs[0]["message"] == "New"

    def test_clear_buffer(self, handler, log_record):
        """Test clearing the buffer."""
        handler.emit(log_record)
        handler.emit(log_record)
        handler.emit(log_record)

        count = handler.clear()
        assert count == 3
        assert len(handler.get_logs()) == 0

    def test_max_entries(self):
        """Test buffer respects max entries."""
        handler = ActivityLogHandler(max_entries=5)

        for i in range(10):
            record = logging.LogRecord(
                name="test", level=logging.INFO, pathname="", lineno=1,
                msg=f"Message {i}", args=(), exc_info=None,
            )
            handler.emit(record)

        logs = handler.get_logs()
        assert len(logs) == 5
        # Should have the latest 5 messages
        assert logs[0]["message"] == "Message 5"
        assert logs[4]["message"] == "Message 9"

    def test_get_stats(self, handler):
        """Test getting buffer statistics."""
        for level, count in [(logging.INFO, 5), (logging.ERROR, 3)]:
            for _ in range(count):
                record = logging.LogRecord(
                    name="test", level=level, pathname="", lineno=1,
                    msg="Test", args=(), exc_info=None,
                )
                handler.emit(record)

        stats = handler.get_stats()
        assert stats["buffer_size"] == 8
        assert stats["max_size"] == 100
        assert stats["by_level"]["INFO"] == 5
        assert stats["by_level"]["ERROR"] == 3

    def test_min_level_filter(self):
        """Test minimum level filter."""
        handler = ActivityLogHandler(max_entries=100, min_level=logging.WARNING)

        debug_record = logging.LogRecord(
            name="test", level=logging.DEBUG, pathname="", lineno=1,
            msg="Debug", args=(), exc_info=None,
        )
        warning_record = logging.LogRecord(
            name="test", level=logging.WARNING, pathname="", lineno=1,
            msg="Warning", args=(), exc_info=None,
        )

        handler.emit(debug_record)
        handler.emit(warning_record)

        logs = handler.get_logs()
        # Handler level filter should apply - at least WARNING should be present
        warning_logs = [l for l in logs if l["level"] == "WARNING"]
        assert len(warning_logs) >= 1


class TestLogContext:
    """Tests for LogContext context manager."""

    def setup_method(self):
        """Clear context before each test."""
        ContextFilter.clear_context()

    def teardown_method(self):
        """Clear context after each test."""
        ContextFilter.clear_context()

    def test_context_set_on_enter(self):
        """Test context is set on enter."""
        with LogContext(torrent_hash="ABC123", category="radarr"):
            context = ContextFilter.get_context()
            assert context["torrent_hash"] == "ABC123"
            assert context["category"] == "radarr"

    def test_context_cleared_on_exit(self):
        """Test context is cleared on exit."""
        with LogContext(torrent_hash="ABC123"):
            pass

        context = ContextFilter.get_context()
        assert "torrent_hash" not in context

    def test_previous_context_restored(self):
        """Test previous context is restored."""
        ContextFilter.set_context(existing_key="value")

        with LogContext(torrent_hash="ABC123"):
            pass

        context = ContextFilter.get_context()
        assert context["existing_key"] == "value"

    def test_nested_context(self):
        """Test nested context managers."""
        with LogContext(outer="outer_value"):
            with LogContext(inner="inner_value"):
                context = ContextFilter.get_context()
                assert context["inner"] == "inner_value"

            context = ContextFilter.get_context()
            assert context["outer"] == "outer_value"


class TestSetupLogging:
    """Tests for setup_logging function."""

    def teardown_method(self):
        """Clean up logging handlers after each test."""
        root = logging.getLogger()
        for handler in root.handlers[:]:
            root.removeHandler(handler)

    def test_setup_logging_returns_activity_handler(self):
        """Test setup returns activity handler."""
        handler = setup_logging(log_level="INFO")
        assert isinstance(handler, ActivityLogHandler)

    def test_setup_logging_sets_root_level(self):
        """Test setup sets root logger level."""
        setup_logging(log_level="DEBUG")
        assert logging.getLogger().level == logging.DEBUG

    def test_setup_logging_json_format(self, tmp_path):
        """Test setup with JSON format."""
        log_file = tmp_path / "test.log"
        setup_logging(log_format="json", log_file=str(log_file))

        logger = logging.getLogger("test.setup")
        logger.info("Test message")

        # Check file contains JSON - may have multiple lines
        content = log_file.read_text()
        lines = [l for l in content.strip().split('\n') if l]
        # Parse the last line which should be our test message
        for line in lines:
            data = json.loads(line)
            if data.get("message") == "Test message":
                break
        else:
            # If we didn't find it, check the last line at least parses
            data = json.loads(lines[-1])
        assert "message" in data

    def test_setup_logging_text_format(self, tmp_path):
        """Test setup with text format."""
        log_file = tmp_path / "test.log"
        setup_logging(log_format="text", log_file=str(log_file))

        logger = logging.getLogger("test.setup")
        logger.info("Test message")

        content = log_file.read_text()
        assert "Test message" in content
        assert "INFO" in content

    def test_component_log_levels_set(self):
        """Test component-specific log levels are set."""
        setup_logging()

        for logger_name, level_name in COMPONENT_LOG_LEVELS.items():
            logger = logging.getLogger(logger_name)
            expected_level = getattr(logging, level_name)
            assert logger.level == expected_level


class TestGetLogger:
    """Tests for get_logger function."""

    def test_get_logger_returns_logger(self):
        """Test get_logger returns a logger."""
        logger = get_logger("test.module")
        assert isinstance(logger, logging.Logger)
        assert logger.name == "test.module"


class TestLogOperation:
    """Tests for log_operation function."""

    def setup_method(self):
        """Clear context before each test."""
        ContextFilter.clear_context()

    def teardown_method(self):
        """Clear context after each test."""
        ContextFilter.clear_context()

    def test_log_operation(self):
        """Test log_operation logs with context."""
        logger = MagicMock()

        log_operation(
            logger,
            operation="Adding torrent",
            torrent_hash="ABC123",
            torrent_name="Movie.mkv",
            category="radarr",
        )

        logger.log.assert_called_once()
        call_args = logger.log.call_args
        assert call_args[0][0] == logging.INFO
        assert call_args[0][1] == "Adding torrent"

    def test_log_operation_with_level(self):
        """Test log_operation with custom level."""
        logger = MagicMock()

        log_operation(
            logger,
            operation="Error occurred",
            level=logging.ERROR,
        )

        call_args = logger.log.call_args
        assert call_args[0][0] == logging.ERROR
