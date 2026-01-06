import asyncio

import websockets
from fastapi import WebSocket
from websockets.exceptions import ConnectionClosed


async def proxy_websocket(client_ws: WebSocket, container_url: str):
    """Bidirectional WebSocket proxy between client and container."""
    async with websockets.connect(container_url) as container_ws:

        async def client_to_container():
            try:
                async for msg in client_ws.iter_text():
                    await container_ws.send(msg)
            except Exception:
                pass

        async def container_to_client():
            try:
                async for msg in container_ws:
                    await client_ws.send_text(msg)
            except (ConnectionClosed, Exception):
                pass

        # Run both directions concurrently, exit when either closes
        done, pending = await asyncio.wait(
            [
                asyncio.create_task(client_to_container()),
                asyncio.create_task(container_to_client()),
            ],
            return_when=asyncio.FIRST_COMPLETED,
        )

        # Cancel pending tasks
        for task in pending:
            task.cancel()
