"""Plan session management for multi-turn implementation planning."""

from dataclasses import dataclass, field
from typing import Any

from pf_server.logging_config import get_logger
from pf_server.models import PlanStatus

logger = get_logger(__name__)


@dataclass
class PlanSession:
    """State for an active planning session.

    Attributes:
        plan_id: Unique identifier for this planning session
        description: User's original plan request
        context: Optional context (files, annotations)
        status: Current status in the plan lifecycle
        revision: Version number (increments with each update)
        content: Current plan content (markdown)
        questions: Array of questions awaiting user responses (multi-question support)
        confirmation_question: Legacy single question field (deprecated)
        choices: Legacy single question choices (deprecated)
        patches: Final patches when plan is completed
    """

    plan_id: str
    description: str
    context: dict[str, Any] | None
    status: PlanStatus
    revision: int
    content: str

    # Multi-question support
    questions: list[dict] | None = None

    # Legacy single-question support (for backward compatibility)
    confirmation_question: str | None = None
    choices: list[str] | None = field(default=None)

    patches: list[dict[str, str]] | None = None


class PlanManager:
    """Manages active planning sessions.

    Thread-safe storage for plan sessions during WebSocket connections.

    Example:
        manager = PlanManager()
        session = manager.create_session("plan-123", "Add auth to API", None)
        manager.update_session("plan-123", status=PlanStatus.NEEDS_CONFIRMATION)
        session = manager.get_session("plan-123")
    """

    def __init__(self) -> None:
        """Initialize an empty session store."""
        self.sessions: dict[str, PlanSession] = {}

    def create_session(
        self,
        plan_id: str,
        description: str,
        context: dict[str, Any] | None,
    ) -> PlanSession:
        """Create a new planning session.

        Args:
            plan_id: Unique identifier for this session
            description: User's plan request description
            context: Optional context (files, annotations)

        Returns:
            The newly created PlanSession
        """
        session = PlanSession(
            plan_id=plan_id,
            description=description,
            context=context,
            status=PlanStatus.DRAFT,
            revision=0,
            content="",
        )
        self.sessions[plan_id] = session

        logger.info(
            "plan_session_created",
            plan_id=plan_id,
            description_preview=description[:50] if description else "",
        )

        return session

    def get_session(self, plan_id: str) -> PlanSession | None:
        """Retrieve a planning session by ID.

        Args:
            plan_id: The session identifier

        Returns:
            The PlanSession if found, None otherwise
        """
        return self.sessions.get(plan_id)

    def update_session(self, plan_id: str, **kwargs: Any) -> bool:
        """Update session state.

        Args:
            plan_id: The session to update
            **kwargs: Fields to update (status, revision, content, etc.)

        Returns:
            True if session was found and updated, False otherwise
        """
        session = self.sessions.get(plan_id)
        if not session:
            logger.warning("plan_session_not_found", plan_id=plan_id)
            return False

        for key, value in kwargs.items():
            if hasattr(session, key):
                setattr(session, key, value)
            else:
                logger.warning(
                    "plan_session_unknown_field",
                    plan_id=plan_id,
                    field=key,
                )

        logger.debug(
            "plan_session_updated",
            plan_id=plan_id,
            fields=list(kwargs.keys()),
        )

        return True

    def close_session(self, plan_id: str) -> bool:
        """Remove a completed or rejected session.

        Args:
            plan_id: The session to remove

        Returns:
            True if session was found and removed, False otherwise
        """
        if plan_id in self.sessions:
            del self.sessions[plan_id]
            logger.info("plan_session_closed", plan_id=plan_id)
            return True

        logger.warning("plan_session_not_found_for_close", plan_id=plan_id)
        return False

    def list_sessions(self) -> list[PlanSession]:
        """List all active sessions.

        Returns:
            List of all active PlanSession objects
        """
        return list(self.sessions.values())

    def has_session(self, plan_id: str) -> bool:
        """Check if a session exists.

        Args:
            plan_id: The session identifier

        Returns:
            True if session exists, False otherwise
        """
        return plan_id in self.sessions
