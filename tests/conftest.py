"""Pytest configuration and shared fixtures.

This module sets up test environment variables BEFORE any pf_server imports,
which is critical because pf_server.config.Settings is instantiated at module load time.
"""

import os
from unittest.mock import MagicMock, AsyncMock

import pytest

# Set test environment variables before any pf_server imports
# These must be set before pytest collects tests to avoid validation errors
os.environ.setdefault("GITHUB_CLIENT_ID", "test-client-id")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-client-secret")
# JWT_SECRET must be at least 32 characters for HS256 security validation
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-for-testing-only")
os.environ.setdefault("ALLOWED_GITHUB_IDS", "12345,67890")


@pytest.fixture
def mock_docker_container():
    """Create a mock Docker container for testing.

    The mock container provides controllable behavior for:
    - exec_run(): Returns configurable exit codes and output
    - status: Can be set to 'running', 'exited', etc.
    - start(): No-op by default
    - reload(): No-op by default
    - logs(): Returns empty bytes by default
    - name: Returns 'mock-container'
    """
    container = MagicMock()
    container.name = "mock-container"
    container.status = "running"
    container.start = MagicMock()
    container.reload = MagicMock()
    container.logs = MagicMock(return_value=b"")

    # Default exec_run returns success
    exec_result = MagicMock()
    exec_result.exit_code = 0
    exec_result.output = b"Success"
    container.exec_run = MagicMock(return_value=exec_result)

    return container


@pytest.fixture
def mock_docker_client(mock_docker_container):
    """Create a mock Docker client for testing.

    Returns a mock that can be patched into pf_server.containers.client.
    """
    client = MagicMock()

    # containers.list returns empty by default (no existing container)
    client.containers.list = MagicMock(return_value=[])

    # containers.run returns the mock container
    client.containers.run = MagicMock(return_value=mock_docker_container)

    return client


@pytest.fixture
def mock_github_oauth():
    """Create mock functions for GitHub OAuth flow.

    Returns a dict with:
    - exchange_github_code: AsyncMock returning a fake GitHub token
    - get_github_user: AsyncMock returning fake user info
    """
    return {
        "exchange_github_code": AsyncMock(return_value="fake-github-token"),
        "get_github_user": AsyncMock(
            return_value={
                "id": 12345,  # In allowed list
                "login": "testuser",
                "name": "Test User",
            }
        ),
    }


@pytest.fixture
def mock_github_oauth_not_whitelisted():
    """Mock GitHub OAuth that returns a user NOT in the whitelist."""
    return {
        "exchange_github_code": AsyncMock(return_value="fake-github-token"),
        "get_github_user": AsyncMock(
            return_value={
                "id": 99999,  # Not in allowed list
                "login": "notallowed",
                "name": "Not Allowed User",
            }
        ),
    }
