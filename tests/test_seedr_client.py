"""
Tests for seedr_client.py utility functions
"""

import pytest
import os
import tempfile

from seedr_sonarr.seedr_client import (
    sanitize_path_component,
    safe_join_path,
    normalize_folder_name,
)


class TestSanitizePathComponent:
    """Tests for sanitize_path_component function."""

    def test_empty_string(self):
        """Test empty string returns empty."""
        assert sanitize_path_component("") == ""

    def test_normal_string(self):
        """Test normal string passes through."""
        assert sanitize_path_component("radarr") == "radarr"

    def test_removes_parent_directory_refs(self):
        """Test .. is removed."""
        assert sanitize_path_component("../etc/passwd") == "etc_passwd"
        assert sanitize_path_component("..") == ""
        assert sanitize_path_component("foo/../bar") == "foo_bar"

    def test_removes_absolute_path(self):
        """Test leading slashes are removed."""
        assert sanitize_path_component("/etc/passwd") == "etc_passwd"
        assert sanitize_path_component("\\Windows\\System32") == "Windows_System32"

    def test_removes_null_bytes(self):
        """Test null bytes are removed."""
        assert sanitize_path_component("foo\x00bar") == "foobar"

    def test_removes_slashes(self):
        """Test slashes become underscores."""
        assert sanitize_path_component("tv/shows") == "tv_shows"
        assert sanitize_path_component("tv\\shows") == "tv_shows"

    def test_collapses_underscores(self):
        """Test multiple underscores are collapsed."""
        result = sanitize_path_component("foo//bar")
        assert "__" not in result

    def test_strips_whitespace(self):
        """Test whitespace is stripped."""
        assert sanitize_path_component("  radarr  ") == "radarr"


class TestSafeJoinPath:
    """Tests for safe_join_path function."""

    def test_simple_join(self):
        """Test simple path joining."""
        result = safe_join_path("/downloads", "radarr")
        assert result.endswith("/radarr")
        assert "/downloads" in result

    def test_multiple_components(self):
        """Test joining multiple components."""
        result = safe_join_path("/downloads", "radarr", "movies")
        assert "radarr" in result
        assert "movies" in result

    def test_empty_components_filtered(self):
        """Test empty components are filtered out."""
        result = safe_join_path("/downloads", "", "radarr")
        assert result.endswith("/radarr")

    def test_path_traversal_blocked(self):
        """Test path traversal is blocked."""
        # This should not raise because .. is sanitized out
        result = safe_join_path("/downloads", "../etc")
        assert "/downloads" in result
        # The result should not contain /etc as a separate directory
        assert not result.startswith("/etc")

    def test_absolute_path_blocked(self):
        """Test absolute path components are sanitized."""
        result = safe_join_path("/downloads", "/etc/passwd")
        assert "/downloads" in result
        # Should not be absolute /etc
        assert not result.startswith("/etc")


class TestNormalizeFolderName:
    """Tests for normalize_folder_name function."""

    def test_empty_string(self):
        """Test empty string returns empty."""
        assert normalize_folder_name("") == ""

    def test_normal_string(self):
        """Test normal string passes through."""
        assert normalize_folder_name("Movie Name") == "Movie Name"

    def test_plus_to_space(self):
        """Test + is converted to space."""
        assert normalize_folder_name("Movie+Name") == "Movie Name"

    def test_collapse_spaces(self):
        """Test multiple spaces are collapsed."""
        assert normalize_folder_name("Movie  Name") == "Movie Name"

    def test_strip_whitespace(self):
        """Test whitespace is stripped."""
        assert normalize_folder_name("  Movie Name  ") == "Movie Name"

    def test_combined_normalization(self):
        """Test combined normalization."""
        assert normalize_folder_name("  Movie+Name++Here  ") == "Movie Name Here"
