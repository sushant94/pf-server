"""Analysis configuration definitions for guessing infrastructure.

Provides unified interface for running analyses via PF or OpenCode backends.
Both backends implement the same AnalysisConfig interface with run() and cancel().
"""

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pf_server.config import settings
from pf_server.containers import exec_with_log_streaming, is_progress_log
from pf_server.logging_config import get_logger
from pf_server.opencode_client import MCPConfig

logger = get_logger(__name__)

if TYPE_CHECKING:
    from docker.models.containers import Container

    from pf_server.opencode_client import OpenCodeClient


# =============================================================================
# Analysis Events and Context
# =============================================================================


@dataclass
class AnalysisEvent:
    """Event from analysis execution."""

    type: str  # "progress", "complete", "error"
    data: dict = field(default_factory=dict)


@dataclass
class AnalysisContext:
    """Context for running an analysis."""

    container: "Container"
    project_path: str
    opencode_client: "OpenCodeClient | None" = None


# =============================================================================
# Base Analysis Config
# =============================================================================


@dataclass
class AnalysisConfig:
    """Base configuration for analysis.

    Subclasses implement run() and cancel() for specific backends.
    """

    name: str  # "lite", "trigger", "ask", etc.
    debounce_ms: int  # How long to wait for quiet period
    progress_filter: Callable[[dict], bool] | None = None  # Filter for progress logs
    feedback_template: str | None = None

    def format_feedback(self, **kwargs: Any) -> str:
        """Create a feedback string from template or just return kwargs as dict str."""
        if self.feedback_template is None:
            return str(kwargs)
        return self.feedback_template.format(**kwargs)

    async def run(
        self,
        ctx: AnalysisContext,
        feedback_content: str | None = None,
        question: str | None = None,
    ) -> AsyncIterator[AnalysisEvent]:
        """Run analysis, yielding progress events. Override in subclasses."""
        raise NotImplementedError
        yield  # Make this a generator

    async def cancel(self, ctx: AnalysisContext) -> bool:
        """Cancel running analysis. Override in subclasses."""
        raise NotImplementedError


# =============================================================================
# Progress Filters
# =============================================================================

# Progress filter configuration for lite analysis (PF)
# These match the "event" field in structlog JSON output
LITE_EVENT_PREFIXES = {"agent_"}
LITE_EVENT_NAMES = {
    "max_iterations_reached",
}


def lite_progress_filter(entry: dict) -> bool:
    """Check if log entry indicates progress for lite analysis."""
    return is_progress_log(entry, LITE_EVENT_PREFIXES, LITE_EVENT_NAMES)


# Progress filter for OpenCode JSON events
# Key event types from OpenCode SSE stream:
# - "session.status": Status change (busy/idle) - most useful for progress
# - "session.updated": Session data updated
# - "file.edited": A file was modified
# - "file.watcher.updated": File watcher detected changes
# - "lsp.client.diagnostics": LSP diagnostics updated
# - "message.part.updated": Message part updated (very frequent, noisy)
# Note: "step-start"/"step-finish" do NOT exist as SSE event types
OPENCODE_PROGRESS_EVENTS = {"session.status", "file.edited"}


def opencode_progress_filter(entry: dict) -> bool:
    """Filter for opencode JSON events that indicate agent progress.

    OpenCode SSE events have a "type" field. We filter for events that
    indicate meaningful progress without being too noisy.
    """
    return entry.get("type", "") in OPENCODE_PROGRESS_EVENTS


# =============================================================================
# PF Analysis Config (uses exec)
# =============================================================================


