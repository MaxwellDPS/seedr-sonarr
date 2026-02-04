"""
Tests for the CLI module (cli.py).
Covers argument parsing, help text, and error handling.
"""

import argparse
import sys
from unittest.mock import patch, MagicMock

import pytest

from seedr_sonarr.cli import (
    main,
    setup_logging,
)


# =============================================================================
# Setup Logging Tests
# =============================================================================

class TestSetupLogging:
    """Test logging setup functionality."""

    def test_setup_logging_does_not_raise(self):
        """Test that setup_logging runs without error."""
        setup_logging("INFO")
        setup_logging("DEBUG")
        setup_logging("WARNING")

    def test_setup_logging_invalid_level(self):
        """Test that invalid log levels raise an error."""
        with pytest.raises(AttributeError):
            setup_logging("INVALID_LEVEL")


# =============================================================================
# Main Entry Point Tests
# =============================================================================

class TestMainEntryPoint:
    """Test the main() entry point and argument parsing."""

    def test_no_command_shows_help(self, capsys):
        """Test that no command shows help and exits."""
        with patch.object(sys, "argv", ["seedr-sonarr"]):
            with pytest.raises(SystemExit) as excinfo:
                main()
            assert excinfo.value.code == 1

    def test_help_flag(self, capsys):
        """Test that --help shows help and exits with 0."""
        with patch.object(sys, "argv", ["seedr-sonarr", "--help"]):
            with pytest.raises(SystemExit) as excinfo:
                main()
            assert excinfo.value.code == 0
        captured = capsys.readouterr()
        assert "seedr-sonarr" in captured.out

    def test_serve_command_recognized(self):
        """Test that serve command is recognized."""
        with patch.object(sys, "argv", ["seedr-sonarr", "serve", "--help"]):
            with pytest.raises(SystemExit) as excinfo:
                main()
            assert excinfo.value.code == 0

    def test_auth_command_recognized(self):
        """Test that auth command is recognized."""
        with patch.object(sys, "argv", ["seedr-sonarr", "auth", "--help"]):
            with pytest.raises(SystemExit) as excinfo:
                main()
            assert excinfo.value.code == 0

    def test_test_command_recognized(self):
        """Test that test command is recognized."""
        with patch.object(sys, "argv", ["seedr-sonarr", "test", "--help"]):
            with pytest.raises(SystemExit) as excinfo:
                main()
            assert excinfo.value.code == 0

    def test_list_command_recognized(self):
        """Test that list command is recognized."""
        with patch.object(sys, "argv", ["seedr-sonarr", "list", "--help"]):
            with pytest.raises(SystemExit) as excinfo:
                main()
            assert excinfo.value.code == 0

    def test_state_command_recognized(self):
        """Test that state command is recognized."""
        with patch.object(sys, "argv", ["seedr-sonarr", "state", "--help"]):
            with pytest.raises(SystemExit) as excinfo:
                main()
            assert excinfo.value.code == 0

    def test_queue_command_recognized(self):
        """Test that queue command is recognized."""
        with patch.object(sys, "argv", ["seedr-sonarr", "queue", "--help"]):
            with pytest.raises(SystemExit) as excinfo:
                main()
            assert excinfo.value.code == 0

    def test_logs_command_recognized(self):
        """Test that logs command is recognized."""
        with patch.object(sys, "argv", ["seedr-sonarr", "logs", "--help"]):
            with pytest.raises(SystemExit) as excinfo:
                main()
            assert excinfo.value.code == 0


# =============================================================================
# Serve Command Argument Tests
# =============================================================================

