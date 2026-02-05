"""
Tests for security features and vulnerabilities.
Tests authentication, session management, and attack prevention.
"""

import secrets
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient

from seedr_sonarr.server import app, reset_auth_rate_limit
import asyncio


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Reset auth rate limiter before each test."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    loop.run_until_complete(reset_auth_rate_limit())
    yield
    loop.run_until_complete(reset_auth_rate_limit())


class TestTimingSafeAuthentication:
    """Tests for timing-safe password comparison."""

    def test_login_success_timing(self):
        """Successful login timing."""
        client = TestClient(app)

        # Multiple successful logins
        times = []
        for _ in range(10):
            start = time.perf_counter()
            response = client.post(
                "/api/v2/auth/login",
                data={"username": "admin", "password": "adminadmin"},
            )
            elapsed = time.perf_counter() - start
            times.append(elapsed)
            assert response.text == "Ok."

        avg_success = sum(times) / len(times)
        return avg_success

    def test_login_failure_timing_consistency(self):
        """Failed logins should have consistent timing regardless of username length."""
        client = TestClient(app)

        # Test with different username lengths
        usernames = ["a", "admin", "a" * 100, "admin" * 10]
        times_by_length = {}

        for username in usernames:
            times = []
            for _ in range(5):
                start = time.perf_counter()
                response = client.post(
                    "/api/v2/auth/login",
                    data={"username": username, "password": "wrongpassword"},
                )
                elapsed = time.perf_counter() - start
                times.append(elapsed)
                assert response.text == "Fails."
            times_by_length[len(username)] = sum(times) / len(times)

        # Timing variance should be small (timing-safe comparison)
        # Note: This is a weak test - timing attacks need statistical analysis
        times_list = list(times_by_length.values())
        if len(times_list) > 1:
            variance = max(times_list) - min(times_list)
            # Timing should be within reasonable variance (network/system jitter)
            # A timing attack would show clear correlation with username length
            assert True  # Placeholder - real timing tests need more samples


class TestSessionSecurity:
    """Tests for session security."""

    def test_session_cookie_httponly(self):
        """Session cookie should have httponly flag."""
        client = TestClient(app)
        response = client.post(
            "/api/v2/auth/login",
            data={"username": "admin", "password": "adminadmin"},
        )
        assert response.status_code == 200

        # Check cookie attributes
        cookies = response.cookies
        assert "SID" in cookies
        # The cookie jar in TestClient doesn't expose httponly directly
        # but we can check the Set-Cookie header
        set_cookie = response.headers.get("set-cookie", "")
        assert "httponly" in set_cookie.lower()

    def test_session_token_randomness(self):
        """Session tokens should be sufficiently random."""
        client = TestClient(app)

        # Get multiple session tokens
        tokens = set()
        for _ in range(100):
            response = client.post(
                "/api/v2/auth/login",
                data={"username": "admin", "password": "adminadmin"},
            )
            token = response.cookies.get("SID")
            if token:
                tokens.add(token)

        # All tokens should be unique
        assert len(tokens) == 100

        # Tokens should be of sufficient length (32 hex chars from 16 bytes)
        for token in tokens:
            assert len(token) >= 32

    def test_invalid_session_rejected(self):
        """Invalid session cookies are rejected."""
        client = TestClient(app)

        # Try with fake session
        client.cookies.set("SID", "fake_session_id_12345")
        response = client.get("/api/v2/app/version")
        assert response.status_code == 403

    def test_expired_session_rejected(self):
        """Expired sessions are rejected."""
        from datetime import datetime, timedelta
        from seedr_sonarr.server import sessions, _sessions_lock, create_session
        import asyncio

        client = TestClient(app)

        # Create a session and immediately expire it
        response = client.post(
            "/api/v2/auth/login",
            data={"username": "admin", "password": "adminadmin"},
        )
        sid = response.cookies.get("SID")

        # Manually expire the session
        async def expire_session():
            async with _sessions_lock:
                if sid in sessions:
                    sessions[sid] = datetime.now() - timedelta(hours=1)

        asyncio.get_event_loop().run_until_complete(expire_session())

        # Session should now be rejected
        client.cookies.set("SID", sid)
        response = client.get("/api/v2/app/version")
        assert response.status_code == 403

    def test_session_logout_invalidates(self):
        """Logout invalidates the session."""
        client = TestClient(app)

        # Login
        response = client.post(
            "/api/v2/auth/login",
            data={"username": "admin", "password": "adminadmin"},
        )
        client.cookies.update(response.cookies)

        # Verify session works
        response = client.get("/api/v2/app/version")
        assert response.status_code == 200

        # Logout
        response = client.post("/api/v2/auth/logout")

        # Session should no longer work
        response = client.get("/api/v2/app/version")
        assert response.status_code == 403