@dataclass
class PFAnalysisConfig(AnalysisConfig):
    """PF-based analysis using container exec.

    Runs pf commands inside the Docker container and streams log output.
    """

    command: str = ""  # Command template with {config_name}, {feedback_file}, etc.
    scope: str | None = None  # Optional analysis scope
    pf_config_name: str = "default-miner.yaml"
    template_vars: dict[str, str] = field(default_factory=dict)

    # Internal: marker for the current running process (for cancellation)
    _current_marker: str | None = field(default=None, init=False, repr=False)

    def format_command(self, **kwargs: Any) -> str:
        """Format the analysis command with given kwargs."""
        base_command = self.command.format(**kwargs, **self.template_vars)
        if self.scope:
            base_command += f" --scope {self.scope}"
        return base_command

    async def run(
        self,
        ctx: AnalysisContext,
        feedback_content: str | None = None,
        question: str | None = None,
    ) -> AsyncIterator[AnalysisEvent]:
        """Run PF analysis in container, yielding progress events."""
        # Generate marker for cancellation
        marker = f"pf_analysis_{self.name}_{time.time_ns()}"
        self._current_marker = marker

        # Build command kwargs
        cmd_kwargs: dict[str, Any] = {
            "config_name": self.pf_config_name,
        }

        # Handle question for ask analysis
        if question:
            import json

            cmd_kwargs["question"] = json.dumps(question)[1:-1]  # Escape for JSON

        # Format the command
        cmd = self.format_command(**cmd_kwargs)

        # Wrap with exec -a for pkill-based cancellation
        full_cmd = f"exec -a {marker} {cmd}"

        # Execute with log streaming
        async def stream_logs() -> tuple[int, str]:
            result = await exec_with_log_streaming(
                container=ctx.container,
                cmd=full_cmd,
                workdir=ctx.project_path,
                progress_filter=self.progress_filter,
                marker=marker,
            )
            return result.exit_code, result.output

        # Run in background and yield progress
        task = asyncio.create_task(stream_logs())

        try:
            # Wait for completion
            exit_code, output = await task

            if exit_code == 0:
                yield AnalysisEvent(type="complete", data={"exit_code": exit_code})
            else:
                yield AnalysisEvent(
                    type="error",
                    data={"exit_code": exit_code, "output": output[:500]},
                )
        except asyncio.CancelledError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            raise
        finally:
            self._current_marker = None

    async def cancel(self, ctx: AnalysisContext) -> bool:
        """Cancel by sending SIGTERM via pkill with marker."""
        if self._current_marker is None:
            return True

        marker = self._current_marker
        result = await asyncio.to_thread(
            ctx.container.exec_run,
            cmd=["pkill", "-TERM", "-f", marker],
            workdir="/",
        )
        # pkill returns 0 if processes matched, 1 if none matched
        # Both are acceptable (process may have already exited)
        return result.exit_code in (0, 1)


# =============================================================================
# OpenCode Analysis Config (uses REST API)
# =============================================================================


@dataclass
class OpenCodeAnalysisConfig(AnalysisConfig):
    """OpenCode-based analysis using REST API.

    Communicates with OpenCode server via HTTP REST API.
    """

    agent: str = "ultraguess"
    model: dict = field(
        default_factory=lambda: {"providerID": "litellm", "modelID": "claude-opus"}
    )
    continue_session: bool = True

    async def run(
        self,
        ctx: AnalysisContext,
        feedback_content: str | None = None,
        question: str | None = None,
    ) -> AsyncIterator[AnalysisEvent]:
        """Run OpenCode analysis via REST API, yielding progress events."""
        client = ctx.opencode_client
        if not client:
            logger.error("opencode_analysis_no_client", config_name=self.name)
            yield AnalysisEvent(
                type="error",
                data={"message": "OpenCodeClient not available in context"},
            )
            return

        # Health check before proceeding
        is_healthy = await client.health_check()
        if not is_healthy:
            logger.error(
                "opencode_analysis_server_unhealthy",
                config_name=self.name,
                base_url=client._base_url,
            )
            yield AnalysisEvent(
                type="error",
                data={"message": "OpenCode server is not healthy"},
            )
            return

        # Ensure MCP servers are configured
        try:
            await client.ensure_mcp(
                ctx.project_path,
                "language-server",
                MCPConfig(
                    command=[
                        "mcp-language-server",
                        "--workspace",
                        ctx.project_path,
                        "--lsp",
                        "pyright-langserver",
                        "--",
                        "--stdio",
                    ],
                ),
            )
            await client.ensure_mcp(
                ctx.project_path,
                "code-pathfinder",
                MCPConfig(
                    command=["pathfinder", "serve", "--project", ctx.project_path],
                ),
            )
        except Exception as e:
            logger.error(
                "opencode_analysis_mcp_failed",
                config_name=self.name,
                error=str(e),
            )
            yield AnalysisEvent(
                type="error",
                data={"message": f"Failed to configure MCP: {e}"},
            )
            return

        # Build prompt
        if self.feedback_template and feedback_content:
            prompt = self.format_feedback(changes=feedback_content)
        elif question:
            prompt = question
        else:
            prompt = "Analyze this codebase and mine for specifications. Focus on discovering invariants, preconditions, postconditions, and temporal properties."

        # Send prompt
        try:
            session_id = await client.prompt_async(
                project_path=ctx.project_path,
                text=prompt,
                agent=self.agent,
                model=self.model,
                continue_session=self.continue_session,
            )
        except Exception as e:
            logger.error(
                "opencode_analysis_prompt_failed",
                config_name=self.name,
                error=str(e),
            )
            yield AnalysisEvent(
                type="error",
                data={"message": f"Failed to submit prompt: {e}"},
            )
            return

        # Stream events - log progress events matching filter
        event_count = 0
        try:
            async for event in client.subscribe_events(ctx.project_path):
                event_count += 1
                event_type = event.get("type", "")
                properties = event.get("properties", {})
                event_session_id = properties.get("sessionID")

                # Only log and yield progress events matching filter
                if self.progress_filter and self.progress_filter(event):
                    logger.info(
                        "opencode_sse_progress",
                        source="opencode_sse",
                        event_type=event_type,
                    )
                    yield AnalysisEvent(type="progress", data=event)

                # Check for session completion
                if event_type == "session.error" and event_session_id == session_id:
                    error_info = properties.get("error", {})
                    error_msg = error_info.get("message", "Session error")
                    logger.error(
                        "opencode_analysis_session_error",
                        config_name=self.name,
                        session_id=session_id,
                        error=error_msg,
                    )
                    yield AnalysisEvent(
                        type="error",
                        data={"message": error_msg, "session_id": session_id},
                    )
                    break

                # Check for idle status (session completion)
                # Note: session.idle may be session.status with status.type="idle"
                is_idle = event_type == "session.idle"
                if event_type == "session.status":
                    status = properties.get("status", {})
                    if isinstance(status, dict) and status.get("type") == "idle":
                        is_idle = True

                if is_idle and event_session_id == session_id:
                    break

            logger.info(
                "opencode_analysis_complete",
                config_name=self.name,
                session_id=session_id,
            )
            yield AnalysisEvent(
                type="complete",
                data={"session_id": session_id},
            )
        except asyncio.CancelledError:
            logger.warning(
                "opencode_analysis_cancelled",
                config_name=self.name,
                session_id=session_id,
            )
            # Try to abort on cancellation
            await client.abort(ctx.project_path)
            raise
        except Exception as e:
            logger.error(
                "opencode_analysis_event_loop_error",
                config_name=self.name,
                error=str(e),
            )
            yield AnalysisEvent(
                type="error",
                data={"message": f"Event stream error: {e}"},
            )

    async def cancel(self, ctx: AnalysisContext) -> bool:
        """Cancel by calling abort API."""
        if ctx.opencode_client:
            return await ctx.opencode_client.abort(ctx.project_path)
        return True


