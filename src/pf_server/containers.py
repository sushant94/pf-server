import asyncio
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from docker.models.containers import Container

import docker
from pf_server.user_context import get_current_user

from .config import settings
from .logging_config import get_logger
from .opencode_client import OpenCodeClient
from .opencode_manager import (
    CONTAINER_INTERNAL_PORT,
    get_manager as get_opencode_manager,
)

client = docker.from_env()

logger = get_logger(__name__)

# Track port mappings for containers (container_id -> host_port)
_container_ports: dict[str, int] = {}


@dataclass
class ExecResult:
    """Result of a docker exec command with log streaming."""

    exit_code: int
    output: str


def is_progress_log(
    entry: dict,
    event_prefixes: set[str] | None = None,
    event_names: set[str] | None = None,
) -> bool:
    """Check if log entry's event matches progress criteria.

    Args:
        entry: Parsed JSON log entry with an "event" field.
        event_prefixes: Event name prefixes to match (e.g., {"agent_"} matches "agent_step").
        event_names: Exact event names to match.

    Returns:
        True if the entry's "event" value matches any criteria.
    """
    event = entry.get("event", "")
    if not event:
        return False
    if event_prefixes and any(event.startswith(p) for p in event_prefixes):
        return True
    if event_names and event in event_names:
        return True
    return False


async def exec_with_log_streaming(
    container: Container,
    cmd: str,
    workdir: str | None = None,
    progress_filter: Callable[[dict], bool] | None = None,
    marker: str | None = None,
    environment: dict[str, str] | None = None,
) -> ExecResult:
    """Execute a command in container with real-time log streaming.

    Runs the command with stdout/stderr redirected to a temp file, polls for
    new log entries at intervals, and forwards them to our logger.

    Args:
        container: Docker container to execute in.
        cmd: Shell command to execute (will be wrapped in bash -c).
        workdir: Working directory inside container.
        progress_filter: Optional function to filter which log entries to forward.
                        If None, all JSON log entries are forwarded.
        marker: Optional marker for log correlation (used in log output).
        environment: Optional environment variables to set for this command.
                    CURRENT_PROJECT_DIR is automatically set to workdir if not provided.

    Returns:
        ExecResult with exit_code and captured output.
    """
    exec_id = uuid.uuid4().hex[:12]
    log_file = f"/tmp/pf_exec_{exec_id}.log"
    correlation_marker = marker or exec_id

    # Build environment with CURRENT_PROJECT_DIR set to workdir
    exec_env = environment.copy() if environment else {}
    if workdir and "CURRENT_PROJECT_DIR" not in exec_env:
        exec_env["CURRENT_PROJECT_DIR"] = workdir

    # Wrap command to redirect output to log file
    full_cmd = f"{cmd} > {log_file} 2>&1"

    logger.debug(
        "exec_streaming_start",
        exec_id=exec_id,
        marker=correlation_marker,
        log_file=log_file,
    )

    # Start exec in background task
    exec_task = asyncio.create_task(
        asyncio.to_thread(
            container.exec_run,
            cmd=["bash", "-c", full_cmd],
            workdir=workdir,
            environment=exec_env if exec_env else None,
        )
    )

    # Poll for progress while exec runs
    last_line = 0
    while not exec_task.done():
        await asyncio.sleep(settings.docker_log_poll_interval)
        last_line = await _poll_and_forward_logs(
            container, log_file, correlation_marker, last_line, progress_filter
        )

    result = await exec_task

    # Read full output from log file
    output = ""
    log_result = await asyncio.to_thread(container.exec_run, cmd=["cat", log_file])
    if log_result.exit_code == 0 and log_result.output:
        output = log_result.output.decode()

    # Cleanup temp log file
    await asyncio.to_thread(container.exec_run, cmd=["rm", "-f", log_file])

    logger.debug(
        "exec_streaming_complete",
        exec_id=exec_id,
        marker=correlation_marker,
        exit_code=result.exit_code,
    )

    return ExecResult(exit_code=result.exit_code, output=output)


