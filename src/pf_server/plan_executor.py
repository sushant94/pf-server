"""Plan Executor for OpenCode planning sessions.

This module provides functionality to execute planning sessions with OpenCode,
including session management and prompt handling.
"""

import asyncio
from dataclasses import dataclass

from pf_server.logging_config import get_logger
from pf_server.opencode_client import OpenCodeClient

logger = get_logger(__name__)


@dataclass
class PlanExecutorResult:
    """Result of a plan execution.

    Attributes:
        output: Agent's text output
        stop_reason: Reason for completion (end_turn, max_tokens, etc.)
    """

    output: str
    stop_reason: str


class PlanExecutor:
    """Execute planning sessions with OpenCode.

    This class handles the execution of planning prompts with OpenCode,
    extracting agent output from conversation messages.

    Uses event-based completion detection via session.status SSE events,
    matching the OpenCode CLI approach. The completion_event is set by
    the SSE listener when session.status = idle is received.

    Note: Questions/clarifications are handled via SSE events (question.asked),
    not by this executor. This just sends prompts and extracts output.

    Example:
        completion_event = asyncio.Event()
        executor = PlanExecutor(opencode_client, "/path/to/repo", completion_event)

        # Start new session
        session_id = await executor.start_session()

        # Send prompt (SSE listener will set completion_event when done)
        result = await executor.prompt_async("Create a plan for auth system")
        print(result.output)
    """

    def __init__(
        self,
        opencode_client: OpenCodeClient,
        project_path: str,
        completion_event: asyncio.Event,
    ) -> None:
        """Initialize plan executor.

        Args:
            opencode_client: OpenCode HTTP client
            project_path: Working directory for the session
            completion_event: Event that SSE listener sets when session.status = idle
        """
        self.client = opencode_client
        self.project_path = project_path
        self.session_id: str | None = None
        self.completion_event = completion_event

        logger.debug("plan_executor_initialized", project_path=project_path)

    async def start_session(self) -> str:
        """Create a new OpenCode session in plan mode.

        Returns:
            Session ID

        Raises:
            Exception: If session creation fails
        """
        self.session_id = await self.client.create_session(self.project_path)

        logger.info(
            "plan_session_created",
            session_id=self.session_id,
            project_path=self.project_path,
        )

        return self.session_id

    async def recover_session(self, session_id: str) -> bool:
        """Recover an existing session.

        Attempts to verify that the session exists and is accessible.

        Args:
            session_id: Session ID to recover

        Returns:
            True if session recovered successfully, False otherwise
        """
        try:
            await self.client.get_session(session_id, self.project_path)
            self.session_id = session_id

            logger.info(
                "plan_session_recovered",
                session_id=session_id,
                project_path=self.project_path,
            )

            return True

        except Exception as e:
            logger.warning(
                "plan_session_recovery_failed", session_id=session_id, error=str(e)
            )
            return False

    async def prompt(self, text: str, timeout: float = 300.0) -> PlanExecutorResult:
        """Send a prompt and get agent output (blocking).

        This sends the prompt to OpenCode and returns after receiving the response.
        It does NOT wait for session completion, allowing SSE listeners to handle
        subsequent events (questions, updates) asynchronously.

        This is the recommended method for plan generation to avoid deadlocks when
        OpenCode asks clarification questions.

        Args:
            text: Prompt text
            timeout: Timeout in seconds (default 5 minutes)

        Returns:
            PlanExecutorResult with agent output and stop reason

        Raises:
            RuntimeError: If no active session
        """
        if not self.session_id:
            raise RuntimeError("No active session - call start_session() first")

        logger.info(
            "sending_plan_prompt",
            session_id=self.session_id,
            text_length=len(text),
            text_preview=text[:100],
        )

        try:
            # Send prompt - this blocks until agent completes or asks question
            response = await asyncio.wait_for(
                self.client.send_prompt(
                    session_id=self.session_id,
                    project_path=self.project_path,
                    text=text,
                    agent="plan",
                ),
                timeout=timeout,
            )

            stop_reason = response.get("stopReason", "unknown")

            # Fetch messages to extract the actual output
            messages = await self.client.get_messages(
                self.session_id, self.project_path
            )
            output = self._extract_agent_output(messages)

            logger.info(
                "plan_prompt_completed",
                session_id=self.session_id,
                output_length=len(output),
                stop_reason=stop_reason,
            )

            return PlanExecutorResult(output=output, stop_reason=stop_reason)

        except asyncio.TimeoutError:
            logger.error(
                "plan_prompt_timeout",
                session_id=self.session_id,
                timeout=timeout,
            )
            return PlanExecutorResult(
                output="Plan generation timed out.", stop_reason="timeout"
            )
        except Exception as e:
            logger.error(
                "plan_prompt_error",
                session_id=self.session_id,
                error=str(e),
                exc_info=True,
            )
            raise

    async def prompt_async(
        self, text: str, completion_timeout: float = 300.0
    ) -> PlanExecutorResult:
        """Send a prompt asynchronously and wait for completion.

        This uses the async endpoint and waits for session.status = idle event.
        The completion_event must be set by the SSE listener when it receives
        a session.status event with type "idle".

        Args:
            text: Prompt text
            completion_timeout: Max time to wait for completion (default 5 minutes)

        Returns:
            PlanExecutorResult with output and stop reason

        Raises:
            RuntimeError: If no active session
        """
        if not self.session_id:
            raise RuntimeError("No active session - call start_session() first")

        logger.info(
            "sending_plan_prompt_async",
            session_id=self.session_id,
            text_length=len(text),
            text_preview=text[:100],
        )

        try:
            # Send async prompt (returns immediately)
            await self.client.prompt_async(
                project_path=self.project_path,
                text=text,
                agent="plan",
                continue_session=True,
            )

            # Wait for session.status = idle (set by SSE listener)
            try:
                await asyncio.wait_for(
                    self.completion_event.wait(), timeout=completion_timeout
                )
            except asyncio.TimeoutError:
                logger.error(
                    "plan_prompt_timeout",
                    session_id=self.session_id,
                    timeout=completion_timeout,
                )
                return PlanExecutorResult(
                    output="Plan generation timed out.", stop_reason="timeout"
                )

            # Fetch final messages and extract output
            messages = await self.client.get_messages(
                session_id=self.session_id, project_path=self.project_path
            )
            output = self._extract_agent_output(messages)

            logger.info(
                "plan_prompt_completed",
                session_id=self.session_id,
                output_length=len(output),
            )

            return PlanExecutorResult(output=output, stop_reason="end_turn")

        except Exception as e:
            logger.error(
                "plan_prompt_error",
                session_id=self.session_id,
                error=str(e),
                exc_info=True,
            )
            raise

    def _extract_agent_output(self, messages: list[dict]) -> str:
        """Extract last agent message from conversation.

        Searches backwards through messages to find the most recent
        assistant message and concatenates all text parts.

        Args:
            messages: List of conversation messages

        Returns:
            Extracted agent text (empty string if not found)
        """
        for msg in reversed(messages):
            info = msg.get("info", {})
            if info.get("role") == "assistant":
                parts = msg.get("parts", [])
                text_parts = [
                    p.get("text", "")
                    for p in parts
                    if p.get("type") == "text" and p.get("text")
                ]
                output = "\n".join(text_parts).strip()

                logger.debug(
                    "agent_output_extracted",
                    session_id=self.session_id,
                    output_length=len(output),
                )

                return output

        logger.warning(
            "no_agent_output_found",
            session_id=self.session_id,
            message_count=len(messages),
        )

        return ""


def build_plan_prompt(
    description: str, context: dict | None = None, codebase_summary: str | None = None
) -> str:
    """Build a comprehensive planning prompt.

    Constructs a prompt that includes the user's description,
    optional context (files, annotations), and codebase summary.

    Args:
        description: User's plan request description
        context: Optional context with files and annotations
        codebase_summary: Optional codebase summary

    Returns:
        Formatted prompt text
    """
    parts = ["Please create an implementation plan for the following request:\n"]
    parts.append(f"\n{description}\n")

    if context:
        files = context.get("files", [])
        annotations = context.get("annotations", [])

        if files:
            parts.append("\n## Relevant Files\n")
            for file_path in files:
                parts.append(f"- {file_path}\n")

        if annotations:
            parts.append("\n## Related Annotations\n")
            for ann in annotations:
                parts.append(f"- {ann}\n")

    if codebase_summary:
        parts.append("\n## Codebase Context\n")
        parts.append(codebase_summary)
        parts.append("\n")

    parts.append(
        "\nPlease analyze the request and provide a detailed implementation plan."
    )
    parts.append("\nIf you need clarification on any aspect, ask specific questions.")

    return "".join(parts)
