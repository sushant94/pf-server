"""Repo manager module for shadow directory lifecycle management."""

from pf_server.repo_manager.diff_parser import FileDiff, Hunk, HunkApplyResult
from pf_server.repo_manager.manager import RepoMgr, RepoStatus, WorktreePaths
from pf_server.repo_manager.tag_state import (
    TagInfo,
    TagStateStore,
    TagStatus,
    extract_tag_from_accepted_line,
    extract_tag_from_pf_line,
)

__all__ = [
    "RepoMgr",
    "RepoStatus",
    "WorktreePaths",
    "Hunk",
    "FileDiff",
    "HunkApplyResult",
    "TagStateStore",
    "TagStatus",
    "TagInfo",
    "extract_tag_from_pf_line",
    "extract_tag_from_accepted_line",
]
