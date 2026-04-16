"""OpenCode server lifecycle management.

Manages OpenCode servers running inside Docker containers, handling:
- Port allocation and mapping
- Server startup and health monitoring
- Client retrieval
- Cleanup on shutdown
"""

import asyncio
import time
from dataclasses import dataclass, field

from docker.models.containers import Container

from .logging_config import get_logger
from .opencode_client import OpenCodeClient

logger = get_logger(__name__)

# Port range for host-side port mapping (mapped to container's 4096)
# Using 15000+ to avoid conflicts with common services (5000 is AirPlay on macOS)
PORT_RANGE_START = 15000
PORT_RANGE_END = 15100  # Max 100 concurrent containers

# Internal port that OpenCode listens on inside containers
CONTAINER_INTERNAL_PORT = 4096

# Idle timeout in seconds (for cleanup purposes)
IDLE_TIMEOUT_SECONDS = 300  # 5 minutes


@dataclass
class OpenCodeServer:
    """Tracks a running OpenCode server for a container."""

    container_id: str
    host_port: int
    client: OpenCodeClient
    started_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)

    def touch(self) -> None:
        """Update last_used timestamp."""
        self.last_used = time.time()

    def is_idle(self, timeout: float = IDLE_TIMEOUT_SECONDS) -> bool:
        """Check if server has been idle longer than timeout."""
        return (time.time() - self.last_used) > timeout


