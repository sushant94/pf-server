"""Tests for HTTP endpoints.

Tests cover:
- auth_token() endpoint - OAuth code exchange and JWT issuance
- sync_tar() endpoint - Archive validation and extraction
"""

import base64
import io
import tarfile
from unittest.mock import patch, MagicMock, AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from pf_server.auth import create_jwt
from pf_server.user_context import _current_user


class TestAuthTokenEndpoint:
    """Tests for POST /auth/token endpoint."""

    def setup_method(self):
        """Reset user context before each test."""
        _current_user.set(None)

    def teardown_method(self):
        """Reset user context after each test."""
        _current_user.set(None)

    @pytest.mark.asyncio
    async def test_auth_token_success(
        self, mock_github_oauth, mock_docker_client, mock_docker_container
    ):
        """Successful OAuth code exchange returns JWT token."""
        from pf_server.main import app

        with (
            patch(
                "pf_server.main.exchange_github_code",
                mock_github_oauth["exchange_github_code"],
            ),
            patch(
                "pf_server.main.get_github_user", mock_github_oauth["get_github_user"]
            ),
            patch("pf_server.containers.client", mock_docker_client),
            patch(
                "pf_server.main.get_or_create_container",
                return_value=mock_docker_container,
            ),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/auth/token", json={"code": "test-oauth-code"}
                )

        assert response.status_code == 200
        data = response.json()
        assert "token" in data
        assert len(data["token"]) > 0

    @pytest.mark.asyncio
    async def test_auth_token_user_not_whitelisted(
        self, mock_github_oauth_not_whitelisted
    ):
        """User not in whitelist gets 403 Forbidden."""
        from pf_server.main import app

        with (
            patch(
                "pf_server.main.exchange_github_code",
                mock_github_oauth_not_whitelisted["exchange_github_code"],
            ),
            patch(
                "pf_server.main.get_github_user",
                mock_github_oauth_not_whitelisted["get_github_user"],
            ),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/auth/token", json={"code": "test-oauth-code"}
                )

        assert response.status_code == 403
        assert "whitelist" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_auth_token_github_code_exchange_fails(self):
        """Failed GitHub code exchange returns 400."""
        from pf_server.main import app

        mock_exchange = AsyncMock(side_effect=ValueError("Invalid code"))

        with patch("pf_server.main.exchange_github_code", mock_exchange):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post("/auth/token", json={"code": "bad-code"})

        assert response.status_code == 400
        assert "exchange code" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_auth_token_github_user_fetch_fails(self, mock_github_oauth):
        """Failed GitHub user info fetch returns 400."""
        from pf_server.main import app

        mock_user = AsyncMock(side_effect=ValueError("API error"))

        with (
            patch(
                "pf_server.main.exchange_github_code",
                mock_github_oauth["exchange_github_code"],
            ),
            patch("pf_server.main.get_github_user", mock_user),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post("/auth/token", json={"code": "test-code"})

        assert response.status_code == 400
        assert "user info" in response.json()["detail"].lower()


class TestAuthLoginEndpoint:
    """Tests for GET /auth/login endpoint."""

    @pytest.mark.asyncio
    async def test_auth_login_returns_github_url(self):
        """Login endpoint returns GitHub OAuth URL."""
        from pf_server.main import app

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/auth/login")

        assert response.status_code == 200
        data = response.json()
        assert "url" in data
        assert "github.com" in data["url"]
        assert "oauth" in data["url"].lower()

    @pytest.mark.asyncio
    async def test_auth_login_custom_redirect(self):
        """Login endpoint respects custom redirect_uri."""
        from urllib.parse import unquote
        from pf_server.main import app

        custom_redirect = "http://myapp.com/callback"
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(f"/auth/login?redirect_uri={custom_redirect}")

        assert response.status_code == 200
        data = response.json()
        # URL is URL-encoded, so decode to check
        assert custom_redirect in unquote(data["url"])


class TestSyncTarEndpoint:
    """Tests for POST /sync/tar endpoint."""

    def setup_method(self):
        """Reset user context before each test."""
        _current_user.set(None)

    def teardown_method(self):
        """Reset user context after each test."""
        _current_user.set(None)

    @staticmethod
    def _create_test_archive(files: dict[str, bytes]) -> str:
        """Create a base64-encoded tar.gz archive."""
        tar_buffer = io.BytesIO()
        with tarfile.open(fileobj=tar_buffer, mode="w:gz") as tar:
            for filename, content in files.items():
                info = tarfile.TarInfo(name=filename)
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
        tar_buffer.seek(0)
        return base64.b64encode(tar_buffer.read()).decode("ascii")

    def _create_tar_payload(self, files: dict[str, bytes]) -> dict:
        """Create a complete TarSyncPayload dict."""
        archive = self._create_test_archive(files)
        archive_bytes = base64.b64decode(archive)
        original_bytes = sum(len(content) for content in files.values())
        compressed_bytes = len(archive_bytes)
        return {
            "sessionId": "test-session",
            "generation": 0,
            "archiveBase64": archive,
            "fileCount": len(files),
            "originalBytes": original_bytes,
            "compressedBytes": compressed_bytes,
            "compressionRatio": original_bytes / compressed_bytes
            if compressed_bytes > 0
            else 1.0,
        }

    @pytest.mark.asyncio
    async def test_sync_tar_requires_auth(self):
        """Sync endpoint requires authentication."""
        from pf_server.main import app

        payload = self._create_tar_payload({"test.txt": b"content"})

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            # No token provided
            response = await client.post("/sync/tar", json=payload)

        assert response.status_code == 422  # Missing token parameter

    @pytest.mark.asyncio
    async def test_sync_tar_invalid_token(self):
        """Sync endpoint rejects invalid token."""
        from pf_server.main import app

        payload = self._create_tar_payload({"test.txt": b"content"})

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/sync/tar?token=invalid-token", json=payload)

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_sync_tar_archive_too_large(self, mock_docker_container, tmp_path):
        """Sync endpoint rejects oversized archives."""
        from pf_server.main import app, require_auth
        from pf_server.user_context import UserContext

        # Create payload
        payload = self._create_tar_payload({"test.txt": b"content"})

        # Create mock user for dependency override
        mock_user = MagicMock(spec=UserContext)
        mock_user.user_id = "12345"

        async def mock_require_auth(token: str = ""):
            return mock_user

        app.dependency_overrides[require_auth] = mock_require_auth

        try:
            # Mock the validation to return failure
            with (
                patch(
                    "pf_server.main.validate_archive_security",
                    return_value=(False, "Archive too large: 200MB"),
                ),
                patch(
                    "pf_server.main.get_or_create_container",
                    return_value=mock_docker_container,
                ),
            ):
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as client:
                    response = await client.post("/sync/tar?token=fake", json=payload)

            assert response.status_code == 200  # Returns 200 with error status in body
            data = response.json()
            assert data["status"] == "error"
            assert "too large" in data["message"].lower()
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_sync_tar_success_mounted_mode(self, mock_docker_container, tmp_path):
        """Successful tar sync in mounted mode."""
        from pf_server.main import app, require_auth
        from pf_server.user_context import UserContext

        payload = self._create_tar_payload({"test.txt": b"hello world"})
        payload["generation"] = 1

        # Mock user context to use tmp_path
        mock_user = MagicMock(spec=UserContext)
        mock_user.user_id = "12345"
        mock_user.host_user_repo_dir = tmp_path / "repo"
        mock_user.host_user_repo_dir.mkdir(parents=True, exist_ok=True)

        # Mock repo manager
        mock_repo = AsyncMock()
        mock_repo.do_init = AsyncMock(return_value="abc123")
        mock_user.repo = mock_repo

        async def mock_require_auth(token: str = ""):
            return mock_user

        app.dependency_overrides[require_auth] = mock_require_auth

        try:
            with (
                patch(
                    "pf_server.main.get_or_create_container",
                    return_value=mock_docker_container,
                ),
                patch(
                    "pf_server.main.extract_tar_to_mounted_volume",
                    return_value=(True, ["test.txt"], ""),
                ),
            ):
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as client:
                    response = await client.post("/sync/tar?token=fake", json=payload)

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "success"
            assert "test.txt" in data["data"]["syncedFiles"]
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_sync_tar_extraction_failure(self, mock_docker_container, tmp_path):
        """Extraction failure returns error response."""
        from pf_server.main import app, require_auth
        from pf_server.user_context import UserContext

        payload = self._create_tar_payload({"test.txt": b"content"})

        mock_user = MagicMock(spec=UserContext)
        mock_user.user_id = "12345"
        mock_user.host_user_repo_dir = tmp_path / "repo"

        async def mock_require_auth(token: str = ""):
            return mock_user

        app.dependency_overrides[require_auth] = mock_require_auth

        try:
            with (
                patch(
                    "pf_server.main.get_or_create_container",
                    return_value=mock_docker_container,
                ),
                patch(
                    "pf_server.main.extract_tar_to_mounted_volume",
                    return_value=(False, [], "Permission denied"),
                ),
            ):
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as client:
                    response = await client.post("/sync/tar?token=fake", json=payload)

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "error"
            assert "permission denied" in data["message"].lower()
        finally:
            app.dependency_overrides.clear()


class TestRequireAuthDependency:
    """Tests for the require_auth FastAPI dependency."""

    def setup_method(self):
        _current_user.set(None)

    def teardown_method(self):
        _current_user.set(None)

    @pytest.mark.asyncio
    async def test_require_auth_valid_token(self):
        """Valid token passes auth dependency."""
        from pf_server.main import require_auth

        token = create_jwt({"sub": "12345", "login": "testuser"})
        user = await require_auth(token=token)

        assert user is not None
        assert user.user_id == "12345"

    @pytest.mark.asyncio
    async def test_require_auth_invalid_token_raises(self):
        """Invalid token raises HTTPException."""
        from fastapi import HTTPException
        from pf_server.main import require_auth

        with pytest.raises(HTTPException) as exc_info:
            await require_auth(token="bad-token")

        assert exc_info.value.status_code == 401
