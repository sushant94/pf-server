"""OpenCode REST API client.

Provides an async client for communicating with OpenCode servers via HTTP.
Handles session management, prompt submission, event streaming, and cancellation.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Literal

import httpx

from .logging_config import get_logger

logger = get_logger(__name__)

# Default timeout for API calls (5 minutes for long-running operations)
DEFAULT_TIMEOUT = 300.0

# Timeout for first meaningful SSE event after connecting (seconds)
SSE_STARTUP_TIMEOUT = 30.0


@dataclass
class MCPConfig:
    """Configuration for an MCP server."""

    type: Literal["local", "remote"] = "local"
    command: list[str] = field(default_factory=list)
    environment: dict[str, str] = field(default_factory=dict)


class OpenCodeClient:
    """Async client for OpenCode REST API.

    Manages communication with an OpenCode server, including:
    - Health checks
    - MCP server configuration
    - Session management (create, get, continue)
    - Prompt submission
    - Event streaming via SSE
    - Cancellation
    """

    def __init__(self, base_url: str, timeout: float = DEFAULT_TIMEOUT):
        """Initialize the client.

        Args:
            base_url: Base URL of the OpenCode server (e.g., http://localhost:5000)
            timeout: Default timeout for API calls in seconds
        """
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._mcp_initialized: set[str] = set()
        self._active_sessions: dict[str, str] = {}  # project_path -> session_id

    async def _ensure_client(self) -> httpx.AsyncClient:
        """Get or create the HTTP client."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout),
                follow_redirects=True,
            )
        return self._client

    def _headers(self, project_path: str) -> dict[str, str]:
        """Build headers for API requests.

        Args:
            project_path: Project directory path for x-opencode-directory header

        Returns:
            Dict of headers
        """
        return {"x-opencode-directory": project_path}

    async def health_check(self) -> bool:
        """Check if the OpenCode server is healthy.

        Returns:
            True if server is healthy, False otherwise
        """
        try:
            client = await self._ensure_client()
            # Health endpoint is under /global route
            response = await client.get(f"{self._base_url}/global/health")
            if response.status_code == 200:
                data = response.json()
                return data.get("healthy", False)
            return False
        except Exception as e:
            logger.warning(
                "opencode_health_check_failed",
                error=str(e),
                error_type=type(e).__name__,
            )
            return False

    async def ensure_mcp(
        self,
        project_path: str,
        name: str,
        config: MCPConfig,
    ) -> None:
        """Ensure an MCP server is configured.

        Only sends configuration once per (name, project_path) combination.

        Args:
            project_path: Project directory path
            name: MCP server name (e.g., "language-server", "code-pathfinder")
            config: MCP server configuration
        """
        cache_key = f"{project_path}:{name}"
        if cache_key in self._mcp_initialized:
            return

        client = await self._ensure_client()

        mcp_payload = {
            "mcpServers": {
                name: {
                    "type": config.type,
                    "command": config.command,
                    "environment": config.environment,
                }
            }
        }

        response = await client.post(
            f"{self._base_url}/config",
            headers=self._headers(project_path),
            json=mcp_payload,
        )

        if response.status_code == 200:
            self._mcp_initialized.add(cache_key)
        else:
            logger.warning(
                "opencode_mcp_config_failed",
                project=project_path,
                name=name,
                status=response.status_code,
                body=response.text[:200],
            )

    async def get_latest_session(self, project_path: str) -> str | None:
        """Get the most recent root session ID.

        Args:
            project_path: Project directory path

        Returns:
            Session ID if found, None otherwise
        """
        try:
            client = await self._ensure_client()
            response = await client.get(
                f"{self._base_url}/session",
                headers=self._headers(project_path),
            )

            if response.status_code == 200:
                sessions = response.json()
                # Find first root session (no parentID) - list is sorted by updated desc
                for session in sessions:
                    if not session.get("parentID"):
                        return session.get("id")
            return None
        except Exception as e:
            logger.debug("opencode_get_session_failed", error=str(e))
            return None

    async def create_session(self, project_path: str) -> str:
        """Create a new session.

        Args:
            project_path: Project directory path

        Returns:
            New session ID

        Raises:
            RuntimeError: If session creation fails
        """
        client = await self._ensure_client()
        response = await client.post(
            f"{self._base_url}/session",
            headers=self._headers(project_path),
            json={},
        )

        if response.status_code in (200, 201):
            data = response.json()
            session_id = data.get("id")
            if session_id:
                self._active_sessions[project_path] = session_id
                return session_id

        raise RuntimeError(
            f"Failed to create session: {response.status_code} {response.text}"
        )

    async def prompt_async(
        self,
        project_path: str,
        text: str,
        agent: str = "ultraguess",
        model: dict | None = None,
        continue_session: bool = True,
    ) -> str:
        """Submit a prompt to the agent asynchronously.

        Starts the prompt and returns immediately. Use subscribe_events() to
        monitor progress.

        Args:
            project_path: Project directory path
            text: Prompt text to send to the agent
            agent: Agent name to use (default: "ultraguess")
            model: Model configuration dict (e.g., {"providerID": "litellm", "modelID": "claude-opus"})
            continue_session: Whether to continue existing session or create new

        Returns:
            Session ID for the prompt

        Raises:
            RuntimeError: If prompt submission fails (non-204 response)
        """
        client = await self._ensure_client()

        # Determine session ID
        session_id = None
        if continue_session:
            session_id = self._active_sessions.get(project_path)
            if not session_id:
                session_id = await self.get_latest_session(project_path)

        if not session_id:
            session_id = await self.create_session(project_path)

        # Build prompt payload using OpenCode's parts format
        payload: dict = {
            "parts": [{"type": "text", "text": text}],
            "agent": agent,
        }
        if model:
            payload["model"] = model

        response = await client.post(
            f"{self._base_url}/session/{session_id}/prompt_async",
            headers=self._headers(project_path),
            json=payload,
        )

        if response.status_code not in (200, 201, 202, 204):
            logger.error(
                "opencode_prompt_failed",
                project=project_path,
                session_id=session_id,
                status_code=response.status_code,
                response_body=response.text[:500] if response.text else "",
            )
            raise RuntimeError(
                f"Failed to submit prompt: {response.status_code} {response.text[:200]}"
            )

        self._active_sessions[project_path] = session_id
        return session_id

    async def abort(self, project_path: str) -> bool:
        """Abort the current operation for a project.

        Args:
            project_path: Project directory path

        Returns:
            True if abort was successful, False otherwise
        """
        session_id = self._active_sessions.get(project_path)
        if not session_id:
            session_id = await self.get_latest_session(project_path)
            if not session_id:
                logger.debug("opencode_abort_no_session", project=project_path)
                return True  # Nothing to abort

        try:
            client = await self._ensure_client()
            response = await client.post(
                f"{self._base_url}/session/{session_id}/abort",
                headers=self._headers(project_path),
            )

            success = response.status_code in (200, 204)
            if success:
                logger.info(
                    "opencode_abort_success",
                    project=project_path,
                    session_id=session_id,
                )
            else:
                logger.warning(
                    "opencode_abort_failed",
                    project=project_path,
                    session_id=session_id,
                    status=response.status_code,
                )
            return success
        except Exception as e:
            logger.warning("opencode_abort_error", project=project_path, error=str(e))
            return False

    async def subscribe_events(
        self,
        project_path: str,
        startup_timeout: float = SSE_STARTUP_TIMEOUT,
    ) -> AsyncIterator[dict]:
        """Subscribe to server-sent events (SSE) for the project.

        Yields events until the session completes or errors. Has a startup timeout
        to detect if the session never starts producing events.

        Args:
            project_path: Project directory path
            startup_timeout: Maximum time to wait for first meaningful event

        Yields:
            Event dictionaries from the SSE stream

        Raises:
            RuntimeError: If no meaningful events received within startup_timeout
        """
        client = await self._ensure_client()

        # Events that indicate the session is actually processing or completed
        meaningful_events = {
            "step-start",
            "step-finish",
            "message.part.updated",
            "session.updated",
            "message.updated",
            "session.idle",
            "session.error",
        }
        received_meaningful_event = False
        start_time = asyncio.get_event_loop().time()

        try:
            async with client.stream(
                "GET",
                f"{self._base_url}/event",
                headers=self._headers(project_path),
                timeout=None,  # SSE streams should not timeout
            ) as response:
                if response.status_code != 200:
                    logger.error(
                        "opencode_subscribe_events_bad_status",
                        project=project_path,
                        status_code=response.status_code,
                    )
                    return

                line_count = 0
                async for line in response.aiter_lines():
                    line_count += 1
                    if not line:
                        continue
                    if line.startswith("data:"):
                        try:
                            data = json.loads(line[5:].strip())
                            event_type = data.get("type", "")

                            # Track if we've received meaningful events
                            if event_type in meaningful_events:
                                received_meaningful_event = True

                            yield data
                        except json.JSONDecodeError:
                            logger.warning(
                                "opencode_subscribe_events_json_error",
                                project=project_path,
                                line=line[:100],
                            )
                            continue

                    # Check startup timeout if we haven't received meaningful events
                    if not received_meaningful_event:
                        elapsed = asyncio.get_event_loop().time() - start_time
                        if elapsed > startup_timeout:
                            logger.error(
                                "opencode_subscribe_events_startup_timeout",
                                project=project_path,
                                timeout=startup_timeout,
                                line_count=line_count,
                            )
                            raise RuntimeError(
                                f"No meaningful events received within {startup_timeout}s - "
                                "session may not have started correctly"
                            )
        except httpx.ReadTimeout:
            # Expected when session goes idle
            pass
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Re-raise RuntimeError (our startup timeout)
            if isinstance(e, RuntimeError):
                raise
            logger.warning(
                "opencode_event_stream_error",
                project=project_path,
                error=str(e),
                error_type=type(e).__name__,
            )

    async def get_session(self, session_id: str, project_path: str) -> dict:
        """Get session information.

        Args:
            session_id: Session ID to retrieve
            project_path: Project directory path

        Returns:
            Session information dict

        Raises:
            RuntimeError: If session not found or request fails
        """
        client = await self._ensure_client()
        response = await client.get(
            f"{self._base_url}/session/{session_id}",
            headers=self._headers(project_path),
        )

        if response.status_code != 200:
            raise RuntimeError(
                f"Failed to get session: {response.status_code} {response.text}"
            )

        return response.json()

    async def get_messages(self, session_id: str, project_path: str) -> list[dict]:
        """Get conversation messages for a session.

        Args:
            session_id: Session ID
            project_path: Project directory path

        Returns:
            List of message dicts

        Raises:
            RuntimeError: If request fails
        """
        client = await self._ensure_client()
        response = await client.get(
            f"{self._base_url}/session/{session_id}/message",
            headers=self._headers(project_path),
        )

        if response.status_code != 200:
            raise RuntimeError(
                f"Failed to get messages: {response.status_code} {response.text}"
            )

        messages = response.json()
        logger.debug(
            "opencode_messages_retrieved",
            session_id=session_id,
            count=len(messages),
        )
        return messages

    async def send_prompt(
        self,
        session_id: str,
        project_path: str,
        text: str,
        agent: str = "ultraguess",
        model: dict | None = None,
    ) -> dict:
        """Send a prompt to the agent (blocking).

        This blocks until the agent completes the response.
        Use prompt_async() for non-blocking behavior.

        Args:
            session_id: Session ID
            project_path: Project directory path
            text: Prompt text
            agent: Agent name (default: "ultraguess")
            model: Optional model configuration

        Returns:
            Response dict with stop reason and other metadata

        Raises:
            RuntimeError: If request fails
        """
        client = await self._ensure_client()

        payload: dict = {
            "parts": [{"type": "text", "text": text}],
            "agent": agent,
        }
        if model:
            payload["model"] = model

        logger.info(
            "opencode_send_prompt",
            session_id=session_id,
            text_length=len(text),
            text_preview=text[:100],
        )

        response = await client.post(
            f"{self._base_url}/session/{session_id}/message",
            headers=self._headers(project_path),
            json=payload,
        )

        if response.status_code not in (200, 201):
            raise RuntimeError(
                f"Failed to send prompt: {response.status_code} {response.text}"
            )

        result = response.json()
        logger.info(
            "opencode_prompt_completed",
            session_id=session_id,
            stop_reason=result.get("stopReason"),
        )
        return result

    async def list_questions(self, project_path: str) -> list[dict]:
        """List all pending questions.

        Args:
            project_path: Project directory path

        Returns:
            List of pending question request dicts

        Raises:
            RuntimeError: If request fails
        """
        client = await self._ensure_client()
        response = await client.get(
            f"{self._base_url}/question",
            headers=self._headers(project_path),
        )

        if response.status_code != 200:
            raise RuntimeError(
                f"Failed to list questions: {response.status_code} {response.text}"
            )

        questions = response.json()
        logger.debug(
            "opencode_questions_listed",
            count=len(questions),
            project=project_path,
        )
        return questions

    async def reply_to_question(
        self,
        request_id: str,
        answers: list[list[str]],
        project_path: str,
    ) -> None:
        """Reply to a question request.

        Args:
            request_id: Question request ID
            answers: List of answers (each answer is a list for multi-select)
            project_path: Project directory path

        Raises:
            RuntimeError: If request fails
        """
        client = await self._ensure_client()

        logger.info(
            "opencode_reply_to_question",
            request_id=request_id,
            answers=answers,
        )

        response = await client.post(
            f"{self._base_url}/question/{request_id}/reply",
            headers=self._headers(project_path),
            json={"answers": answers},
        )

        if response.status_code not in (200, 204):
            raise RuntimeError(
                f"Failed to reply to question: {response.status_code} {response.text}"
            )

        logger.info("opencode_question_reply_sent", request_id=request_id)

    async def reject_question(self, request_id: str, project_path: str) -> None:
        """Reject a question request.

        Args:
            request_id: Question request ID
            project_path: Project directory path

        Raises:
            RuntimeError: If request fails
        """
        client = await self._ensure_client()

        logger.info("opencode_reject_question", request_id=request_id)

        response = await client.post(
            f"{self._base_url}/question/{request_id}/reject",
            headers=self._headers(project_path),
        )

        if response.status_code not in (200, 204):
            raise RuntimeError(
                f"Failed to reject question: {response.status_code} {response.text}"
            )

        logger.info("opencode_question_rejected", request_id=request_id)

    async def close(self) -> None:
        """Close the HTTP client and release resources."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            self._mcp_initialized.clear()
            self._active_sessions.clear()