async def _poll_and_forward_logs(
    container: Container,
    log_file: str,
    marker: str,
    last_line: int,
    progress_filter: Callable[[dict], bool] | None,
) -> int:
    """Read new lines from log file and forward to logger.

    Args:
        container: Docker container to read from.
        log_file: Path to log file inside container.
        marker: Marker for log correlation.
        last_line: Last line number read (0-indexed).
        progress_filter: Optional filter function. If None, forward all JSON entries.

    Returns:
        Updated last line number after reading.
    """
    result = await asyncio.to_thread(
        container.exec_run,
        cmd=["tail", "-n", f"+{last_line + 1}", log_file],
    )
    if result.exit_code != 0 or not result.output:
        return last_line

    lines = result.output.decode().strip().split("\n")
    for line in lines:
        if not line:
            continue
        try:
            entry = json.loads(line)
            # Forward if no filter (transparent mode) or filter returns True
            if progress_filter is None or progress_filter(entry):
                # Rename 'event' to 'docker_event' to avoid conflict with structlog's event arg
                log_context = {k: v for k, v in entry.items() if k != "event"}
                log_context["docker_event"] = entry.get("event")
                logger.info(
                    "docker_exec_progress",
                    source="docker_exec",
                    marker=marker,
                    **log_context,
                )
        except json.JSONDecodeError:
            # Non-JSON line - forward as raw message in transparent mode
            if progress_filter is None:
                logger.info(
                    "docker_exec_output", source="docker_exec", marker=marker, raw=line
                )

    return last_line + len(lines)


def _do_first_time_setup() -> Path:
    # Create user data directory if it doesn't exist
    user = get_current_user()
    user.create_dirs()
    return user.host_user_dir


def get_or_create_container() -> Container:
    """Get existing container or create a new one for the user.

    Container lifecycle:
    - One container per user (named pf-user-{user_id})
    - Containers are long-lived with 'sleep infinity' command
    - Stopped containers are restarted, not recreated
    - New containers get pf-tools installed and pf init run
    - Containers without OpenCode port mapping are removed and recreated
    """
    user = get_current_user()
    name = f"pf-user-{user.user_id}"

    logger.debug("container_lookup", container_name=name)

    # Try to find existing container by name
    # Using list() with exact name filter is more reliable than get()
    existing = client.containers.list(all=True, filters={"name": f"^/{name}$"})
    if existing:
        container = existing[0]
        logger.debug("container_found", container_name=name, status=container.status)
        if container.status != "running":
            logger.debug("container_starting", container_name=name)
            container.start()
            logger.info("container_started", container_name=name)

        # Ensure port mapping is tracked (may be missing if container was created before tracking)
        container_id: str = container.id  # type: ignore[assignment]
        if container_id not in _container_ports:
            # Look up port from Docker's port mappings
            container.reload()  # Refresh to get latest port bindings
            ports = container.attrs.get("NetworkSettings", {}).get("Ports", {})
            opencode_port_key = f"{CONTAINER_INTERNAL_PORT}/tcp"
            if opencode_port_key in ports and ports[opencode_port_key]:
                host_port = int(ports[opencode_port_key][0]["HostPort"])
                _container_ports[container_id] = host_port
                logger.info(
                    "container_port_recovered",
                    container_name=name,
                    container_id=container_id[:12],
                    host_port=host_port,
                )
                # OpenCode server will be started by ensure_opencode_ready()
                return container
            else:
                # Container was created before port mapping was added
                # Need to recreate the container
                logger.warning(
                    "container_missing_opencode_port",
                    container_name=name,
                    container_id=container_id[:12],
                    message="Container was created without OpenCode port mapping. Removing old container.",
                )
                container.stop()
                container.remove()
                logger.info(
                    "container_removed_for_recreation",
                    container_name=name,
                    container_id=container_id[:12],
                )
                # Fall through to create a new container with proper port mapping
        else:
            # Port already tracked, OpenCode server will be started by ensure_opencode_ready()
            return container

    # This is the first time we're encountering this user, or container was removed for recreation
    # TODO: In the future, this will probably be moved to a separate user management module
    _user_data_dir = _do_first_time_setup()

    # Allocate a host port for OpenCode server
    opencode_manager = get_opencode_manager()
    host_port = opencode_manager.allocate_port_for_container()

    logger.info(
        "container_creating",
        container_name=name,
        image=settings.container_image,
        data_dir=str(user.host_mount_dir),
        opencode_port=host_port,
    )

    start_time = time.perf_counter()

    # Create new container with port mapping for OpenCode
    container = client.containers.run(
        settings.container_image,
        command="sleep infinity",  # Keep container running
        name=name,
        detach=True,
        network=settings.container_network,
        extra_hosts={"host.docker.internal": "host-gateway"},
        ports={f"{CONTAINER_INTERNAL_PORT}/tcp": host_port},
        environment={
            "CONTEXT7_API_KEY": settings.context7_api_key,
            "CURRENT_PROJECT_DIR": str(settings.docker_base_cwd),
        },
        volumes={
            str(user.host_mount_dir): {
                "bind": str(settings.docker_base_cwd),
                "mode": "rw",
            },
            str(settings.host_pf_tools_directory): {
                "bind": str(settings.docker_pf_tools_directory),
                "mode": "rw",
            },
            str(settings.host_opencode_directory): {
                "bind": str(settings.docker_opencode_directory),
                "mode": "rw",  # Needs write access for bun install to create node_modules
            },
        },
    )

    # Track the port mapping
    container_id_str: str = container.id  # type: ignore[assignment]
    _container_ports[container_id_str] = host_port

    # Verify container is running before exec
    container.reload()
    if container.status != "running":
        logs = container.logs().decode()
        logger.error(
            "container_start_failed",
            container_name=name,
            status=container.status,
            logs=logs,
        )
        raise RuntimeError(
            f"Container failed to start (status={container.status}): {logs}"
        )

    container_id = container.short_id
    logger.info("container_created", container_name=name, container_id=container_id)

    # Install pf-tools in editable mode
    logger.debug("pf_tools_installing", container_name=name)
    result = container.exec_run(
        cmd=["pip", "install", "-e", "."],
        workdir=str(settings.docker_pf_tools_directory),
    )
    if result.exit_code != 0:
        error_msg = result.output.decode()
        logger.error(
            "pf_tools_install_failed",
            container_name=name,
            exit_code=result.exit_code,
            error=error_msg,
        )
        raise RuntimeError(f"Failed to install pf-tools: {error_msg}")
    logger.info("pf_tools_installed", container_name=name)

    # Run pf-init in the user directory
    logger.debug("pf_init_running", container_name=name)
    result = container.exec_run(
        cmd=["pf", "init"],
        workdir=str(user.docker_shadow_dir),
    )
    if result.exit_code != 0:
        error_msg = result.output.decode()
        logger.error(
            "pf_init_failed",
            container_name=name,
            exit_code=result.exit_code,
            error=error_msg,
        )
        raise RuntimeError(f"Failed to run pf-init: {error_msg}")

    # Setup opencode: install dependencies from monorepo root, then link from package dir
    opencode_root_dir = str(settings.docker_opencode_directory)
    opencode_pkg_dir = str(settings.docker_opencode_directory / "packages" / "opencode")

    logger.debug("opencode_installing", container_name=name)
    result = container.exec_run(
        cmd=["bun", "install"],
        workdir=opencode_root_dir,  # Must run from monorepo root for workspace deps
    )
    if result.exit_code != 0:
        error_msg = result.output.decode()
        logger.error(
            "opencode_install_failed",
            container_name=name,
            exit_code=result.exit_code,
            error=error_msg,
        )
        raise RuntimeError(f"Failed to install opencode dependencies: {error_msg}")
    logger.info("opencode_installed", container_name=name)

    logger.debug("opencode_linking", container_name=name)
    result = container.exec_run(
        cmd=["bun", "link"],
        workdir=opencode_pkg_dir,
    )
    if result.exit_code != 0:
        error_msg = result.output.decode()
        logger.error(
            "opencode_link_failed",
            container_name=name,
            exit_code=result.exit_code,
            error=error_msg,
        )
        raise RuntimeError(f"Failed to link opencode: {error_msg}")
    logger.info("opencode_linked", container_name=name)

    # OpenCode server will be started by ensure_opencode_ready() after this returns

    duration_ms = int((time.perf_counter() - start_time) * 1000)
    logger.info(
        "container_created_ready_for_opencode",
        container_name=name,
        container_id=container_id,
        opencode_port=host_port,
        duration_ms=duration_ms,
    )

    return container


