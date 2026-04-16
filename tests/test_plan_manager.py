"""Tests for PlanManager session management."""

from pf_server.models import PlanStatus
from pf_server.plan_manager import PlanManager


class TestPlanManager:
    """Test suite for PlanManager."""

    def test_create_session(self):
        """Test creating a new planning session."""
        manager = PlanManager()

        session = manager.create_session(
            plan_id="plan-123",
            description="Add authentication",
            context={"files": ["auth.py"]},
        )

        assert session.plan_id == "plan-123"
        assert session.description == "Add authentication"
        assert session.context == {"files": ["auth.py"]}
        assert session.status == PlanStatus.DRAFT
        assert session.revision == 0
        assert session.content == ""

    def test_get_session(self):
        """Test retrieving a session by ID."""
        manager = PlanManager()
        manager.create_session("plan-123", "Test", None)

        session = manager.get_session("plan-123")

        assert session is not None
        assert session.plan_id == "plan-123"

    def test_get_session_not_found(self):
        """Test retrieving a non-existent session."""
        manager = PlanManager()

        session = manager.get_session("nonexistent")

        assert session is None

    def test_update_session(self):
        """Test updating session state."""
        manager = PlanManager()
        manager.create_session("plan-123", "Test", None)

        result = manager.update_session(
            "plan-123",
            status=PlanStatus.NEEDS_CONFIRMATION,
            revision=1,
            content="# Plan\n\nStep 1",
            confirmation_question="Proceed?",
        )

        assert result is True

        session = manager.get_session("plan-123")
        assert session.status == PlanStatus.NEEDS_CONFIRMATION
        assert session.revision == 1
        assert session.content == "# Plan\n\nStep 1"
        assert session.confirmation_question == "Proceed?"

    def test_update_session_not_found(self):
        """Test updating a non-existent session."""
        manager = PlanManager()

        result = manager.update_session("nonexistent", status=PlanStatus.COMPLETED)

        assert result is False

    def test_close_session(self):
        """Test closing/removing a session."""
        manager = PlanManager()
        manager.create_session("plan-123", "Test", None)

        result = manager.close_session("plan-123")

        assert result is True
        assert manager.get_session("plan-123") is None

    def test_close_session_not_found(self):
        """Test closing a non-existent session."""
        manager = PlanManager()

        result = manager.close_session("nonexistent")

        assert result is False

    def test_list_sessions(self):
        """Test listing all active sessions."""
        manager = PlanManager()
        manager.create_session("plan-1", "First", None)
        manager.create_session("plan-2", "Second", None)
        manager.create_session("plan-3", "Third", None)

        sessions = manager.list_sessions()

        assert len(sessions) == 3
        plan_ids = [s.plan_id for s in sessions]
        assert "plan-1" in plan_ids
        assert "plan-2" in plan_ids
        assert "plan-3" in plan_ids

    def test_has_session(self):
        """Test checking if a session exists."""
        manager = PlanManager()
        manager.create_session("plan-123", "Test", None)

        assert manager.has_session("plan-123") is True
        assert manager.has_session("nonexistent") is False

    def test_multiple_sessions(self):
        """Test managing multiple independent sessions."""
        manager = PlanManager()

        # Create multiple sessions
        manager.create_session("plan-1", "First plan", {"files": ["a.py"]})
        manager.create_session("plan-2", "Second plan", {"files": ["b.py"]})

        # Update them independently
        manager.update_session(
            "plan-1", status=PlanStatus.NEEDS_CONFIRMATION, revision=1
        )
        manager.update_session("plan-2", status=PlanStatus.COMPLETED, revision=2)

        session1 = manager.get_session("plan-1")
        session2 = manager.get_session("plan-2")

        assert session1.status == PlanStatus.NEEDS_CONFIRMATION
        assert session1.revision == 1
        assert session2.status == PlanStatus.COMPLETED
        assert session2.revision == 2

    def test_session_with_choices(self):
        """Test session with multiple choice options."""
        manager = PlanManager()
        manager.create_session("plan-123", "Test", None)

        manager.update_session(
            "plan-123",
            status=PlanStatus.NEEDS_CONFIRMATION,
            choices=["Yes", "No", "Maybe"],
        )

        session = manager.get_session("plan-123")
        assert session.choices == ["Yes", "No", "Maybe"]

    def test_session_with_patches(self):
        """Test session with final patches."""
        manager = PlanManager()
        manager.create_session("plan-123", "Test", None)

        patches = [
            {"id": "patch-1", "patch": "--- a/file.py\n+++ b/file.py"},
            {"id": "patch-2", "patch": "--- a/other.py\n+++ b/other.py"},
        ]

        manager.update_session(
            "plan-123",
            status=PlanStatus.COMPLETED,
            patches=patches,
        )

        session = manager.get_session("plan-123")
        assert session.status == PlanStatus.COMPLETED
        assert len(session.patches) == 2


class TestPlanStatus:
    """Test PlanStatus enum."""

    def test_status_values(self):
        """Test that status values are correct strings."""
        assert PlanStatus.DRAFT.value == "draft"
        assert PlanStatus.NEEDS_CONFIRMATION.value == "needs_confirmation"
        assert PlanStatus.CONFIRMED.value == "confirmed"
        assert PlanStatus.REJECTED.value == "rejected"
        assert PlanStatus.COMPLETED.value == "completed"

    def test_status_is_str_enum(self):
        """Test that PlanStatus values work as strings."""
        assert PlanStatus.DRAFT == "draft"
        assert PlanStatus.NEEDS_CONFIRMATION.value == "needs_confirmation"
