"""Tests for OpenCode server lifecycle management.

Tests cover:
- Port allocation
- Server startup and health monitoring
- Client retrieval
- Server cleanup
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestOpenCodeManagerPortAllocation:
    """Tests for port allocation."""

    def test_port_allocation_is_unique(self):
        """Each allocation returns a unique port."""
        from pf_server.opencode_manager import OpenCodeManager

        manager = OpenCodeManager()

        ports = [manager.allocate_port_for_container() for _ in range(5)]

        assert len(set(ports)) == 5  # All unique

    def test_port_allocation_within_range(self):
        """Allocated ports are within configured range."""
        from pf_server.opencode_manager import (
            PORT_RANGE_END,
            PORT_RANGE_START,
            OpenCodeManager,
        )

        manager = OpenCodeManager()

        for _ in range(10):
            port = manager.allocate_port_for_container()
            assert PORT_RANGE_START <= port < PORT_RANGE_END

    def test_released_port_can_be_reallocated(self):
        """Released ports return to the pool."""
        from pf_server.opencode_manager import OpenCodeManager

        manager = OpenCodeManager()

        # Exhaust all ports except one
        allocated = []
        while len(manager._available_ports) > 1:
            allocated.append(manager._allocate_port())

        # Allocate last port
        last_port = manager._allocate_port()

        # Release it
        manager._release_port(last_port)

        # Should get it back since it's the only one available
        port2 = manager._allocate_port()

        assert last_port == port2  # Same port reused

    def test_port_exhaustion_raises_error(self):
        """RuntimeError raised when no ports available."""
        from pf_server.opencode_manager import OpenCodeManager

        manager = OpenCodeManager()
        manager._available_ports.clear()

        with pytest.raises(RuntimeError, match="No available ports"):
            manager._allocate_port()


class TestOpenCodeManagerServerLifecycle:
    """Tests for server start/stop."""

    @pytest.mark.asyncio
    async def test_start_server_returns_client(self):
        """start_server returns an OpenCodeClient."""
        from pf_server.opencode_manager import OpenCodeManager

        manager = OpenCodeManager()
        mock_container = MagicMock()
        mock_container.id = "test-container-id"

        # Mock exec_run for server start
        mock_result = MagicMock()
        mock_result.exit_code = 0
        mock_container.exec_run = MagicMock(return_value=mock_result)

        with patch(
            "pf_server.opencode_manager.OpenCodeClient"
        ) as mock_client_class:
            mock_client = AsyncMock()
            mock_client.health_check = AsyncMock(return_value=True)
            mock_client_class.return_value = mock_client

            client = await manager.start_server(mock_container, 5000)

        assert client is mock_client
        assert manager.has_server("test-container-id")

    @pytest.mark.asyncio
    async def test_start_server_reuses_existing(self):
        """start_server reuses existing healthy server."""
        from pf_server.opencode_manager import OpenCodeManager, OpenCodeServer
        from pf_server.opencode_client import OpenCodeClient

        manager = OpenCodeManager()
        mock_container = MagicMock()
        mock_container.id = "test-container-id"

        # Create existing healthy server
        existing_client = AsyncMock(spec=OpenCodeClient)
        existing_client.health_check = AsyncMock(return_value=True)
        server = OpenCodeServer(
            container_id="test-container-id",
            host_port=5000,
            client=existing_client,
        )
        manager._servers["test-container-id"] = server

        client = await manager.start_server(mock_container, 5000)

        assert client is existing_client

    @pytest.mark.asyncio
    async def test_stop_server_releases_port(self):
        """stop_server releases port back to pool."""
        from pf_server.opencode_manager import OpenCodeManager, OpenCodeServer, PORT_RANGE_START
        from pf_server.opencode_client import OpenCodeClient

        manager = OpenCodeManager()
        test_port = PORT_RANGE_START  # Use a port within the valid range

        # Create existing server
        mock_client = AsyncMock(spec=OpenCodeClient)
        server = OpenCodeServer(
            container_id="test-container-id",
            host_port=test_port,
            client=mock_client,
        )
        manager._servers["test-container-id"] = server
        manager._available_ports.discard(test_port)  # Port is in use

        await manager.stop_server("test-container-id")

        assert "test-container-id" not in manager._servers
        assert test_port in manager._available_ports
        mock_client.close.assert_called_once()


class TestOpenCodeManagerClientRetrieval:
    """Tests for get_client."""

    @pytest.mark.asyncio
    async def test_get_client_returns_existing_client(self):
        """get_client returns client for existing server."""
        from pf_server.opencode_manager import OpenCodeManager, OpenCodeServer
        from pf_server.opencode_client import OpenCodeClient

        manager = OpenCodeManager()

        mock_client = AsyncMock(spec=OpenCodeClient)
        server = OpenCodeServer(
            container_id="test-container-id",
            host_port=5000,
            client=mock_client,
        )
        manager._servers["test-container-id"] = server

        client = await manager.get_client("test-container-id")

        assert client is mock_client

    @pytest.mark.asyncio
    async def test_get_client_raises_when_no_server(self):
        """get_client raises RuntimeError when no server exists."""
        from pf_server.opencode_manager import OpenCodeManager

        manager = OpenCodeManager()

        with pytest.raises(RuntimeError, match="No OpenCode server"):
            await manager.get_client("nonexistent-container")


class TestOpenCodeManagerCleanup:
    """Tests for cleanup methods."""

    @pytest.mark.asyncio
    async def test_cleanup_idle_servers(self):
        """cleanup_idle_servers removes idle servers."""
        from pf_server.opencode_manager import OpenCodeManager, OpenCodeServer, PORT_RANGE_START
        from pf_server.opencode_client import OpenCodeClient
        import time

        manager = OpenCodeManager()
        test_port = PORT_RANGE_START  # Use a port within the valid range

        # Create an idle server (old last_used)
        mock_client = AsyncMock(spec=OpenCodeClient)
        server = OpenCodeServer(
            container_id="test-container-id",
            host_port=test_port,
            client=mock_client,
        )
        server.last_used = time.time() - 1000  # Very old
        manager._servers["test-container-id"] = server
        manager._available_ports.discard(test_port)

        count = await manager.cleanup_idle_servers(timeout=1)

        assert count == 1
        assert "test-container-id" not in manager._servers
        assert test_port in manager._available_ports

    @pytest.mark.asyncio
    async def test_shutdown_all_clears_everything(self):
        """shutdown_all closes all servers."""
        from pf_server.opencode_manager import OpenCodeManager, OpenCodeServer, PORT_RANGE_START
        from pf_server.opencode_client import OpenCodeClient

        manager = OpenCodeManager()
        test_ports = [PORT_RANGE_START, PORT_RANGE_START + 1]

        # Add two servers
        for i, port in enumerate(test_ports):
            mock_client = AsyncMock(spec=OpenCodeClient)
            server = OpenCodeServer(
                container_id=f"container-{i}",
                host_port=port,
                client=mock_client,
            )
            manager._servers[f"container-{i}"] = server
            manager._available_ports.discard(port)

        await manager.shutdown_all()

        assert len(manager._servers) == 0
        assert test_ports[0] in manager._available_ports
        assert test_ports[1] in manager._available_ports


class TestGetManager:
    """Tests for get_manager singleton."""

    def test_get_manager_returns_singleton(self):
        """get_manager returns same instance."""
        from pf_server.opencode_manager import get_manager

        manager1 = get_manager()
        manager2 = get_manager()

        assert manager1 is manager2