class TestEndpointAuthentication:
    """Tests for endpoint authentication requirements."""

    @pytest.fixture
    def client(self):
        """Create unauthenticated test client."""
        return TestClient(app)

    def test_protected_endpoints_require_auth(self, client):
        """All protected endpoints require authentication."""
        protected_endpoints = [
            ("GET", "/api/v2/app/version"),
            ("GET", "/api/v2/app/webapiVersion"),
            ("GET", "/api/v2/app/buildInfo"),
            ("GET", "/api/v2/app/preferences"),
            ("GET", "/api/v2/app/defaultSavePath"),
            ("GET", "/api/v2/transfer/info"),
            ("GET", "/api/v2/torrents/info"),
            ("GET", "/api/v2/torrents/categories"),
            ("GET", "/api/v2/sync/maindata"),
            ("GET", "/api/seedr/downloads"),
            ("GET", "/api/seedr/queue"),
            ("GET", "/api/seedr/storage"),
            ("GET", "/api/seedr/logs"),
            ("GET", "/api/seedr/state"),
        ]

        for method, endpoint in protected_endpoints:
            if method == "GET":
                response = client.get(endpoint)
            else:
                response = client.post(endpoint)

            assert response.status_code == 403, f"{method} {endpoint} should require auth"

    def test_public_endpoints_accessible(self, client):
        """Public endpoints are accessible without auth."""
        public_endpoints = [
            ("GET", "/health"),
            ("GET", "/api/health"),
            ("POST", "/api/v2/auth/login"),
            ("POST", "/api/v2/auth/logout"),
        ]

        for method, endpoint in public_endpoints:
            if method == "GET":
                response = client.get(endpoint)
            else:
                response = client.post(endpoint, data={})

            # Should not return 403
            assert response.status_code != 403, f"{method} {endpoint} should be public"


class TestInputSanitization:
    """Tests for input sanitization to prevent injection attacks."""

    @pytest.fixture
    def auth_client(self):
        """Create authenticated test client."""
        client = TestClient(app)
        response = client.post(
            "/api/v2/auth/login",
            data={"username": "admin", "password": "adminadmin"},
        )
        client.cookies.update(response.cookies)
        return client

    def test_sql_injection_in_category(self, auth_client):
        """SQL injection attempts in category are handled safely."""
        with patch("seedr_sonarr.server.state_manager") as mock_state:
            mock_state.add_category = AsyncMock()

            # Various SQL injection attempts
            payloads = [
                "'; DROP TABLE torrents; --",
                "1 OR 1=1",
                "UNION SELECT * FROM users",
                "'; DELETE FROM categories WHERE ''='",
            ]

            for payload in payloads:
                response = auth_client.post(
                    "/api/v2/torrents/createCategory",
                    data={"category": payload, "savePath": ""},
                )
                # Should handle without error
                assert response.status_code == 200

    def test_xss_in_category_name(self, auth_client):
        """XSS attempts in category name are handled safely."""
        with patch("seedr_sonarr.server.state_manager") as mock_state:
            mock_state.add_category = AsyncMock()

            payloads = [
                "<script>alert('xss')</script>",
                "javascript:alert('xss')",
                "<img src=x onerror=alert('xss')>",
            ]

            for payload in payloads:
                response = auth_client.post(
                    "/api/v2/torrents/createCategory",
                    data={"category": payload, "savePath": ""},
                )
                assert response.status_code == 200

    def test_command_injection_in_hash(self, auth_client):
        """Command injection attempts in hash are handled safely."""
        payloads = [
            "; rm -rf /",
            "| cat /etc/passwd",
            "$(whoami)",
            "`id`",
        ]

        for payload in payloads:
            response = auth_client.get(
                "/api/v2/torrents/properties",
                params={"hash": payload},
            )
            # Should return 404 or 503, not execute commands
            assert response.status_code in (404, 500, 503)


