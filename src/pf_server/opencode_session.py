"""OpenCode session management with SSE event handling.

Provides a unified interface for running OpenCode prompts with proper
SSE event handling, supporting both WebSocket-connected and fire-and-forget modes.
"""

import asyncio
from typing import Awaitable, Callable

from fastapi import WebSocket

from pf_server.logging_config import get_logger
from pf_server.opencode_client import OpenCodeClient

logger = get_logger(__name__)


class OpenCodeSession:
    """Manages an OpenCode session with SSE event handling.

    This provides a unified way to:
    1. Send prompts to OpenCode
    2. Listen for SSE events (questions, errors, completion)
    3. Optionally forward events to a WebSocket client
    4. Wait for session completion

    Example (with WebSocket):
        async with OpenCodeSession(client, project_path, client_ws) as session:
            await session.prompt("Analyze this code", agent="ultraguess")
            success = await session.wait_for_completion()

    Example (fire-and-forget, no WebSocket):
        async with OpenCodeSession(client, project_path) as session:
            await session.prompt("Analyze this code", agent="ultraguess")
            success = await session.wait_for_completion()
    """

    def __init__(
        self,
        client: OpenCodeClient,
        project_path: str,
        client_ws: WebSocket | None = None,
        on_question: Callable[[dict], Awaitable[None]] | None = None,
        on_idle: Callable[[dict], Awaitable[None]] | None = None,
        on_error: Callable[[dict], Awaitable[None]] | None = None,
    ) -> None:
        """Initialize OpenCode session.

        Args:
            client: OpenCode HTTP client
            project_path: Working directory for the session
            client_ws: Optional WebSocket for forwarding events
            on_question: Optional custom handler for question.asked events
            on_idle: Optional custom handler for session.idle events
            on_error: Optional custom handler for session.error events
        """
        self.client = client
        self.project_path = project_path
        self.client_ws = client_ws

        # Custom handlers
        self._on_question_handler = on_question
        self._on_idle_handler = on_idle
        self._on_error_handler = on_error

        # State
        self.session_id: str | None = None
        self.error: str | None = None
        self.completion_event = asyncio.Event()
        self._listen_task: asyncio.Task | None = None
        self._running = False

    async def __aenter__(self) -> "OpenCodeSession":
        """Start the SSE listener."""
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Stop the SSE listener."""
        # Exception parameters unused - we always stop the listener
        _ = exc_type, exc_val, exc_tb
        await self.stop()

    async def start(self) -> None:
        """Start listening for SSE events in background."""
        if self._running:
            logger.warning("opencode_session_already_running")
            return

        self._running = True
        self._listen_task = asyncio.create_task(self._listen_loop())

        logger.info(
            "opencode_session_started",
            project_path=self.project_path,
            has_websocket=self.client_ws is not None,
        )

    async def stop(self) -> None:
        """Stop listening for SSE events."""
        if not self._running:
            return

        self._running = False

        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
            self._listen_task = None

        logger.info("opencode_session_stopped", project_path=self.project_path)

    async def prompt(
        self,
        text: str,
        agent: str = "ultraguess",
        model: dict | None = None,
        continue_session: bool = True,
    ) -> str:
        """Send a prompt to OpenCode and return session ID.

        Args:
            text: Prompt text
            agent: Agent name (default: ultraguess)
            model: Model configuration (default: claude-opus via litellm)
            continue_session: Whether to continue existing session

        Returns:
            Session ID
        """
        if model is None:
            model = {"providerID": "litellm", "modelID": "claude-opus"}

        self.session_id = await self.client.prompt_async(
            project_path=self.project_path,
            text=text,
            agent=agent,
            model=model,
            continue_session=continue_session,
        )

        logger.info(
            "opencode_session_prompt_sent",
            session_id=self.session_id,
            agent=agent,
        )

        return self.session_id

    async def wait_for_completion(self, timeout: float | None = None) -> bool:
        """Wait for session to complete (idle or error).

        Args:
            timeout: Maximum time to wait in seconds (None = no timeout)

        Returns:
            True if completed successfully, False if error or timeout
        """
        try:
            if timeout:
                await asyncio.wait_for(self.completion_event.wait(), timeout=timeout)
            else:
                await self.completion_event.wait()

            return self.error is None

        except asyncio.TimeoutError:
            logger.warning(
                "opencode_session_timeout",
                session_id=self.session_id,
                timeout=timeout,
            )
            return False

    async def _listen_loop(self) -> None:
        """Main SSE event listening loop."""
        # Events to skip logging (too noisy)
        noisy_events = {
            "message.part.updated",
            "message.updated",
            "server.heartbeat",
            "session.diff",
        }

        # Track event counts for debugging
        event_counts: dict[str, int] = {}

        try:
            logger.info(
                "opencode_session_listen_started",
                project_path=self.project_path,
                session_id=self.session_id,
            )

            async for event in self.client.subscribe_events(self.project_path):
                if not self._running:
                    break

                event_type = event.get("type", "")
                event_counts[event_type] = event_counts.get(event_type, 0) + 1

                # Log non-noisy events at info level for visibility
                if event_type not in noisy_events:
                    logger.info(
                        "opencode_session_event",
                        event_type=event_type,
                        session_id=self.session_id,
                        count=event_counts[event_type],
                    )

                # Handle events
                # Note: session.idle may actually be session.status with status.type="idle"
                if event_type == "session.idle":
                    await self._handle_idle(event)
                elif event_type == "session.status":
                    # Check if this is an idle status
                    properties = event.get("properties", {})
                    status = properties.get("status", {})
                    status_type = (
                        status.get("type", "") if isinstance(status, dict) else ""
                    )
                    logger.info(
                        "opencode_session_status_event",
                        status_type=status_type,
                        session_id=self.session_id,
                        properties=properties,
                    )
                    if status_type == "idle":
                        await self._handle_idle(event)
                elif event_type == "session.error":
                    await self._handle_error(event)
                elif event_type == "question.asked":
                    await self._handle_question(event)

        except asyncio.CancelledError:
            logger.info(
                "opencode_session_listen_cancelled",
                event_counts=event_counts,
            )
            raise
        except Exception as e:
            logger.error(
                "opencode_session_listen_error",
                error=str(e),
                event_counts=event_counts,
                exc_info=True,
            )
        finally:
            logger.info(
                "opencode_session_listen_ended",
                event_counts=event_counts,
                session_id=self.session_id,
            )

    async def _handle_idle(self, event: dict) -> None:
        """Handle session.idle event."""
        properties = event.get("properties", {})
        session_id = properties.get("sessionID")

        # Only handle our session
        if session_id != self.session_id:
            return

        logger.info(
            "opencode_session_idle",
            session_id=session_id,
        )

        # Call custom handler if provided
        if self._on_idle_handler:
            try:
                await self._on_idle_handler(event)
            except Exception as e:
                logger.error("opencode_session_idle_handler_error", error=str(e))

        # Signal completion
        self.completion_event.set()

    async def _handle_error(self, event: dict) -> None:
        """Handle session.error event."""
        properties = event.get("properties", {})
        session_id = properties.get("sessionID")

        # Only handle our session
        if session_id != self.session_id:
            return

        error_info = properties.get("error", {})
        self.error = error_info.get("message", "Unknown error")

        logger.error(
            "opencode_session_error",
            session_id=session_id,
            error=self.error,
        )

        # Call custom handler if provided
        if self._on_error_handler:
            try:
                await self._on_error_handler(event)
            except Exception as e:
                logger.error("opencode_session_error_handler_error", error=str(e))

        # Signal completion (with error)
        self.completion_event.set()

    async def _handle_question(self, event: dict) -> None:
        """Handle question.asked event."""
        properties = event.get("properties", {})
        session_id = properties.get("sessionID")

        # Only handle our session
        if session_id != self.session_id:
            return

        logger.info(
            "opencode_session_question",
            session_id=session_id,
            question_count=len(properties.get("questions", [])),
        )

        # Call custom handler if provided
        if self._on_question_handler:
            try:
                await self._on_question_handler(event)
            except Exception as e:
                logger.error("opencode_session_question_handler_error", error=str(e))
