"""Tests for MCP server integration.

These tests verify:
- MCP endpoint is accessible and requires auth
"""

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture(scope="module")
def event_loop():
    """Create event loop for module-scoped async fixtures."""
    import asyncio

    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="module")
async def mcp_client():
    """Module-scoped client with lifespan managed once."""
    from asgi_lifespan import LifespanManager
    from pf_server.main import app

    async with LifespanManager(app) as manager:
        async with AsyncClient(
            transport=ASGITransport(app=manager.app),
            base_url="http://test",
        ) as client:
            yield client


def mcp_headers(token: str | None = None) -> dict:
    """Build headers required for MCP Streamable HTTP requests."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


class TestMCPEndpointAuth:
    """Tests that MCP endpoint enforces authentication."""

    @pytest.mark.asyncio
    async def test_mcp_rejects_request_without_auth(self, mcp_client):
        """MCP endpoint returns 401 without Authorization header."""
        response = await mcp_client.post(
            "/mcp/mcp",
            headers=mcp_headers(),
            json={"jsonrpc": "2.0", "method": "initialize", "id": 1},
        )

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_mcp_rejects_invalid_token(self, mcp_client):
        """MCP endpoint returns 401 with invalid token."""
        response = await mcp_client.post(
            "/mcp/mcp",
            headers=mcp_headers("invalid.token.here"),
            json={"jsonrpc": "2.0", "method": "initialize", "id": 1},
        )

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_mcp_accepts_valid_token(self, mcp_client):
        """MCP endpoint accepts request with valid token."""
        from pf_server.auth import create_jwt

        token = create_jwt({"sub": "test_user_123", "login": "testuser"})

        response = await mcp_client.post(
            "/mcp/mcp",
            headers=mcp_headers(token),
            json={
                "jsonrpc": "2.0",
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test-client", "version": "1.0.0"},
                },
                "id": 1,
            },
        )

        assert response.status_code == 200
        assert "mcp-session-id" in response.headers