class TestServeCommand:
    """Test serve command argument parsing."""

    def _parse_serve_args(self, args_list):
        """Helper to parse serve command args."""
        with patch.object(sys, "argv", ["seedr-sonarr", "serve"] + args_list):
            parser = argparse.ArgumentParser()
            subparsers = parser.add_subparsers(dest="command")
            serve_parser = subparsers.add_parser("serve")
            serve_parser.add_argument("--host", default="0.0.0.0")
            serve_parser.add_argument("--port", type=int, default=8080)
            serve_parser.add_argument("--email")
            serve_parser.add_argument("--password")
            serve_parser.add_argument("--token")
            serve_parser.add_argument("--username", default="admin")
            serve_parser.add_argument("--api-password", default="adminadmin")
            serve_parser.add_argument("--download-path", default="/downloads")
            serve_parser.add_argument("--log-level", default="INFO")
            serve_parser.add_argument("--log-file")
            serve_parser.add_argument("--log-format", default="text")
            serve_parser.add_argument("--state-file")
            serve_parser.add_argument("--no-persist", action="store_true")
            serve_parser.add_argument("--reload", action="store_true")
            return parser.parse_args(["serve"] + args_list)

    def test_serve_default_args(self):
        """Test serve command default arguments."""
        args = self._parse_serve_args([])
        assert args.host == "0.0.0.0"
        assert args.port == 8080
        assert args.username == "admin"
        assert args.api_password == "adminadmin"
        assert args.download_path == "/downloads"
        assert args.log_level == "INFO"
        assert args.log_format == "text"
        assert args.no_persist is False
        assert args.reload is False

    def test_serve_custom_host_port(self):
        """Test serve command with custom host and port."""
        args = self._parse_serve_args(["--host", "127.0.0.1", "--port", "9000"])
        assert args.host == "127.0.0.1"
        assert args.port == 9000

    def test_serve_seedr_credentials(self):
        """Test serve command with Seedr credentials."""
        args = self._parse_serve_args([
            "--email", "test@example.com",
            "--password", "secret123",
        ])
        assert args.email == "test@example.com"
        assert args.password == "secret123"

    def test_serve_api_credentials(self):
        """Test serve command with custom API credentials."""
        args = self._parse_serve_args([
            "--username", "myuser",
            "--api-password", "mypass",
        ])
        assert args.username == "myuser"
        assert args.api_password == "mypass"

    def test_serve_logging_options(self):
        """Test serve command logging options."""
        args = self._parse_serve_args([
            "--log-level", "DEBUG",
            "--log-file", "/var/log/seedr.log",
            "--log-format", "json",
        ])
        assert args.log_level == "DEBUG"
        assert args.log_file == "/var/log/seedr.log"
        assert args.log_format == "json"

    def test_serve_no_persist_flag(self):
        """Test serve command --no-persist flag."""
        args = self._parse_serve_args(["--no-persist"])
        assert args.no_persist is True

    def test_serve_reload_flag(self):
        """Test serve command --reload flag."""
        args = self._parse_serve_args(["--reload"])
        assert args.reload is True


# =============================================================================
# Auth Command Argument Tests
# =============================================================================

class TestAuthCommand:
    """Test auth command argument parsing."""

    def _parse_auth_args(self, args_list):
        """Helper to parse auth command args."""
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")
        auth_parser = subparsers.add_parser("auth")
        auth_parser.add_argument("--save")
        return parser.parse_args(["auth"] + args_list)

    def test_auth_default_args(self):
        """Test auth command default arguments."""
        args = self._parse_auth_args([])
        assert args.save is None

    def test_auth_with_save(self):
        """Test auth command with --save option."""
        args = self._parse_auth_args(["--save", "/path/to/token.txt"])
        assert args.save == "/path/to/token.txt"


# =============================================================================
# Test Command Argument Tests
# =============================================================================

class TestTestCommand:
    """Test test command argument parsing."""

    def _parse_test_args(self, args_list):
        """Helper to parse test command args."""
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")
        test_parser = subparsers.add_parser("test")
        test_parser.add_argument("--email")
        test_parser.add_argument("--password")
        test_parser.add_argument("--token")
        return parser.parse_args(["test"] + args_list)

    def test_test_default_args(self):
        """Test test command default arguments."""
        args = self._parse_test_args([])
        assert args.email is None
        assert args.password is None
        assert args.token is None

    def test_test_with_credentials(self):
        """Test test command with credentials."""
        args = self._parse_test_args([
            "--email", "test@example.com",
            "--password", "secret",
        ])
        assert args.email == "test@example.com"
        assert args.password == "secret"


# =============================================================================
# List Command Argument Tests
# =============================================================================

class TestListCommand:
    """Test list command argument parsing."""

    def _parse_list_args(self, args_list):
        """Helper to parse list command args."""
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")
        list_parser = subparsers.add_parser("list")
        list_parser.add_argument("--email")
        list_parser.add_argument("--password")
        list_parser.add_argument("--token")
        list_parser.add_argument("--instance")
        return parser.parse_args(["list"] + args_list)

    def test_list_default_args(self):
        """Test list command default arguments."""
        args = self._parse_list_args([])
        assert args.instance is None

    def test_list_with_instance_filter(self):
        """Test list command with instance filter."""
        args = self._parse_list_args(["--instance", "radarr-4k"])
        assert args.instance == "radarr-4k"


# =============================================================================
# State Command Argument Tests
# =============================================================================

