"""Session Manager for OpenCode sessions.

This module provides a singleton manager that tracks OpenCode sessions
and SSE listeners per user/project combination.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pf_server.logging_config import get_logger

if TYPE_CHECKING:
    from pf_server.sse_listener import SSEEventListener

logger = get_logger(__name__)


@dataclass
class SessionInfo:
    """Information about an active OpenCode session.

    Attributes:
        session_id: OpenCode session ID
        project_path: Working directory for the session
        listener: SSE event listener (if active)
        last_used: Timestamp of last access
        pending_question_id: ID of currently pending question (if any)
        initial_analysis_task: Background task for initial analysis (if running)
        initial_analysis_complete: Whether initial analysis has completed
        accumulated_changes: Changes buffered during initial analysis
    """

    session_id: str
    project_path: str
    listener: "SSEEventListener | None"
    last_used: float
    pending_question_id: str | None = None

    # Initial analysis tracking (for tar sync auto-start)
    initial_analysis_task: asyncio.Task | None = None
    initial_analysis_complete: bool = False
    accumulated_changes: list[str] = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def is_initial_running(self) -> bool:
        """Check if initial analysis is still in progress."""
        if self.initial_analysis_task is None:
            return False
        return not self.initial_analysis_task.done()

    async def accumulate_change(self, change: str) -> None:
        """Add a change to the accumulation buffer (thread-safe)."""
        async with self._lock:
            self.accumulated_changes.append(change)
            logger.debug(
                "change_accumulated",
                change_count=len(self.accumulated_changes),
            )

    async def drain_accumulated(self) -> list[str]:
        """Get and clear accumulated changes (thread-safe)."""
        async with self._lock:
            changes = self.accumulated_changes.copy()
            self.accumulated_changes.clear()
            return changes


class SessionManager:
    """Manage OpenCode sessions per user/project.

    This is a singleton class that maintains a registry of active OpenCode
    sessions. Each session is identified by a (user_id, project_name) tuple.

    Thread-safe for concurrent access.

    Example:
        mgr = SessionManager.get_instance()

        mgr.set(
            user_id="user123",
            project="myproject",
            session_id="ses_abc",
            project_path="/path/to/repo"
        )

        info = mgr.get("user123", "myproject")
        if info:
            print(f"Session: {info.session_id}")
    """

    _instance: "SessionManager | None" = None
    _lock: threading.Lock = threading.Lock()

    def __init__(self) -> None:
        """Initialize session manager.

        Note: Use get_instance() instead of direct instantiation.
        """
        self._sessions: dict[tuple[str, str], SessionInfo] = {}
        self._sessions_lock: threading.Lock = threading.Lock()
        logger.debug("session_manager_initialized")

    @classmethod
    def get_instance(cls) -> "SessionManager":
        """Get the singleton instance.

        Thread-safe singleton implementation.

        Returns:
            The singleton SessionManager instance
        """
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def get(self, user_id: str, project: str) -> SessionInfo | None:
        """Get session information for a user/project.

        Args:
            user_id: User identifier
            project: Project name

        Returns:
            SessionInfo if exists, None otherwise
        """
        key = (user_id, project)

        with self._sessions_lock:
            info = self._sessions.get(key)

            if info:
                # Update last_used timestamp
                info.last_used = time.time()
                logger.debug(
                    "session_retrieved",
                    user_id=user_id,
                    project=project,
                    session_id=info.session_id,
                )
            else:
                logger.debug("session_not_found", user_id=user_id, project=project)

            return info

    def set(
        self,
        user_id: str,
        project: str,
        session_id: str,
        project_path: str,
        listener: "SSEEventListener | None" = None,
    ) -> None:
        """Store or update session information.

        Args:
            user_id: User identifier
            project: Project name
            session_id: OpenCode session ID
            project_path: Working directory
            listener: Optional SSE event listener
        """
        key = (user_id, project)

        with self._sessions_lock:
            self._sessions[key] = SessionInfo(
                session_id=session_id,
                project_path=project_path,
                listener=listener,
                last_used=time.time(),
            )

        logger.info(
            "session_stored",
            user_id=user_id,
            project=project,
            session_id=session_id,
            has_listener=listener is not None,
        )

    def update_listener(
        self, user_id: str, project: str, listener: "SSEEventListener"
    ) -> bool:
        """Update the SSE listener for an existing session.

        Args:
            user_id: User identifier
            project: Project name
            listener: SSE event listener

        Returns:
            True if session was found and updated, False otherwise
        """
        key = (user_id, project)

        with self._sessions_lock:
            info = self._sessions.get(key)
            if info:
                info.listener = listener
                logger.debug(
                    "session_listener_updated", user_id=user_id, project=project
                )
                return True

            logger.warning(
                "session_not_found_for_listener_update",
                user_id=user_id,
                project=project,
            )
            return False

    def set_pending_question(
        self, user_id: str, project: str, question_id: str | None
    ) -> bool:
        """Set or clear pending question ID for a session.

        Args:
            user_id: User identifier
            project: Project name
            question_id: Question ID or None to clear

        Returns:
            True if session was found and updated, False otherwise
        """
        key = (user_id, project)

        with self._sessions_lock:
            info = self._sessions.get(key)
            if info:
                info.pending_question_id = question_id
                logger.debug(
                    "session_question_updated",
                    user_id=user_id,
                    project=project,
                    question_id=question_id,
                )
                return True

            return False

    def remove(self, user_id: str, project: str) -> bool:
        """Remove a session.

        Args:
            user_id: User identifier
            project: Project name

        Returns:
            True if session was found and removed, False otherwise
        """
        key = (user_id, project)

        with self._sessions_lock:
            if key in self._sessions:
                del self._sessions[key]
                logger.info("session_removed", user_id=user_id, project=project)
                return True

            logger.warning(
                "session_not_found_for_removal", user_id=user_id, project=project
            )
            return False

    def cleanup_old_sessions(self, max_age_seconds: float = 3600.0) -> int:
        """Remove sessions that haven't been accessed recently.

        Args:
            max_age_seconds: Maximum age in seconds before cleanup

        Returns:
            Number of sessions removed
        """
        current_time = time.time()
        removed_count = 0

        with self._sessions_lock:
            keys_to_remove = [
                key
                for key, info in self._sessions.items()
                if current_time - info.last_used > max_age_seconds
            ]

            for key in keys_to_remove:
                del self._sessions[key]
                removed_count += 1

        if removed_count > 0:
            logger.info(
                "sessions_cleaned_up",
                removed_count=removed_count,
                max_age_seconds=max_age_seconds,
            )

        return removed_count

    def list_sessions(self) -> list[tuple[str, str, str]]:
        """List all active sessions.

        Returns:
            List of (user_id, project, session_id) tuples
        """
        with self._sessions_lock:
            return [
                (key[0], key[1], info.session_id)
                for key, info in self._sessions.items()
            ]

    def get_or_create(
        self,
        user_id: str,
        project: str,
        project_path: str,
    ) -> SessionInfo:
        """Get existing session or create a new one.

        This is used by tar sync to ensure a SessionInfo exists for
        tracking initial analysis state, even before an OpenCode session
        is established.

        Args:
            user_id: User identifier
            project: Project name
            project_path: Working directory path

        Returns:
            Existing or newly created SessionInfo
        """
        key = (user_id, project)

        with self._sessions_lock:
            info = self._sessions.get(key)
            if info:
                info.last_used = time.time()
                logger.debug(
                    "session_retrieved_or_created",
                    user_id=user_id,
                    project=project,
                    existed=True,
                )
                return info

            # Create new session info with placeholder session_id
            # The actual OpenCode session_id will be set later when
            # plan generation or WebSocket connection establishes it
            info = SessionInfo(
                session_id="",  # Will be set when OpenCode session starts
                project_path=project_path,
                listener=None,
                last_used=time.time(),
            )
            self._sessions[key] = info

            logger.info(
                "session_created_for_initial_analysis",
                user_id=user_id,
                project=project,
                project_path=project_path,
            )
            return info

    def mark_initial_complete(self, user_id: str, project: str) -> bool:
        """Mark initial analysis as complete for a session.

        Args:
            user_id: User identifier
            project: Project name

        Returns:
            True if session was found and updated, False otherwise
        """
        key = (user_id, project)

        with self._sessions_lock:
            info = self._sessions.get(key)
            if info:
                info.initial_analysis_complete = True
                info.initial_analysis_task = None
                logger.info(
                    "initial_analysis_marked_complete",
                    user_id=user_id,
                    project=project,
                )
                return True
            return False
