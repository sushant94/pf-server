"""User context management via ContextVar.

Provides a single source of truth for the current authenticated user,
accessible from FastAPI routes, MCP tools, and any helper functions
without explicit parameter passing
"""

from contextvars import ContextVar
from pathlib import Path


from pf_server.config import settings

from pf_server.repo_manager.manager import RepoMgr

from .logging_config import get_logger

logger = get_logger(__name__)


class UserContext:
    """Authenticated user context."""

    # pf:invariant:UserContext.user_id_immutable user_id is set at construction and never changes
    # pf:invariant:UserContext.project_once_set project_name can only be set once per session
    def __init__(self, user_id: str, login: str | None = None) -> None:
        self.user_id = user_id
        self.login = login
        self.project_name: str | None = None
        self._repo: RepoMgr | None = None

    def set_project_name(self, project_name: str) -> bool:
        """Set the project name for this user context."""
        # pf:requires:set_project_name.not_already_different if project_name already set, must match or returns False
        # pf:ensures:set_project_name.creates_repo_on_success if returns True, _repo is initialized
        if self.project_name is not None and self.project_name != project_name:
            logger.warning(
                "Project name is already set in this session. Currently a user can only work on one project at a time."
            )
            return False
        self.project_name = project_name
        self._repo = RepoMgr(
            repo_dir=self.host_user_repo_dir,
            shadow_dir=self.host_user_shadow_dir,
            patches_dir=self.host_patches_dir,
            pf_dir=self.host_pf_dir,
        )
        return True

    # XXX: There is an implicit assumption in these paths that host_user_dir -> docker_workdir_dir
    # Would be nicer if we could *ensure* that they stay in sync, perhaps via a mapping function

    @property
    def project_root(self) -> Path:
        """Get the user's project root path (derived from user_id)."""
        return settings.host_users_data_directory / self.user_id / self.project_name

    @property
    def host_mount_dir(self) -> Path:
        """Get the user's host mount directory path."""
        return settings.host_users_data_directory / self.user_id

    @property
    def host_user_dir(self) -> Path:
        """Get the user's host directory path (derived from user_id)."""
        return self.host_mount_dir / self.project_name

    @property
    def host_user_repo_dir(self) -> Path:
        """Get the user's host repository directory path for specific project."""
        return self.host_user_dir / "repo"

    @property
    def host_user_shadow_dir(self) -> Path:
        """Get the user's host shadow directory path for specific project."""
        return self.host_user_dir / "shadow"

    @property
    def host_feedback_dir(self) -> Path:
        """Get the user's host feedback directory path for specific project."""
        return self.host_user_shadow_dir / "feedback"

    @property
    def host_pf_dir(self) -> Path:
        """Get the user's .pf directory for persistent state."""
        return self.host_user_shadow_dir / ".pf"

    @property
    def host_patches_dir(self) -> Path:
        """Get the user's patches directory for storing hunk patches."""
        return self.host_user_shadow_dir / ".patches"

    @property
    def docker_workdir_dir(self) -> Path:
        """Get the user's docker directory path."""
        return Path(settings.docker_base_cwd) / self.project_name

    @property
    def docker_shadow_dir(self) -> Path:
        """Get the user's docker shadow directory path."""
        return self.docker_workdir_dir / "shadow"

    @property
    def docker_feedback_dir(self) -> Path:
        """Get the user's docker feedback directory path."""
        return self.docker_workdir_dir / "shadow" / "feedback"

    @property
    def repo(self) -> RepoMgr:
        """Get the RepoMgr instance for this user.

        Usage:
            async with user.repo.context():
                # Work with repo
                pass

        Raises:
            RuntimeError: If project_name has not been set yet.
        """
        # pf:requires:repo.project_set project_name must be set before accessing repo
        # pf:ensures:repo.returns_valid returns non-None RepoMgr instance
        if self._repo is None:
            raise RuntimeError("RepoMgr not available: project_name not set")
        return self._repo

    def create_dirs(self) -> None:
        """Create necessary user directories if they don't exist."""
        self.host_user_repo_dir.mkdir(parents=True, exist_ok=True)
        self.host_user_shadow_dir.mkdir(parents=True, exist_ok=True)
        self.host_feedback_dir.mkdir(parents=True, exist_ok=True)


_current_user: ContextVar[UserContext | None] = ContextVar("current_user", default=None)


def get_current_user() -> UserContext:
    """Get the current authenticated user.

    Raises:
        RuntimeError: If no user context is set (not authenticated).
    """
    # pf:requires:get_current_user.context_set context variable must have been set by authentication
    # pf:ensures:get_current_user.returns_valid returns UserContext or raises RuntimeError
    user = _current_user.get()
    if user is None:
        raise RuntimeError("No user context - authentication required")
    return user


def set_current_user(user: UserContext) -> None:
    """Set the current user context."""
    _current_user.set(user)


def set_project_name(project_name: str) -> bool:
    """Set the project name for the current user context."""
    user = get_current_user()
    return user.set_project_name(project_name)
