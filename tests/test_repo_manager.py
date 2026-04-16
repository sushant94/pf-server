"""Tests for RepoMgr class."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pf_server.repo_manager import RepoMgr, RepoStatus, WorktreePaths


@pytest.fixture
def repo_dirs(tmp_path):
    """Create temporary directories for repo, shadow, and patches."""
    repo_dir = tmp_path / "repo"
    shadow_dir = tmp_path / "shadow"
    patches_dir = tmp_path / "patches"

    repo_dir.mkdir()

    # Create some initial files in repo
    (repo_dir / "main.py").write_text("def main():\n    pass\n")
    (repo_dir / "utils.py").write_text("def helper():\n    return 1\n")

    return repo_dir, shadow_dir, patches_dir


class TestDoInit:
    """Tests for do_init()."""

    @pytest.mark.asyncio
    async def test_initializes_git_repo(self, repo_dirs):
        """do_init creates a git repo in repo_dir."""
        repo_dir, shadow_dir, patches_dir = repo_dirs
        mgr = RepoMgr(repo_dir, shadow_dir, patches_dir)

        await mgr.do_init()

        # Verify repo is initialized
        assert (repo_dir / ".git").exists()

    @pytest.mark.asyncio
    async def test_creates_initial_commit(self, repo_dirs):
        """do_init commits all files."""
        repo_dir, shadow_dir, patches_dir = repo_dirs
        mgr = RepoMgr(repo_dir, shadow_dir, patches_dir)

        await mgr.do_init()

        # Verify commit exists
        result = subprocess.run(
            "git log --oneline",
            shell=True,
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )
        assert "Initial commit @" in result.stdout

    @pytest.mark.asyncio
    async def test_creates_shadow_worktree(self, repo_dirs):
        """do_init creates shadow as a git worktree."""
        repo_dir, shadow_dir, patches_dir = repo_dirs
        mgr = RepoMgr(repo_dir, shadow_dir, patches_dir)

        await mgr.do_init()

        # Verify shadow dir exists and is a worktree
        assert shadow_dir.exists()
        assert (shadow_dir / ".git").exists()  # worktrees have a .git file

        # Verify shadow has the files
        assert (shadow_dir / "main.py").exists()
        assert (shadow_dir / "utils.py").exists()

    @pytest.mark.asyncio
    async def test_shadow_on_shadow_branch(self, repo_dirs):
        """do_init creates shadow worktree on 'shadow' branch."""
        repo_dir, shadow_dir, patches_dir = repo_dirs
        mgr = RepoMgr(repo_dir, shadow_dir, patches_dir)

        await mgr.do_init()

        # Verify shadow is on shadow branch
        result = subprocess.run(
            "git branch --show-current",
            shell=True,
            cwd=shadow_dir,
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == "shadow"

    @pytest.mark.asyncio
    async def test_cleans_existing_shadow(self, repo_dirs):
        """do_init removes existing shadow dir before creating worktree."""
        repo_dir, shadow_dir, patches_dir = repo_dirs

        # Create a pre-existing shadow dir with junk
        shadow_dir.mkdir()
        (shadow_dir / "old_file.txt").write_text("should be deleted")

        mgr = RepoMgr(repo_dir, shadow_dir, patches_dir)
        await mgr.do_init()

        # Verify old file is gone and new worktree is created
        assert not (shadow_dir / "old_file.txt").exists()
        assert (shadow_dir / "main.py").exists()

    @pytest.mark.asyncio
    async def test_reinit_works(self, repo_dirs):
        """do_init can be called multiple times (reinitialize)."""
        repo_dir, shadow_dir, patches_dir = repo_dirs
        mgr = RepoMgr(repo_dir, shadow_dir, patches_dir)

        # First init
        await mgr.do_init()

        # Add a file and reinit
        (repo_dir / "new_file.py").write_text("# new")

        # Second init should work (prunes old worktree, deletes old branch)
        await mgr.do_init()

        assert shadow_dir.exists()
        assert (shadow_dir / "new_file.py").exists()


class TestCommitChanges:
    """Tests for commit_changes()."""

    @pytest.fixture
    async def initialized_repo(self, repo_dirs):
        """Create and initialize a repo."""
        repo_dir, shadow_dir, patches_dir = repo_dirs
        mgr = RepoMgr(repo_dir, shadow_dir, patches_dir)
        await mgr.do_init()
        return mgr, repo_dir, shadow_dir

    @pytest.mark.asyncio
    async def test_returns_commit_hash(self, initialized_repo):
        """commit_changes returns a valid commit hash."""
        mgr, repo_dir, _ = initialized_repo

        # Make a change
        (repo_dir / "main.py").write_text("def main():\n    print('hello')\n")

        commit_hash = await mgr.commit_changes("test commit")

        # Verify it's a valid hash (40 hex chars)
        assert len(commit_hash) == 40
        assert all(c in "0123456789abcdef" for c in commit_hash)

    @pytest.mark.asyncio
    async def test_commit_with_custom_message(self, initialized_repo):
        """commit_changes uses the provided message."""
        mgr, repo_dir, _ = initialized_repo

        (repo_dir / "main.py").write_text("def main():\n    print('custom')\n")
        await mgr.commit_changes("my custom message")

        result = subprocess.run(
            "git log -1 --format=%s",
            shell=True,
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == "my custom message"

    @pytest.mark.asyncio
    async def test_commit_with_default_message(self, initialized_repo):
        """commit_changes generates a timestamp message if none provided."""
        mgr, repo_dir, _ = initialized_repo

        (repo_dir / "main.py").write_text("def main():\n    print('default')\n")
        await mgr.commit_changes()

        result = subprocess.run(
            "git log -1 --format=%s",
            shell=True,
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip().startswith("sync @")

    @pytest.mark.asyncio
    async def test_commit_hash_matches_head(self, initialized_repo):
        """Returned hash matches HEAD after commit."""
        mgr, repo_dir, _ = initialized_repo

        (repo_dir / "main.py").write_text("def main():\n    print('verify')\n")
        commit_hash = await mgr.commit_changes("verify hash")

        result = subprocess.run(
            "git rev-parse HEAD",
            shell=True,
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == commit_hash


class TestGetStatus:
    """Tests for get_status()."""

    @pytest.fixture
    async def initialized_repo(self, repo_dirs):
        """Create and initialize a repo."""
        repo_dir, shadow_dir, patches_dir = repo_dirs
        mgr = RepoMgr(repo_dir, shadow_dir, patches_dir)
        await mgr.do_init()
        return mgr, repo_dir, shadow_dir

    @pytest.mark.asyncio
    async def test_returns_repo_status(self, initialized_repo):
        """get_status returns a RepoStatus namedtuple."""
        mgr, _, _ = initialized_repo

        status = await mgr.get_status()

        assert isinstance(status, RepoStatus)
        assert hasattr(status, "commit_hash")
        assert hasattr(status, "has_changes")

    @pytest.mark.asyncio
    async def test_no_changes_when_clean(self, initialized_repo):
        """has_changes is False when repo is clean."""
        mgr, _, _ = initialized_repo

        status = await mgr.get_status()

        assert status.has_changes is False

    @pytest.mark.asyncio
    async def test_has_changes_when_modified(self, initialized_repo):
        """has_changes is True when files are modified."""
        mgr, repo_dir, _ = initialized_repo

        # Modify a file
        (repo_dir / "main.py").write_text("def main():\n    # modified\n")

        status = await mgr.get_status()

        assert status.has_changes is True

    @pytest.mark.asyncio
    async def test_has_changes_when_untracked(self, initialized_repo):
        """has_changes is True when there are untracked files."""
        mgr, repo_dir, _ = initialized_repo

        # Add an untracked file
        (repo_dir / "new_untracked.py").write_text("# new file")

        status = await mgr.get_status()

        assert status.has_changes is True

    @pytest.mark.asyncio
    async def test_commit_hash_is_valid(self, initialized_repo):
        """commit_hash is a valid 40-char hex string."""
        mgr, _, _ = initialized_repo

        status = await mgr.get_status()

        assert len(status.commit_hash) == 40
        assert all(c in "0123456789abcdef" for c in status.commit_hash)

    @pytest.mark.asyncio
    async def test_commit_hash_matches_head(self, initialized_repo):
        """commit_hash matches git rev-parse HEAD."""
        mgr, repo_dir, _ = initialized_repo

        status = await mgr.get_status()

        result = subprocess.run(
            "git rev-parse HEAD",
            shell=True,
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )
        assert status.commit_hash == result.stdout.strip()

    @pytest.mark.asyncio
    async def test_clean_after_commit(self, initialized_repo):
        """has_changes is False after committing changes."""
        mgr, repo_dir, _ = initialized_repo

        # Modify and verify dirty
        (repo_dir / "main.py").write_text("def main():\n    # changed\n")
        status = await mgr.get_status()
        assert status.has_changes is True

        # Commit and verify clean
        await mgr.commit_changes("commit changes")
        status = await mgr.get_status()
        assert status.has_changes is False


class TestContext:
    """Tests for context() method."""

    @pytest.fixture
    async def initialized_repo(self, repo_dirs):
        """Create and initialize a repo."""
        repo_dir, shadow_dir, patches_dir = repo_dirs
        mgr = RepoMgr(repo_dir, shadow_dir, patches_dir)
        await mgr.do_init()
        return mgr, repo_dir, shadow_dir, patches_dir

    @pytest.mark.asyncio
    async def test_context_yields_manager(self, initialized_repo):
        """context() yields the RepoMgr instance."""
        mgr, _, _, _ = initialized_repo

        async with mgr.context() as ctx_mgr:
            assert ctx_mgr is mgr

    @pytest.mark.asyncio
    async def test_context_restores_on_exit(self, initialized_repo):
        """context() restores shadow to clean state on exit."""
        mgr, _, shadow_dir, _ = initialized_repo

        # Make changes in shadow within context
        async with mgr.context():
            (shadow_dir / "main.py").write_text(
                "def main():\n    # modified in context\n"
            )
            # Verify change exists
            assert "modified in context" in (shadow_dir / "main.py").read_text()

        # After context exit, shadow should be restored
        assert "modified in context" not in (shadow_dir / "main.py").read_text()

    @pytest.mark.asyncio
    async def test_context_saves_patches_on_exit(self, initialized_repo):
        """context() saves changes as patches on exit."""
        mgr, _, shadow_dir, patches_dir = initialized_repo

        async with mgr.context():
            (shadow_dir / "main.py").write_text("def main():\n    # save this change\n")

        # Patches should be saved
        assert patches_dir.exists()
        patches = list(patches_dir.glob("*.patch"))
        assert len(patches) >= 1

    @pytest.mark.asyncio
    async def test_context_applies_patches_on_entry(self, initialized_repo):
        """context() applies saved patches on entry."""
        mgr, _, shadow_dir, patches_dir = initialized_repo

        # First context: make and save changes
        async with mgr.context():
            (shadow_dir / "main.py").write_text("def main():\n    # patched\n")

        # Verify shadow is restored (clean)
        assert "patched" not in (shadow_dir / "main.py").read_text()

        # Second context: patches should be re-applied
        async with mgr.context():
            content = (shadow_dir / "main.py").read_text()
            assert "patched" in content

    @pytest.mark.asyncio
    async def test_context_cleans_patches_during_entry(self, initialized_repo):
        """context() removes patches dir after applying on entry."""
        mgr, _, shadow_dir, patches_dir = initialized_repo

        # Create patches
        async with mgr.context():
            (shadow_dir / "main.py").write_text("def main():\n    # for cleanup test\n")

        assert patches_dir.exists()
        original_patches = set(patches_dir.glob("*.patch"))

        # Enter context again - patches should be applied then cleaned
        async with mgr.context():
            # During the context, patches dir should be cleaned (after apply)
            # Note: it may be recreated on exit if there are changes
            pass

        # Patches are re-saved on exit (since applied changes are still there)
        # But they should be the same content - verify patches exist
        assert patches_dir.exists()
        new_patches = set(patches_dir.glob("*.patch"))
        assert len(new_patches) == len(original_patches)

    @pytest.mark.asyncio
    async def test_context_restores_on_exception(self, initialized_repo):
        """context() still restores on exception."""
        mgr, _, shadow_dir, _ = initialized_repo

        with pytest.raises(ValueError):
            async with mgr.context():
                (shadow_dir / "main.py").write_text(
                    "def main():\n    # before exception\n"
                )
                raise ValueError("test error")

        # Should still be restored
        assert "before exception" not in (shadow_dir / "main.py").read_text()

    @pytest.mark.asyncio
    async def test_can_use_manager_outside_context(self, initialized_repo):
        """RepoMgr methods work outside of context()."""
        mgr, repo_dir, _, _ = initialized_repo

        # These should work without entering context
        status = await mgr.get_status()
        assert status.commit_hash

        (repo_dir / "main.py").write_text("def main():\n    # outside context\n")
        commit_hash = await mgr.commit_changes("outside context commit")
        assert len(commit_hash) == 40


class TestWorktreeContext:
    """Tests for worktree_context() method."""

    @pytest.fixture
    async def initialized_repo(self, repo_dirs):
        """Create and initialize a repo."""
        repo_dir, shadow_dir, patches_dir = repo_dirs
        mgr = RepoMgr(repo_dir, shadow_dir, patches_dir)
        await mgr.do_init()
        return mgr, repo_dir, shadow_dir, patches_dir

    @pytest.fixture
    def mock_container(self):
        """Create a mock Docker container."""
        container = MagicMock()
        exec_result = MagicMock()
        exec_result.exit_code = 0
        exec_result.output = b"pf initialized"
        container.exec_run.return_value = exec_result
        return container

    @pytest.fixture
    def mock_user_context(self, tmp_path):
        """Create a mock user context with docker paths."""
        user_ctx = MagicMock()
        user_ctx.docker_workdir_dir = Path("/workdir/test_project")
        return user_ctx

    @pytest.mark.asyncio
    async def test_creates_worktree_directory(
        self, initialized_repo, mock_container, mock_user_context
    ):
        """worktree_context creates a worktree directory."""
        mgr, repo_dir, _, _ = initialized_repo

        with patch(
            "pf_server.user_context.get_current_user",
            return_value=mock_user_context,
        ):
            async with mgr.worktree_context(
                container=mock_container,
                name="test_wt",
            ) as wt:
                assert wt.host_dir.exists()
                assert wt.host_dir == repo_dir.parent / "test_wt"

    @pytest.mark.asyncio
    async def test_returns_worktree_paths(
        self, initialized_repo, mock_container, mock_user_context
    ):
        """worktree_context returns WorktreePaths namedtuple."""
        mgr, repo_dir, _, _ = initialized_repo

        with patch(
            "pf_server.user_context.get_current_user",
            return_value=mock_user_context,
        ):
            async with mgr.worktree_context(
                container=mock_container,
                name="paths_test",
            ) as wt:
                assert isinstance(wt, WorktreePaths)
                assert wt.name == "paths_test"
                assert wt.branch_name == "worktree-paths_test"
                assert wt.host_dir == repo_dir.parent / "paths_test"
                assert wt.docker_dir == Path("/workdir/test_project/paths_test")

    @pytest.mark.asyncio
    async def test_generates_name_if_not_provided(
        self, initialized_repo, mock_container, mock_user_context
    ):
        """worktree_context generates a name when not provided."""
        mgr, _, _, _ = initialized_repo

        with patch(
            "pf_server.user_context.get_current_user",
            return_value=mock_user_context,
        ):
            async with mgr.worktree_context(
                container=mock_container,
            ) as wt:
                assert wt.name.startswith("wt_")
                assert len(wt.name) == 11  # "wt_" + 8 hex chars

    @pytest.mark.asyncio
    async def test_runs_pf_init_in_container(
        self, initialized_repo, mock_container, mock_user_context
    ):
        """worktree_context runs pf init in the container."""
        mgr, _, _, _ = initialized_repo

        with patch(
            "pf_server.user_context.get_current_user",
            return_value=mock_user_context,
        ):
            async with mgr.worktree_context(
                container=mock_container,
                name="pf_init_test",
            ):
                pass

        # Verify pf init was called
        mock_container.exec_run.assert_called_once()
        call_kwargs = mock_container.exec_run.call_args
        assert call_kwargs[1]["cmd"] == ["pf", "init"]
        assert call_kwargs[1]["workdir"] == "/workdir/test_project/pf_init_test"

    @pytest.mark.asyncio
    async def test_cleans_up_on_normal_exit(
        self, initialized_repo, mock_container, mock_user_context
    ):
        """worktree_context removes directory on normal exit."""
        mgr, repo_dir, _, _ = initialized_repo

        with patch(
            "pf_server.user_context.get_current_user",
            return_value=mock_user_context,
        ):
            async with mgr.worktree_context(
                container=mock_container,
                name="cleanup_test",
            ) as wt:
                worktree_path = wt.host_dir
                assert worktree_path.exists()

        # After exit, directory should be gone
        assert not worktree_path.exists()

    @pytest.mark.asyncio
    async def test_cleans_up_on_exception(
        self, initialized_repo, mock_container, mock_user_context
    ):
        """worktree_context removes directory even on exception."""
        mgr, repo_dir, _, _ = initialized_repo

        with patch(
            "pf_server.user_context.get_current_user",
            return_value=mock_user_context,
        ):
            with pytest.raises(ValueError):
                async with mgr.worktree_context(
                    container=mock_container,
                    name="exception_test",
                ) as wt:
                    worktree_path = wt.host_dir
                    assert worktree_path.exists()
                    raise ValueError("intentional error")

        # After exit, directory should still be cleaned up
        assert not worktree_path.exists()

    @pytest.mark.asyncio
    async def test_worktree_is_on_correct_branch(
        self, initialized_repo, mock_container, mock_user_context
    ):
        """worktree is created on the correct branch."""
        mgr, _, _, _ = initialized_repo

        with patch(
            "pf_server.user_context.get_current_user",
            return_value=mock_user_context,
        ):
            async with mgr.worktree_context(
                container=mock_container,
                name="branch_test",
            ) as wt:
                # Check the branch in the worktree
                result = subprocess.run(
                    "git branch --show-current",
                    shell=True,
                    cwd=wt.host_dir,
                    capture_output=True,
                    text=True,
                )
                assert result.stdout.strip() == "worktree-branch_test"

    @pytest.mark.asyncio
    async def test_worktree_has_repo_files(
        self, initialized_repo, mock_container, mock_user_context
    ):
        """worktree contains the repository files."""
        mgr, _, _, _ = initialized_repo

        with patch(
            "pf_server.user_context.get_current_user",
            return_value=mock_user_context,
        ):
            async with mgr.worktree_context(
                container=mock_container,
                name="files_test",
            ) as wt:
                assert (wt.host_dir / "main.py").exists()
                assert (wt.host_dir / "utils.py").exists()

    @pytest.mark.asyncio
    async def test_validates_worktree_name(self, initialized_repo, mock_container):
        """worktree_context rejects invalid names."""
        mgr, _, _, _ = initialized_repo

        with pytest.raises(ValueError, match="Invalid worktree name"):
            async with mgr.worktree_context(
                container=mock_container,
                name="invalid/name",
            ):
                pass

    @pytest.mark.asyncio
    async def test_validates_worktree_name_spaces(
        self, initialized_repo, mock_container
    ):
        """worktree_context rejects names with spaces."""
        mgr, _, _, _ = initialized_repo

        with pytest.raises(ValueError, match="Invalid worktree name"):
            async with mgr.worktree_context(
                container=mock_container,
                name="has spaces",
            ):
                pass

    @pytest.mark.asyncio
    async def test_allows_valid_names(
        self, initialized_repo, mock_container, mock_user_context
    ):
        """worktree_context accepts valid names with underscores and hyphens."""
        mgr, _, _, _ = initialized_repo

        with patch(
            "pf_server.user_context.get_current_user",
            return_value=mock_user_context,
        ):
            async with mgr.worktree_context(
                container=mock_container,
                name="valid_name-123",
            ) as wt:
                assert wt.name == "valid_name-123"

    @pytest.mark.asyncio
    async def test_branch_deleted_after_cleanup(
        self, initialized_repo, mock_container, mock_user_context
    ):
        """Branch is deleted after worktree cleanup."""
        mgr, repo_dir, _, _ = initialized_repo

        with patch(
            "pf_server.user_context.get_current_user",
            return_value=mock_user_context,
        ):
            async with mgr.worktree_context(
                container=mock_container,
                name="branch_cleanup",
            ):
                pass

        # Branch should be deleted
        result = subprocess.run(
            "git branch",
            shell=True,
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )
        assert "worktree-branch_cleanup" not in result.stdout
