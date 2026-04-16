"""RepoMgr: Async context manager for shadow directory lifecycle.

Manages the user's shadow repository by:
- On entry: rebasing against main, applying saved patches
- On exit: capturing changes as patches, restoring clean state
"""

import asyncio
import re
import shutil
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple
from uuid import uuid4

from pf_server.logging_config import get_logger
from pf_server.repo_manager.diff_parser import (
    FileDiff,
    FilteredPatch,
    Hunk,
    HunkApplyResult,
    filter_and_split_pf_hunks,
    parse_diff,
)
from pf_server.repo_manager.tag_state import (
    TagStateStore,
    TagStatus,
    extract_tag_from_pf_line,
)
from pf_server.models import SpecPatch

if TYPE_CHECKING:
    from docker.models.containers import Container

logger = get_logger(__name__)


class RepoStatus(NamedTuple):
    """Current state of the shadow repository."""

    commit_hash: str
    has_changes: bool


class WorktreePaths(NamedTuple):
    """Paths for a temporary git worktree."""

    host_dir: Path  # e.g., /home/user/pf_users_data/uid/project/wt_abc123
    docker_dir: Path  # e.g., /workdir/project/wt_abc123
    branch_name: str  # e.g., worktree-wt_abc123
    name: str  # e.g., wt_abc123


# Pattern for valid worktree names: alphanumeric, underscore, hyphen only
_WORKTREE_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


