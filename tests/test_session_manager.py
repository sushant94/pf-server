"""Tests for SessionManager."""

import time
from unittest.mock import MagicMock

from pf_server.session_manager import SessionManager


class TestSessionManagerSingleton:
    """Test SessionManager singleton behavior."""

    def test_singleton(self):
        """Test that SessionManager is a singleton."""
        mgr1 = SessionManager.get_instance()
        mgr2 = SessionManager.get_instance()

        assert mgr1 is mgr2


class TestSessionManagerBasic:
    """Test basic SessionManager operations."""

    def setup_method(self):
        """Reset singleton state before each test."""
        # Get instance and clear sessions
        mgr = SessionManager.get_instance()
        for user_id, project, _ in mgr.list_sessions():
            mgr.remove(user_id, project)

    def test_set_and_get(self):
        """Test storing and retrieving session info."""
        mgr = SessionManager.get_instance()

        mgr.set(
            user_id="user1",
            project="proj1",
            session_id="ses_123",
            project_path="/path/to/repo",
        )

        info = mgr.get("user1", "proj1")

        assert info is not None
        assert info.session_id == "ses_123"
        assert info.project_path == "/path/to/repo"
        assert info.listener is None

    def test_get_nonexistent(self):
        """Test getting a nonexistent session returns None."""
        mgr = SessionManager.get_instance()

        info = mgr.get("nonexistent_user", "nonexistent_project")

        assert info is None

    def test_update_listener(self):
        """Test updating session listener."""
        mgr = SessionManager.get_instance()

        # First create a session
        mgr.set(
            user_id="user2", project="proj2", session_id="ses_456", project_path="/path"
        )

        # Create mock listener
        mock_listener = MagicMock()

        # Update with listener
        success = mgr.update_listener("user2", "proj2", mock_listener)

        assert success is True

        # Verify listener was set
        info = mgr.get("user2", "proj2")
        assert info.listener is mock_listener

    def test_update_listener_nonexistent(self):
        """Test updating listener for nonexistent session fails."""
        mgr = SessionManager.get_instance()
        mock_listener = MagicMock()

        success = mgr.update_listener("nonexistent", "nonexistent", mock_listener)

        assert success is False

    def test_set_pending_question(self):
        """Test setting and clearing pending question."""
        mgr = SessionManager.get_instance()

        mgr.set("user3", "proj3", "ses_789", "/path")

        # Set pending question
        success = mgr.set_pending_question("user3", "proj3", "que_123")
        assert success is True

        info = mgr.get("user3", "proj3")
        assert info.pending_question_id == "que_123"

        # Clear pending question
        success = mgr.set_pending_question("user3", "proj3", None)
        assert success is True

        info = mgr.get("user3", "proj3")
        assert info.pending_question_id is None

    def test_remove(self):
        """Test removing a session."""
        mgr = SessionManager.get_instance()

        mgr.set("user4", "proj4", "ses_abc", "/path")

        # Verify it exists
        assert mgr.get("user4", "proj4") is not None

        # Remove it
        success = mgr.remove("user4", "proj4")
        assert success is True

        # Verify it's gone
        assert mgr.get("user4", "proj4") is None

    def test_remove_nonexistent(self):
        """Test removing nonexistent session returns False."""
        mgr = SessionManager.get_instance()

        success = mgr.remove("nonexistent", "nonexistent")

        assert success is False

    def test_cleanup_old_sessions(self):
        """Test cleaning up old sessions."""
        mgr = SessionManager.get_instance()

        # Create a session and manually set old timestamp
        mgr.set("old_user", "old_proj", "ses_old", "/path")
        old_info = mgr.get("old_user", "old_proj")
        old_info.last_used = time.time() - 7200  # 2 hours ago

        # Create a recent session
        mgr.set("new_user", "new_proj", "ses_new", "/path")

        # Cleanup sessions older than 1 hour
        removed = mgr.cleanup_old_sessions(max_age_seconds=3600)

        # Should have removed the old session
        assert removed >= 1
        assert mgr.get("old_user", "old_proj") is None
        assert mgr.get("new_user", "new_proj") is not None

    def test_list_sessions(self):
        """Test listing all sessions."""
        mgr = SessionManager.get_instance()

        # Add some sessions
        mgr.set("user_a", "proj_a", "ses_a", "/path/a")
        mgr.set("user_b", "proj_b", "ses_b", "/path/b")

        sessions = mgr.list_sessions()

        assert len(sessions) >= 2

        # Check that both sessions are in the list
        session_ids = {ses_id for _, _, ses_id in sessions}
        assert "ses_a" in session_ids
        assert "ses_b" in session_ids