# =============================================================================
# Config Instances
# =============================================================================

# PF Configs
LITE_ANALYSIS = PFAnalysisConfig(
    name="lite",
    debounce_ms=settings.lite_analysis_debounce_ms,
    command=settings.lite_analysis_command,
    progress_filter=lite_progress_filter,
    pf_config_name="guesser-miner-v2.yaml",
    feedback_template="The following changes have occured. Please use this information to continue the mining for properties.\n\n<changes>\n{changes}\n</changes>\n",
)

TRIGGER_ANALYSIS = PFAnalysisConfig(
    name="trigger",
    debounce_ms=0,  # No debounce for manual analysis
    command="pf -q --log-level='debug' mine --no-sandbox -c /pf-tools/proofactory/configs/{config_name} guess .",
    progress_filter=lite_progress_filter,
    pf_config_name="guesser-miner-v2.yaml",
    feedback_template="The following changes have occured. Please use this information to continue the mining for properties.\n\n<changes>\n{changes}\n</changes>\n",
)

ASK_ANALYSIS_CONFIG = PFAnalysisConfig(
    name="ask",
    debounce_ms=0,  # No debounce - run immediately
    command="pf -q --log-level='debug' mine --no-sandbox -c /pf-tools/proofactory/configs/{config_name} guess . --extra '{{\"question\": \"{question}\" }}'",
    progress_filter=lite_progress_filter,
    pf_config_name="ask-question-guess.yaml",
    feedback_template=None,
)

# OpenCode Configs
OPENCODE_LITE_ANALYSIS = OpenCodeAnalysisConfig(
    name="opencode_lite",
    debounce_ms=settings.lite_analysis_debounce_ms,
    progress_filter=opencode_progress_filter,
    feedback_template="The following changes have occurred in the codebase. Please analyze these changes and continue mining for specifications.\n\n<changes>\n{changes}\n</changes>",
    continue_session=True,
)

OPENCODE_TRIGGER_ANALYSIS = OpenCodeAnalysisConfig(
    name="opencode_trigger",
    debounce_ms=0,  # No debounce for manual trigger
    progress_filter=opencode_progress_filter,
    continue_session=False,  # Create new session for fresh analysis
)

OPENCODE_ASK_ANALYSIS = OpenCodeAnalysisConfig(
    name="opencode_ask",
    debounce_ms=0,  # No debounce - run immediately
    progress_filter=opencode_progress_filter,
    continue_session=False,  # Create new session for each question
)


# =============================================================================
# Config Selectors
# =============================================================================


def get_lite_analysis_config() -> AnalysisConfig:
    """Get the lite analysis config based on settings.lite_analysis_backend."""
    if settings.lite_analysis_backend == "opencode":
        return OPENCODE_LITE_ANALYSIS
    return LITE_ANALYSIS


def get_trigger_analysis_config() -> AnalysisConfig:
    """Get the trigger analysis config based on settings.trigger_analysis_backend."""
    if settings.trigger_analysis_backend == "opencode":
        return OPENCODE_TRIGGER_ANALYSIS
    return TRIGGER_ANALYSIS


def get_ask_analysis_config() -> AnalysisConfig:
    """Get the ask analysis config based on settings.ask_analysis_backend."""
    if settings.ask_analysis_backend == "opencode":
        return OPENCODE_ASK_ANALYSIS
    return ASK_ANALYSIS_CONFIG