class RepoMgr:
    """Manager for shadow repository lifecycle.

    Usage:
        mgr = RepoMgr(repo_dir, shadow_dir, patches_dir)
        await mgr.do_init()  # Initialize once

        async with mgr.context():
            # Work with the repo - patches applied, rebased against main
            pass
        # On exit: changes saved as patches, working tree restored
    """

    # pf:invariant:RepoMgr.shadow_is_worktree shadow_dir is always a git worktree of repo_dir after do_init
    # pf:invariant:RepoMgr.tag_state_synced tag_state reflects all saved patches (via _register_patch_tag)

    def __init__(
        self,
        repo_dir: Path,
        shadow_dir: Path,
        patches_dir: Path,
        pf_dir: Path,
        main_branch: str = "main",
    ):
        self.repo_dir = repo_dir
        self.shadow_dir = shadow_dir
        self.patches_dir = patches_dir
        self.pf_dir = pf_dir
        self.main_branch = main_branch
        self.failed_hunks: list[HunkApplyResult] = []
        self.changes = ""
        self.tag_state = TagStateStore(pf_dir=pf_dir)
        self._worktree_patches: list[SpecPatch] = []

    @asynccontextmanager
    async def context(self) -> AsyncIterator["RepoMgr"]:
        """Context manager for shadow repo lifecycle.

        On entry: rebase against main, apply saved patches, cleanup patches dir.
        On exit: save current changes as patches, restore working tree.
        """
        # pf:ensures:context.cleanup_on_exception _restore and _save_patches called even on exception
        # pf:ensures:context.patches_applied_on_entry saved patches applied before yield
        # pf:ensures:context.shadow_clean_on_exit shadow directory restored to HEAD after exit
        # Entry
        # First, before doing a rebase, capture current diff against main
        # This will be required to notify the agent as to what's changed with
        # the repo since the last time
        self.changes = await self._diff_against_main()
        await self._rebase()
        await self._apply_patches()
        self._cleanup_patches_dir()

        try:
            yield self
        finally:
            # Exit - always run cleanup
            try:
                await self._save_patches()
            except Exception as e:
                logger.warning("repo_save_patches_failed", error=str(e))
            try:
                await self._restore()
            except Exception as e:
                logger.warning("repo_restore_failed", error=str(e))

    async def do_init(self) -> str:
        """Initialize repo and create shadow worktree.

        1. Clean shadow_dir if it exists
        2. git init in repo_dir
        3. Add all files and commit
        4. Create shadow_dir as git worktree on 'shadow' branch

        Returns the initial commit hash.
        """
        # pf:requires:do_init.repo_dir_exists repo_dir must exist with files to initialize
        # pf:ensures:do_init.git_initialized repo_dir contains initialized .git directory
        # pf:ensures:do_init.worktree_created shadow_dir is a git worktree on 'shadow' branch
        # pf:ensures:do_init.pf_initialized pf init has been run in shadow directory
        # pf:ensures:do_init.returns_commit returns 40-char hex commit hash
        logger.info("repo_initializing_start", repo_dir=str(self.repo_dir))
        # Clean shadow dir first
        if self.shadow_dir.exists():
            shutil.rmtree(self.shadow_dir)

        # Init, add, commit (batched)
        init_cmd = "git init && git add ."
        await self._run(init_cmd, workdir="repo")

        commit = await self.commit_changes(
            message=f"Initial commit @ {self._timestamp()}"
        )

        # Prune stale worktrees and delete shadow branch if exists
        cleanup_cmd = "git worktree prune && git branch -D shadow 2>/dev/null || true"
        await self._run(cleanup_cmd, workdir="repo")

        # Create shadow as worktree on new branch
        worktree_cmd = f"git worktree add {self.shadow_dir} -b shadow"
        await self._run(worktree_cmd, workdir="repo")

        logger.debug("repo_worktree_created", shadow_dir=str(self.shadow_dir))

        # Initialize pf within shadow with `pf init`
        logger.debug("repo_initializing_pf", shadow_dir=str(self.shadow_dir))
        await self._run("pf init", workdir="shadow")
        logger.info("repo_initializing_complete", repo_dir=str(self.repo_dir))
        return commit

    async def commit_changes(self, message: str | None = None) -> str:
        """Commit all changes with given message. Returns commit hash."""
        # pf:ensures:commit_changes.returns_valid_hash returns 40-char hex commit hash matching HEAD
        # pf:ensures:commit_changes.default_message uses timestamped message if none provided
        if message is None:
            message = f"sync @ {self._timestamp()}"
        cmd = f'git add . && git commit --quiet -am "{message}" && git rev-parse HEAD'
        return (await self._run(cmd, workdir="repo")).strip()

    async def get_status(self) -> RepoStatus:
        """Get current commit hash and whether there are uncommitted changes."""
        # pf:ensures:get_status.returns_valid_hash commit_hash is 40-char hex string
        # pf:ensures:get_status.has_changes_accurate has_changes reflects git status --porcelain output
        # Batch both commands for efficiency
        cmd = "git rev-parse HEAD && git status --porcelain"
        output = await self._run(cmd, workdir="repo")
        lines = output.strip().split("\n")
        commit_hash = lines[0]
        has_changes = len(lines) > 1 and any(lines[1:])
        return RepoStatus(commit_hash=commit_hash, has_changes=has_changes)

    def _timestamp(self) -> str:
        """Get current timestamp string."""
        return (
            datetime.now().strftime("%H:%M:%S:%f")[:-3]
            + " "
            + datetime.now().strftime("%Y-%m-%d")
        )

    def get_patch_contents(self, mark_sent: bool = True) -> list[SpecPatch]:
        """Get contents of UNSEEN patches as list of SpecPatch objects.

        Only returns patches whose tags are in UNSEEN state.
        If mark_sent=True, marks returned patches as SENT.

        Args:
            mark_sent: Whether to mark returned patches as SENT (default True)

        Returns:
            List of SpecPatch objects for UNSEEN tags only
        """
        # pf:requires:get_patch_contents.patches_registered tags must be registered via _save_hunks before filtering works
        # pf:ensures:get_patch_contents.only_unseen all returned patches have tag status == UNSEEN
        # pf:ensures:get_patch_contents.skips_untagged patches without valid pf: tags are skipped (not returned)
        # pf:ensures:get_patch_contents.marks_returned if mark_sent=True, returned patch tags transition to SENT
        patches: list[SpecPatch] = []
        if not self.patches_dir.exists():
            return patches

        tags_to_mark: list[str] = []
        patch_files = sorted(self.patches_dir.glob("*.patch"))

        for patch_file in patch_files:
            patch_content = patch_file.read_text()
            # Extract tag from the patch to check state
            tag = self._extract_tag_from_patch(patch_content)
            if tag is None:
                # Can't extract tag - skip patch since we can't track its state
                logger.warning(
                    "patch_tag_extraction_failed",
                    patch_file=patch_file.name,
                    patch_preview=patch_content[:200],
                )
                continue

            status = self.tag_state.get_status(tag)
            if status == TagStatus.UNSEEN:
                patches.append(SpecPatch(id=str(uuid4()), patch=patch_content))
                tags_to_mark.append(tag)

        if mark_sent and tags_to_mark:
            self.tag_state.mark_sent(tags_to_mark)
            logger.debug(
                "repo_patches_filtered",
                total=len(patch_files),
                unseen=len(patches),
                marked_sent=len(tags_to_mark),
            )

        return patches

    def filter_unseen_spec_patches(
        self,
        patches: list[SpecPatch],
        mark_sent: bool = True,
    ) -> list[SpecPatch]:
        """Filter in-memory SpecPatch list to only UNSEEN tag patches.

        This mirrors get_patch_contents but operates on an in-memory list
        (e.g., patches captured from a worktree). Patches without parseable tags
        are included unchanged.

        Args:
            patches: List of SpecPatch objects to filter.
            mark_sent: Whether to mark UNSEEN tags as SENT (default True).

        Returns:
            List of SpecPatch objects for UNSEEN tags only (plus untagged patches).
        """
        # pf:ensures:filter_unseen_spec_patches.only_unseen returned patches have tag status == UNSEEN
        # pf:ensures:filter_unseen_spec_patches.upserts_tags calls upsert_tag for all valid tags (registers new)
        # pf:ensures:filter_unseen_spec_patches.skips_untagged patches without valid pf: tags are skipped
        if not patches:
            return []

        filtered: list[SpecPatch] = []
        tags_to_mark: list[str] = []

        for patch in patches:
            tag_info = self._extract_tag_info_from_patch(patch.patch)
            if tag_info is None:
                # Can't extract tag - skip patch since we can't track its state
                logger.warning(
                    "patch_tag_extraction_failed",
                    patch_id=patch.id,
                    patch_preview=patch.patch[:200],
                )
                continue

            tag, pf_line, file_path = tag_info
            status = self.tag_state.upsert_tag(
                tag=tag,
                patch_content=patch.patch,
                pf_line=pf_line,
                file_path=file_path,
            )
            if status == TagStatus.UNSEEN:
                filtered.append(patch)
                tags_to_mark.append(tag)

        if mark_sent and tags_to_mark:
            self.tag_state.mark_sent(tags_to_mark)
            logger.debug(
                "repo_in_memory_patches_filtered",
                total=len(patches),
                unseen=len(filtered),
                marked_sent=len(tags_to_mark),
            )

        return filtered

    def _extract_tag_from_patch(self, patch_content: str) -> str | None:
        """Extract the annotation tag from a patch's pf_line.

        Searches for a line containing '# pf:' and extracts the tag.
        """
        for line in patch_content.splitlines():
            # Look for added lines with pf pattern
            if line.startswith("+") and "# pf:" in line:
                parsed = extract_tag_from_pf_line(line[1:])  # Remove + prefix
                if parsed:
                    return parsed[1]  # Return tag
        return None

    def _extract_tag_info_from_patch(
        self, patch_content: str
    ) -> tuple[str, str, str] | None:
        """Extract tag, pf_line, and file_path from patch content."""
        file_path = "unknown"
        file_diffs = parse_diff(patch_content)
        if file_diffs:
            file_path = file_diffs[0].file_path

        for line in patch_content.splitlines():
            if line.startswith("+") and "# pf:" in line:
                pf_line = line[1:]
                parsed = extract_tag_from_pf_line(pf_line)
                if parsed:
                    _, tag, _ = parsed
                    return tag, pf_line, file_path
        return None

    def get_patches_by_file(self) -> dict[str, list[SpecPatch]]:
        """Get patches grouped by file path.

        Returns:
            Dict mapping file paths to list of patch contents for that file.
        """
        patches_by_file: dict[str, list[SpecPatch]] = {}
        if not self.patches_dir.exists():
            return patches_by_file

        patch_files = sorted(self.patches_dir.glob("*.patch"))
        for patch_file in patch_files:
            content = patch_file.read_text()
            # Parse the patch to extract the file path
            file_diffs = parse_diff(content)
            if file_diffs:
                file_path = file_diffs[0].file_path
                if file_path not in patches_by_file:
                    patches_by_file[file_path] = []
                patches_by_file[file_path].append(
                    SpecPatch(id=str(uuid4()), patch=content)
                )

        return patches_by_file

    async def _diff_against_main(self) -> str:
        """Get git diff of shadow against main branch."""
        cmd = f"git diff HEAD {self.main_branch}"
        return await self._run(cmd, workdir="shadow")

    async def _rebase(self) -> None:
        """Rebase against main (batched command)."""
        cmd = f"git rebase {self.main_branch}"
        await self._run(cmd, workdir="shadow")

    async def _apply_patches(self) -> None:
        """Apply all patches from patches_dir (optimistic)."""
        if not self.patches_dir.exists():
            return

        patches = sorted(self.patches_dir.glob("*.patch"))
        if not patches:
            return

        for patch_file in patches:
            patch_content = patch_file.read_text()
            # Parse to get hunk info for failure reporting
            file_diffs = parse_diff(patch_content)
            hunk = (
                file_diffs[0].hunks[0] if file_diffs and file_diffs[0].hunks else None
            )

            result = await self._apply_single_patch(patch_file)
            if not result.success and hunk:
                self.failed_hunks.append(
                    HunkApplyResult(hunk=hunk, success=False, error=result.error)
                )
            elif not result.success:
                logger.warning(
                    "repo_patch_failed", patch=patch_file.name, error=result.error
                )

    async def _apply_single_patch(self, patch_file: Path) -> HunkApplyResult:
        """Apply a single patch file, returning success/failure."""
        cmd = f"git apply {patch_file}"
        try:
            await self._run(cmd, workdir="shadow")
            # Create a minimal hunk for success reporting
            return HunkApplyResult(
                hunk=Hunk(
                    old_start=0,
                    old_count=0,
                    new_start=0,
                    new_count=0,
                    header="",
                    context="",
                    content="",
                    patch=patch_file.read_text(),
                    file_path=patch_file.name,
                ),
                success=True,
            )
        except subprocess.CalledProcessError as e:
            return HunkApplyResult(
                hunk=Hunk(
                    old_start=0,
                    old_count=0,
                    new_start=0,
                    new_count=0,
                    header="",
                    context="",
                    content="",
                    patch=patch_file.read_text(),
                    file_path=patch_file.name,
                ),
                success=False,
                error=e.stderr or str(e),
            )

    def _cleanup_patches_dir(self) -> None:
        """Remove all patches after applying (maintain freshness)."""
        if self.patches_dir.exists():
            shutil.rmtree(self.patches_dir)

    async def _save_patches(self) -> None:
        """Capture current changes and save as individual hunk patches."""
        # Get diff
        diff_output = await self._run("git diff --binary HEAD", workdir="shadow")
        if not diff_output.strip():
            return

        # Parse into file diffs
        file_diffs = parse_diff(diff_output)
        if not file_diffs:
            return

        # Ensure patches dir exists
        self.patches_dir.mkdir(parents=True, exist_ok=True)

        # Save each hunk as a separate patch
        self._save_hunks(file_diffs)

    def _save_hunks(self, file_diffs: list[FileDiff]) -> None:
        """Save each PF-filtered line as a separate, ordered patch file.

        Only lines containing '# pf:' are saved. Each such line becomes its own
        patch file with a strict numeric prefix (0001_, 0002_, etc.) for ordered
        application.

        Also registers each tag with TagStateStore for state tracking.
        """
        # pf:invariant:_save_hunks.registers_before_get tags MUST be registered here before get_patch_contents filters
        # pf:ensures:_save_hunks.ordered_patches patches are named with 4-digit prefix for ordered application
        # pf:ensures:_save_hunks.one_per_pf_line each "# pf:" line becomes its own patch file
        filtered_patches = filter_and_split_pf_hunks(file_diffs)

        for fp in filtered_patches:
            safe_name = fp.file_path.replace("/", "_").replace(".", "_")
            patch_path = (
                self.patches_dir / f"{fp.sequence_number:04d}_{safe_name}.patch"
            )
            patch_path.write_text(fp.patch)

            # Register tag with state store
            self._register_patch_tag(fp)

        logger.debug("repo_patches_saved", patch_count=len(filtered_patches))

    def _register_patch_tag(self, fp: FilteredPatch) -> None:
        """Register a filtered patch's tag with the TagStateStore.

        Uses upsert semantics: new tags become UNSEEN, changed patches reset to UNSEEN.
        """
        # pf:ensures:_register_patch_tag.upsert_semantics new tags UNSEEN, changed content resets to UNSEEN
        parsed = extract_tag_from_pf_line(fp.pf_line)
        if not parsed:
            logger.warning(
                "repo_patch_tag_parse_failed",
                pf_line=fp.pf_line[:100],
                file_path=fp.file_path,
            )
            return

        _, tag, _ = parsed
        status = self.tag_state.upsert_tag(
            tag=tag,
            patch_content=fp.patch,
            pf_line=fp.pf_line,
            file_path=fp.file_path,
        )
        logger.debug(
            "repo_patch_tag_registered",
            tag=tag,
            status=status.value,
            file_path=fp.file_path,
        )

    async def _restore(self) -> None:
        """Restore working tree to clean state."""
        await self._run("git restore .", workdir="shadow")

    def drain_worktree_patches(self) -> list[SpecPatch]:
        """Return worktree patches and clear the internal list."""
        # pf:ensures:drain_worktree_patches.clears_internal _worktree_patches is empty after call
        # pf:ensures:drain_worktree_patches.returns_all returns all previously captured patches
        patches = self._worktree_patches
        logger.debug("drain_worktree_patches", patch_count=len(patches))
        self._worktree_patches = []
        return patches

    async def _capture_patches_to_memory(self, workdir: Path) -> list[SpecPatch]:
        """Capture patches from a working directory into memory."""
        diff_output = await self._run("git diff --binary HEAD", workdir=workdir)
        if not diff_output.strip():
            return []

        file_diffs = parse_diff(diff_output)
        if not file_diffs:
            return []

        filtered_patches = filter_and_split_pf_hunks(file_diffs)
        return [SpecPatch(id=str(uuid4()), patch=fp.patch) for fp in filtered_patches]

    async def _run(self, cmd: str, workdir: str | Path) -> str:
        """Run a shell command async, return stdout."""
        if isinstance(workdir, Path):
            cwd = workdir
        elif workdir == "repo":
            cwd = self.repo_dir
        elif workdir == "shadow":
            cwd = self.shadow_dir
        else:
            raise ValueError(f"Invalid workdir: {workdir}")

        def _sync_run() -> str:
            logger.debug("repo_running_command", cmd=cmd, cwd=str(cwd))
            result = subprocess.run(
                cmd,
                shell=True,
                cwd=cwd,
                capture_output=True,
                check=True,
            )
            return result.stdout.decode("utf-8", errors="replace")

        return await asyncio.to_thread(_sync_run)

    @asynccontextmanager
    async def worktree_context(
        self,
        container: "Container",
        name: str | None = None,
    ) -> AsyncIterator[WorktreePaths]:
        """Create a temporary git worktree for isolated operations.

        Creates a worktree as a sibling to repo/ and shadow/, runs `pf init`
        in the container, yields paths, and cleans up on exit.

        Args:
            container: Docker container for running pf init
            name: Optional worktree name (auto-generated if not provided)

        Yields:
            WorktreePaths with host and docker paths for the worktree

        Example:
            async with user.repo.worktree_context(container=c) as wt:
                result = container.exec_run(cmd=["pf", "mine"], workdir=str(wt.docker_dir))
        """
        # pf:requires:worktree_context.valid_name name must match ^[a-zA-Z0-9_-]+$ pattern if provided
        # pf:ensures:worktree_context.cleanup_always worktree dir and branch are deleted even on exception
        # pf:ensures:worktree_context.patches_captured patches are captured to _worktree_patches before cleanup
        # pf:ensures:worktree_context.pf_init_run pf init is run in container at worktree docker_dir
        # Generate or validate name
        if name is None:
            name = f"wt_{uuid4().hex[:8]}"
        self._validate_worktree_name(name)

        # Calculate paths - worktree is sibling to repo/ and shadow/
        host_dir = self.repo_dir.parent / name
        branch_name = f"worktree-{name}"

        # Get docker path via user context
        from pf_server.user_context import get_current_user

        user = get_current_user()
        docker_dir = user.docker_workdir_dir / name

        paths = WorktreePaths(
            host_dir=host_dir,
            docker_dir=docker_dir,
            branch_name=branch_name,
            name=name,
        )

        try:
            # Create worktree
            await self._create_worktree(host_dir, branch_name)

            # Run pf init in container
            await self._run_pf_init_in_container(container, docker_dir)

            yield paths

        finally:
            # Capture patches before cleanup (directory will be deleted)
            try:
                self._worktree_patches = await self._capture_patches_to_memory(host_dir)
                logger.debug(
                    "worktree_patches_stored",
                    patch_count=len(self._worktree_patches),
                )
            except Exception as e:
                logger.warning("worktree_save_patches_failed", error=str(e))
            # Always cleanup
            await self._cleanup_worktree(host_dir, branch_name)

    def _validate_worktree_name(self, name: str) -> None:
        """Validate worktree name contains only safe characters.

        Raises:
            ValueError: If name contains invalid characters.
        """
        # pf:requires:_validate_worktree_name.non_empty name must be non-empty string
        # pf:ensures:_validate_worktree_name.raises_on_invalid raises ValueError for invalid patterns
        if not _WORKTREE_NAME_PATTERN.match(name):
            raise ValueError(
                f"Invalid worktree name '{name}': must contain only "
                "alphanumeric characters, underscores, and hyphens"
            )

    async def _create_worktree(self, host_dir: Path, branch_name: str) -> None:
        """Create a git worktree at the specified path."""
        cmd = f"git worktree add {host_dir} -b {branch_name}"
        await self._run(cmd, workdir="repo")
        logger.debug(
            "worktree_created",
            host_dir=str(host_dir),
            branch=branch_name,
        )

    async def _run_pf_init_in_container(
        self, container: "Container", docker_dir: Path
    ) -> None:
        """Run pf init in the container at the worktree path."""
        result = container.exec_run(
            cmd=["pf", "init"],
            workdir=str(docker_dir),
        )
        if result.exit_code != 0:
            logger.warning(
                "worktree_pf_init_failed",
                docker_dir=str(docker_dir),
                exit_code=result.exit_code,
                output=result.output.decode("utf-8", errors="replace")
                if result.output
                else "",
            )
        else:
            logger.debug("worktree_pf_init_complete", docker_dir=str(docker_dir))

    async def _cleanup_worktree(self, host_dir: Path, branch_name: str) -> None:
        """Clean up worktree directory and branch."""
        # Remove the worktree directory
        try:
            if host_dir.exists():
                shutil.rmtree(host_dir)
                logger.debug("worktree_dir_removed", host_dir=str(host_dir))
        except Exception as e:
            logger.warning(
                "worktree_dir_remove_failed",
                host_dir=str(host_dir),
                error=str(e),
            )

        # Prune worktrees
        try:
            await self._run("git worktree prune", workdir="repo")
        except Exception as e:
            logger.warning("worktree_prune_failed", error=str(e))

        # Delete the branch (ignore errors if branch doesn't exist)
        try:
            cmd = f"git branch -D {branch_name} 2>/dev/null || true"
            await self._run(cmd, workdir="repo")
            logger.debug("worktree_branch_deleted", branch=branch_name)
        except Exception as e:
            logger.warning(
                "worktree_branch_delete_failed",
                branch=branch_name,
                error=str(e),
            )
