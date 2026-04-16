"""Structured logging configuration using structlog."""

import logging
import time
import uuid
from contextlib import contextmanager
from typing import Any

import structlog
from rich.console import Console
from rich.text import Text

# Shared Rich console instance
_console = Console()


class RichStyledLogger:
    """Logger that uses Rich for styled output.

    Applies line-level styles based on content:
    - Docker exec logs (source=docker_exec): Dim cyan
    - OpenCode SSE logs (opencode_sse, opencode_session_*): Dim blue
    - Debug level logs: Dim (with gray tag via ConsoleRenderer)
    """

    def _print_styled(self, message: str) -> None:
        # Determine style based on content
        style = None
        if "docker_exec" in message:
            style = "dim cyan"
        elif "opencode_sse" in message or "opencode_session_" in message:
            style = "dim blue"
        elif "\x1b[1mdebug" in message:
            style = "dim"

        if style:
            # Use Rich Text to apply style over existing ANSI codes
            text = Text.from_ansi(message)
            text.stylize(style)
            _console.print(text)
        else:
            # Print with existing ANSI codes preserved
            _console.print(Text.from_ansi(message))

    def msg(self, message: str) -> None:
        self._print_styled(message)

    # All log methods delegate to msg
    err = debug = info = warning = error = critical = exception = msg


class RichStyledLoggerFactory:
    """Factory for RichStyledLogger instances."""

    def __call__(self, *args: Any, **kwargs: Any) -> RichStyledLogger:
        return RichStyledLogger()


def get_console_renderer() -> structlog.dev.ConsoleRenderer:
    """Get a ConsoleRenderer with the standard pf-server styling.

    Used by both the logging config and the log viewer CLI to ensure
    consistent formatting across local dev and log viewing.

    Returns:
        Configured ConsoleRenderer instance.
    """
    level_styles = structlog.dev.ConsoleRenderer.get_default_level_styles()
    level_styles["debug"] = "\x1b[90m"  # Bright black (gray)
    return structlog.dev.ConsoleRenderer(colors=True, level_styles=level_styles)


def configure_logging(json_output: bool = False, log_level: str = "INFO") -> None:
    """Configure structlog with console or JSON output.

    Args:
        json_output: If True, output JSON lines (for production).
                     If False, output colored console (for development).
        log_level: Minimum log level (DEBUG, INFO, WARNING, ERROR, CRITICAL).
    """
    # Shared processors for all outputs
    shared_processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    if json_output:
        # Production: JSON lines to stdout
        processors = shared_processors + [
            structlog.processors.JSONRenderer(),
        ]
        logger_factory = structlog.PrintLoggerFactory()
    else:
        # Development: colored console output with styled logger
        processors = shared_processors + [
            get_console_renderer(),
        ]
        logger_factory = RichStyledLoggerFactory()

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, log_level.upper())
        ),
        context_class=dict,
        logger_factory=logger_factory,
        cache_logger_on_first_use=True,
    )


def bind_request_context(user_id: str | None = None, login: str | None = None) -> None:
    """Bind user context to all subsequent log calls in this async context.

    Call this at the start of request/WebSocket handling after authentication.
    The bound variables will appear in all log entries until the context ends.

    Args:
        user_id: GitHub user ID (from JWT sub claim).
        login: GitHub username.
    """
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        request_id=str(uuid.uuid4())[:8],
        user_id=user_id,
        login=login,
    )


def bind_analysis_context(
    generation: int, analysis_type: str, marker: str | None = None
) -> None:
    """Add analysis-specific context to logs.

    Call this when starting analysis to add generation tracking to logs.

    Args:
        generation: Analysis generation number.
        analysis_type: Type of analysis ("lite" or "heavy").
        marker: Process marker for cancellation tracking.
    """
    structlog.contextvars.bind_contextvars(
        generation=generation,
        analysis_type=analysis_type,
        marker=marker,
    )


def unbind_analysis_context() -> None:
    """Remove analysis-specific context from logs."""
    structlog.contextvars.unbind_contextvars("generation", "analysis_type", "marker")


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Get a structlog logger.

    Args:
        name: Logger name (typically __name__).

    Returns:
        A bound structlog logger with context variables merged.
    """
    return structlog.get_logger(name)


@contextmanager
def timed_operation(logger: Any, event: str, **initial_context: Any):
    """Context manager for timing operations and logging duration.

    Usage:
        with timed_operation(logger, "analysis_completed", generation=1):
            do_analysis()
        # Logs: analysis_completed with duration_ms=...

    Args:
        logger: The structlog logger to use.
        event: Event name to log on completion.
        **initial_context: Additional context to include in the log.

    Yields:
        A dict that can be updated with additional context during the operation.
    """
    start_time = time.perf_counter()
    context: dict[str, Any] = dict(initial_context)
    try:
        yield context
    finally:
        duration_ms = int((time.perf_counter() - start_time) * 1000)
        logger.info(event, duration_ms=duration_ms, **context)