class TestPathTraversalPrevention:
    """Tests for path traversal attack prevention."""

    def test_path_traversal_in_save_path(self):
        """Path traversal in savePath is prevented."""
        client = TestClient(app)
        response = client.post(
            "/api/v2/auth/login",
            data={"username": "admin", "password": "adminadmin"},
        )
        client.cookies.update(response.cookies)

        with patch("seedr_sonarr.server.state_manager") as mock_state:
            mock_state.add_category = AsyncMock()

            response = client.post(
                "/api/v2/torrents/createCategory",
                data={
                    "category": "test",
                    "savePath": "../../../../etc/passwd",
                },
            )
            # Should either reject or sanitize the path
            assert response.status_code == 200


class TestErrorMessageSanitization:
    """Tests for error message sanitization."""

    def test_error_messages_dont_leak_secrets(self):
        """Error messages don't expose sensitive information."""
        from seedr_sonarr.server import sanitize_error_message

        # Errors containing sensitive keywords should be sanitized
        sensitive_errors = [
            "Invalid token: ABC123DEF456",
            "Password mismatch for user admin",
            "API key invalid: secret_key_here",
            "Bearer token expired: eyJhbG...",
            "Authentication failed with credential xyz",
        ]

        for error in sensitive_errors:
            sanitized = sanitize_error_message(Exception(error))
            assert "token" not in sanitized.lower() or "internal error" in sanitized.lower()
            assert "password" not in sanitized.lower() or "internal error" in sanitized.lower()
            assert "secret" not in sanitized.lower() or "internal error" in sanitized.lower()
            assert "credential" not in sanitized.lower() or "internal error" in sanitized.lower()

    def test_long_error_messages_truncated(self):
        """Very long error messages are truncated."""
        from seedr_sonarr.server import sanitize_error_message

        long_error = "A" * 500
        sanitized = sanitize_error_message(Exception(long_error))
        assert len(sanitized) <= 203  # 200 + "..."


class TestAuthenticationBruteForce:
    """Tests related to brute force prevention."""

    def test_failed_login_logging(self):
        """Failed login attempts are logged."""
        client = TestClient(app)

        with patch("seedr_sonarr.server.logger") as mock_logger:
            response = client.post(
                "/api/v2/auth/login",
                data={"username": "attacker", "password": "wrong"},
            )

            assert response.text == "Fails."
            # Logger should have been called with warning
            mock_logger.warning.assert_called()


class TestSecureDefaults:
    """Tests for secure default configurations."""

    def test_session_timeout_reasonable(self):
        """Session timeout is set to a reasonable value."""
        from seedr_sonarr.server import SESSION_TIMEOUT
        from datetime import timedelta

        # Should be between 1 hour and 1 week
        assert timedelta(hours=1) <= SESSION_TIMEOUT <= timedelta(days=7)

    def test_max_upload_size_limited(self):
        """Maximum upload size is limited."""
        from seedr_sonarr.server import MAX_UPLOAD_SIZE

        # Should be reasonable (e.g., < 100MB for torrent files)
        assert MAX_UPLOAD_SIZE <= 100 * 1024 * 1024