class OpenCodeManager:
    """Manages OpenCode servers across containers.

    Each container runs its own OpenCode server on internal port 4096.
    The manager allocates host ports for accessing these servers from
    the pf-server host.

    Usage:
        manager = get_manager()

        # Start server when container is created
        client = await manager.start_server(container)

        # Get client for existing container
        client = await manager.get_client(container_id)

        # Stop server when container is removed
        await manager.stop_server(container_id)
    """

    def __init__(self) -> None:
        self._servers: dict[str, OpenCodeServer] = {}  # container_id -> server
        self._available_ports: set[int] = set(range(PORT_RANGE_START, PORT_RANGE_END))
        self._lock = asyncio.Lock()

    def _allocate_port(self) -> int:
        """Allocate an available host port.

        Returns:
            Available port number

        Raises:
            RuntimeError: If no ports are available
        """
        if not self._available_ports:
            raise RuntimeError("No available ports for OpenCode server")
        return self._available_ports.pop()

    def _release_port(self, port: int) -> None:
        """Release a port back to the pool."""
        if PORT_RANGE_START <= port < PORT_RANGE_END:
            self._available_ports.add(port)

    def allocate_port_for_container(self) -> int:
        """Allocate a port for a new container (called during container creation).

        Returns:
            Host port to map to container's internal port 4096
        """
        return self._allocate_port()

    async def get_client(self, container_id: str) -> OpenCodeClient:
        """Get client for a container.

        Args:
            container_id: Docker container ID

        Returns:
            OpenCodeClient for the container

        Raises:
            RuntimeError: If no server exists for the container
        """
        async with self._lock:
            server = self._servers.get(container_id)
            if not server:
                raise RuntimeError(f"No OpenCode server for container {container_id}")
            server.touch()
            return server.client

    async def start_server(
        self,
        container: Container,
        host_port: int,
    ) -> OpenCodeClient:
        """Start OpenCode server in a container and return client.

        The container must already have port mapping configured.
        This method starts the server process inside the container
        and waits for it to become healthy.

        Args:
            container: Docker container with port mapping configured
            host_port: Host port mapped to container's 4096

        Returns:
            OpenCodeClient connected to the server

        Raises:
            RuntimeError: If server fails to start
        """
        async with self._lock:
            container_id: str = container.id  # type: ignore[assignment]

            # Check if already running
            if container_id in self._servers:
                server = self._servers[container_id]
                if await server.client.health_check():
                    server.touch()
                    return server.client
                else:
                    # Server died, clean up
                    logger.warning(
                        "opencode_server_died_restarting",
                        container_id=container_id[:12],
                    )
                    await server.client.close()
                    del self._servers[container_id]

            # Start the server process inside the container
            await self._start_server_process(container, host_port)

            # Create client
            base_url = f"http://localhost:{host_port}"
            client = OpenCodeClient(base_url)

            # Wait for server to be healthy
            for attempt in range(10):
                if await client.health_check():
                    break
                await asyncio.sleep(1)
            else:
                await client.close()
                raise RuntimeError(
                    f"OpenCode server failed to start on port {host_port}"
                )

            # Track the server
            server = OpenCodeServer(
                container_id=container_id,
                host_port=host_port,
                client=client,
            )
            self._servers[container_id] = server

            logger.info(
                "opencode_server_started",
                container_id=container_id[:12],
                host_port=host_port,
            )

            return client

    async def _start_server_process(
        self,
        container: Container,
        _host_port: int,
    ) -> None:
        """Start the OpenCode server process inside the container.

        Args:
            container: Docker container
            _host_port: Host port (unused, server listens on internal 4096)
        """
        # Build the start command
        # Server runs in /opencode directory where bun workspace is set up
        cmd = f"nohup bun dev serve --port {CONTAINER_INTERNAL_PORT} > /tmp/opencode-server.log 2>&1 &"

        container_id: str = container.id  # type: ignore[assignment]
        result = await asyncio.to_thread(
            container.exec_run,
            cmd=["bash", "-c", cmd],
            workdir="/opencode",
            detach=False,  # Wait for bash to return (immediately due to &)
        )

        if result.exit_code != 0:
            error_msg = result.output.decode() if result.output else "Unknown error"
            logger.error(
                "opencode_server_start_failed",
                container_id=container_id[:12],
                error=error_msg,
            )
            raise RuntimeError(f"Failed to start OpenCode server: {error_msg}")

        # Brief wait for server to initialize
        await asyncio.sleep(2)

    async def stop_server(self, container_id: str) -> None:
        """Stop server and release resources.

        Args:
            container_id: Docker container ID
        """
        async with self._lock:
            server = self._servers.pop(container_id, None)
            if server:
                self._release_port(server.host_port)
                await server.client.close()
                logger.info(
                    "opencode_server_stopped",
                    container_id=container_id[:12],
                    host_port=server.host_port,
                )

    async def cleanup_idle_servers(
        self,
        timeout: float = IDLE_TIMEOUT_SECONDS,
    ) -> int:
        """Clean up servers that have been idle too long.

        Args:
            timeout: Idle timeout in seconds

        Returns:
            Number of servers cleaned up
        """
        async with self._lock:
            to_remove = [
                (cid, server)
                for cid, server in self._servers.items()
                if server.is_idle(timeout)
            ]

            for container_id, server in to_remove:
                self._release_port(server.host_port)
                await server.client.close()
                del self._servers[container_id]

                logger.info(
                    "opencode_server_idle_cleanup",
                    container_id=container_id[:12],
                    host_port=server.host_port,
                    idle_seconds=time.time() - server.last_used,
                )

            return len(to_remove)

    async def shutdown_all(self) -> None:
        """Shutdown all managed servers."""
        async with self._lock:
            for container_id, server in self._servers.items():
                await server.client.close()
                self._release_port(server.host_port)
                logger.info(
                    "opencode_server_shutdown",
                    container_id=container_id[:12],
                    host_port=server.host_port,
                )

            self._servers.clear()
            self._available_ports = set(range(PORT_RANGE_START, PORT_RANGE_END))

    def get_server_count(self) -> int:
        """Get number of active servers."""
        return len(self._servers)

    def has_server(self, container_id: str) -> bool:
        """Check if a server exists for a container."""
        return container_id in self._servers


# Global manager singleton
_manager: OpenCodeManager | None = None


def get_manager() -> OpenCodeManager:
    """Get or create the global OpenCode manager."""
    global _manager
    if _manager is None:
        _manager = OpenCodeManager()
    return _manager
