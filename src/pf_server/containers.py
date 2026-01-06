import docker
from docker.models.containers import Container

from .config import settings

client = docker.from_env()


def get_or_create_container(user_id: int) -> Container:
    """Get existing container or create a new one for the user."""
    name = f"pf-user-{user_id}"

    # Try to find existing container by name
    # Using list() with exact name filter is more reliable than get()
    existing = client.containers.list(all=True, filters={"name": f"^/{name}$"})
    if existing:
        container = existing[0]
        if container.status != "running":
            container.start()
        return container

    # Create new container
    container = client.containers.run(
        settings.container_image,
        name=name,
        detach=True,
        network=settings.container_network,
    )
    return container


def get_container_ws_url(user_id: int) -> str:
    """Get WebSocket URL for user's container."""
    container = get_or_create_container(user_id)
    # Container listens on port 8000 internally
    return f"ws://{container.name}:8000/ws"
