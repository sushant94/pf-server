"""SSE Event Listener for OpenCode events.

This module provides a background task that listens for Server-Sent Events
from OpenCode and dispatches them to registered handlers.
"""

import asyncio
from typing import Awaitable, Callable

from fastapi import WebSocket

from pf_server.logging_config import get_logger
from pf_server.opencode_client import OpenCodeClient

logger = get_logger(__name__)


class SSEEventListener:
    """Listen for OpenCode SSE events and route to handlers.

    This class manages a background task that streams events from OpenCode
    and dispatches them to registered event handlers.

    Example:
        listener = SSEEventListener(opencode_client, "/path/to/repo", websocket)

        async def on_question(event: dict):
            # Handle question.asked event
            pass

        listener.on("question.asked", on_question)
        await listener.start()

        # ... later ...
        await listener.stop()
    """

    def __init__(
        self, opencode_client: OpenCodeClient, project_path: str, client_ws: WebSocket
    ) -> None:
        """Initialize SSE event listener.

        Args:
            opencode_client: OpenCode HTTP client
            project_path: Working directory for event stream
            client_ws: WebSocket connection to forward events
        """
        self.opencode = opencode_client
        self.project_path = project_path
        self.client_ws = client_ws
        self.running = False
        self.task: asyncio.Task | None = None
        self.handlers: dict[str, list[Callable[[dict], Awaitable[None]]]] = {}

        logger.debug("sse_listener_initialized", project_path=project_path)

    def on(self, event_type: str, handler: Callable[[dict], Awaitable[None]]) -> None:
        """Register an event handler.

        Multiple handlers can be registered for the same event type.

        Args:
            event_type: Event type to listen for (e.g., "question.asked")
            handler: Async function to call when event occurs
        """
        if event_type not in self.handlers:
            self.handlers[event_type] = []

        self.handlers[event_type].append(handler)

        logger.debug(
            "event_handler_registered",
            event_type=event_type,
            handler_count=len(self.handlers[event_type]),
        )

    async def start(self) -> None:
        """Start listening for events in a background task.

        This creates an asyncio task that will run until stop() is called.
        """
        if self.running:
            logger.warning("sse_listener_already_running")
            return

        self.running = True
        self.task = asyncio.create_task(self._listen_loop())

        logger.info("sse_listener_started", project_path=self.project_path)

    async def stop(self) -> None:
        """Stop listening for events and clean up.

        This cancels the background task and waits for it to complete.
        """
        if not self.running:
            logger.warning("sse_listener_not_running")
            return

        self.running = False

        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None

        logger.info("sse_listener_stopped", project_path=self.project_path)

    async def _listen_loop(self) -> None:
        """Main event listening loop.

        This runs in a background task and continuously streams events
        from OpenCode, dispatching them to registered handlers.
        """
        # Events to skip logging (too noisy)
        noisy_events = {
            "message.part.updated",  # Fires 100+ times during generation
            "message.updated",  # Fires after each message
            "server.heartbeat",  # Fires every 30s
            "session.status",  # Fires frequently
            "session.diff",  # Internal state changes
            "session.updated",  # Internal state changes
        }

        try:
            async for event in self.opencode.subscribe_events(self.project_path):
                if not self.running:
                    break

                event_type = event.get("type")
                if not event_type:
                    logger.warning("sse_event_no_type", event=event)
                    continue

                # Log only if:
                # 1. Event is NOT in noisy list, OR
                # 2. Event has handlers registered (something actually cares about it)
                if event_type not in noisy_events or event_type in self.handlers:
                    logger.debug(
                        "sse_event_received",
                        event_type=event_type,
                        has_handlers=event_type in self.handlers,
                    )

                # Dispatch to registered handlers
                handlers = self.handlers.get(event_type, [])
                for handler in handlers:
                    try:
                        await handler(event)
                    except Exception as e:
                        logger.error(
                            "sse_handler_error",
                            event_type=event_type,
                            error=str(e),
                            exc_info=True,
                        )
        except asyncio.CancelledError:
            logger.debug("sse_listen_loop_cancelled")
            raise
        except Exception as e:
            logger.error("sse_listen_error", error=str(e), exc_info=True)
            # Don't raise - let the listener die gracefully