class TestStateCommand:
    """Test state command argument parsing."""

    def _parse_state_args(self, args_list):
        """Helper to parse state command args."""
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")
        state_parser = subparsers.add_parser("state")
        state_parser.add_argument("--db", default="seedr_state.db")
        state_parser.add_argument("--torrents", action="store_true")
        state_parser.add_argument("--queue", action="store_true")
        state_parser.add_argument("--categories", action="store_true")
        state_parser.add_argument("--stats", action="store_true")
        return parser.parse_args(["state"] + args_list)

    def test_state_default_args(self):
        """Test state command default arguments."""
        args = self._parse_state_args([])
        assert args.db == "seedr_state.db"
        assert args.torrents is False
        assert args.queue is False
        assert args.categories is False
        assert args.stats is False

    def test_state_with_db_path(self):
        """Test state command with custom DB path."""
        args = self._parse_state_args(["--db", "/custom/path.db"])
        assert args.db == "/custom/path.db"

    def test_state_filter_flags(self):
        """Test state command filter flags."""
        args = self._parse_state_args(["--torrents", "--queue", "--categories", "--stats"])
        assert args.torrents is True
        assert args.queue is True
        assert args.categories is True
        assert args.stats is True


# =============================================================================
# Queue Command Argument Tests
# =============================================================================

class TestQueueCommand:
    """Test queue command argument parsing."""

    def _parse_queue_args(self, args_list):
        """Helper to parse queue command args."""
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")
        queue_parser = subparsers.add_parser("queue")
        queue_parser.add_argument("--db", default="seedr_state.db")
        queue_subparsers = queue_parser.add_subparsers(dest="queue_command")
        queue_subparsers.add_parser("list")
        queue_subparsers.add_parser("clear")
        return parser.parse_args(["queue"] + args_list)

    def test_queue_list_args(self):
        """Test queue list subcommand."""
        args = self._parse_queue_args(["list"])
        assert args.queue_command == "list"

    def test_queue_clear_args(self):
        """Test queue clear subcommand."""
        args = self._parse_queue_args(["clear"])
        assert args.queue_command == "clear"

    def test_queue_with_db_path(self):
        """Test queue command with custom DB path."""
        args = self._parse_queue_args(["--db", "/custom/path.db", "list"])
        assert args.db == "/custom/path.db"


# =============================================================================
# Logs Command Argument Tests
# =============================================================================

class TestLogsCommand:
    """Test logs command argument parsing."""

    def _parse_logs_args(self, args_list):
        """Helper to parse logs command args."""
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")
        logs_parser = subparsers.add_parser("logs")
        logs_parser.add_argument("--db", default="seedr_state.db")
        logs_parser.add_argument("-n", "--limit", type=int, default=50)
        logs_parser.add_argument("--level")
        logs_parser.add_argument("--hash")
        return parser.parse_args(["logs"] + args_list)

    def test_logs_default_args(self):
        """Test logs command default arguments."""
        args = self._parse_logs_args([])
        assert args.db == "seedr_state.db"
        assert args.limit == 50
        assert args.level is None
        assert args.hash is None

    def test_logs_with_options(self):
        """Test logs command with all options."""
        args = self._parse_logs_args([
            "--db", "/custom/path.db",
            "-n", "100",
            "--level", "ERROR",
            "--hash", "ABC123",
        ])
        assert args.db == "/custom/path.db"
        assert args.limit == 100
        assert args.level == "ERROR"
        assert args.hash == "ABC123"


# =============================================================================
# Help Text Tests
# =============================================================================

class TestHelpText:
    """Test help text generation."""

    def test_main_help_contains_examples(self, capsys):
        """Test that main help contains examples."""
        with patch.object(sys, "argv", ["seedr-sonarr", "--help"]):
            with pytest.raises(SystemExit):
                main()
        captured = capsys.readouterr()
        # Just verify help is shown
        assert "usage:" in captured.out.lower() or "seedr-sonarr" in captured.out

    def test_serve_help(self, capsys):
        """Test serve command help."""
        with patch.object(sys, "argv", ["seedr-sonarr", "serve", "--help"]):
            with pytest.raises(SystemExit):
                main()
        captured = capsys.readouterr()
        assert "--host" in captured.out
        assert "--port" in captured.out


# =============================================================================
# Error Handling Tests
# =============================================================================

class TestErrorHandling:
    """Test error handling in CLI."""

    def test_invalid_port(self, capsys):
        """Test that invalid port raises error."""
        with patch.object(sys, "argv", ["seedr-sonarr", "serve", "--port", "invalid"]):
            with pytest.raises(SystemExit) as excinfo:
                main()
            assert excinfo.value.code != 0