def get_container_opencode_port(container_id: str) -> int | None:
    """Get the host port mapped for OpenCode for a container.

    Args:
        container_id: Docker container ID

    Returns:
        Host port number or None if not found
    """
    return _container_ports.get(container_id)


async def ensure_opencode_ready(container: Container) -> OpenCodeClient:
    """Ensure OpenCode server is running for a container and return client.

    This starts the OpenCode server if not running and registers it with
    the OpenCodeManager. Call this after get_or_create_container().

    Args:
        container: Docker container with port mapping configured

    Returns:
        OpenCodeClient connected to the server

    Raises:
        RuntimeError: If no port allocated or server fails to start
    """
    container_id: str = container.id  # type: ignore[assignment]
    host_port = get_container_opencode_port(container_id)
    if host_port is None:
        raise RuntimeError(
            f"No port allocated for container {container_id[:12]}. "
            "Container may have been created before port tracking was added."
        )

    manager = get_opencode_manager()
    return await manager.start_server(container, host_port)


async def get_container_and_client() -> tuple[Container, OpenCodeClient]:
    """Get or create container with OpenCode server ready.

    This is the primary entry point for code that needs to interact with
    OpenCode inside a container. It handles both container creation and
    OpenCode server lifecycle in one call.

    Returns:
        Tuple of (container, opencode_client)

    Raises:
        RuntimeError: If container or server fails to start
    """
    container = get_or_create_container()
    client = await ensure_opencode_ready(container)
    return container, client
