"""Tag state management for annotation patches.

Provides a single source of truth for tracking the lifecycle of annotation tags:
UNSEEN → SENT → ACCEPTED/REJECTED

State is persisted to a JSON file in the .pf directory with file locking
for concurrent access safety.
"""

import fcntl
import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from pf_server.logging_config import get_logger

logger = get_logger(__name__)

# Regex to extract type, tag, and text from a pf_line
# Matches: "# pf:<type>:<tag> <text>" with optional whitespace
PF_LINE_REGEX = re.compile(r"#\s*pf:(\w+):(\S+)\s+(.*)")


class TagStatus(str, Enum):
    """Status of an annotation tag in the review lifecycle."""

    # pf:invariant:TagStatus.valid_transitions only valid transitions are UNSEEN->SENT, SENT->ACCEPTED, SENT->REJECTED, any->UNSEEN (on content change)
    UNSEEN = "unseen"  # Tag exists but hasn't been sent to client
    SENT = "sent"  # Tag has been sent to client, awaiting decision
    ACCEPTED = "accepted"  # Client accepted the annotation
    REJECTED = "rejected"  # Client rejected the annotation


@dataclass
class TagInfo:
    """Information about a single tag's state."""

    status: TagStatus
    patch_hash: str  # Hash of patch content for change detection
    pf_line: str  # Original pf_line content
    file_path: str  # Source file path


@dataclass
class TagStateStore:
    """Persistent store for tag states with file locking.

    State is stored in the .pf directory as tag_state.json.
    Uses file locking to ensure safe concurrent access.
    """

    # pf:invariant:TagStateStore.loaded_before_access _state only accessed after _ensure_loaded() called
    # pf:invariant:TagStateStore.state_persisted state in memory equals state on disk after any mutation
    # pf:invariant:TagStateStore.atomic_writes writes use temp file + rename for atomicity
    pf_dir: Path
    _state: dict[str, TagInfo] = field(default_factory=dict)
    _loaded: bool = False

    @property
    def state_file(self) -> Path:
        """Path to the state JSON file."""
        return self.pf_dir / "tag_state.json"

    def _ensure_loaded(self) -> None:
        """Load state from disk if not already loaded."""
        if self._loaded:
            return
        self._load()

    def _load(self) -> None:
        """Load state from disk with file locking."""
        self._state = {}

        if not self.state_file.exists():
            self._loaded = True
            return

        try:
            with open(self.state_file, "r") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                try:
                    data = json.load(f)
                    for tag, info in data.items():
                        self._state[tag] = TagInfo(
                            status=TagStatus(info["status"]),
                            patch_hash=info["patch_hash"],
                            pf_line=info["pf_line"],
                            file_path=info["file_path"],
                        )
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning("tag_state_load_failed", error=str(e))
            self._state = {}

        self._loaded = True

    def _save(self) -> None:
        """Save state to disk with exclusive file locking."""
        self.pf_dir.mkdir(parents=True, exist_ok=True)

        data: dict[str, Any] = {}
        for tag, info in self._state.items():
            data[tag] = {
                "status": info.status.value,
                "patch_hash": info.patch_hash,
                "pf_line": info.pf_line,
                "file_path": info.file_path,
            }

        # Write atomically with lock
        temp_file = self.state_file.with_suffix(".tmp")
        try:
            with open(temp_file, "w") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    json.dump(data, f, indent=2)
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            temp_file.rename(self.state_file)
        except Exception as e:
            logger.error("tag_state_save_failed", error=str(e))
            if temp_file.exists():
                temp_file.unlink()
            raise

    def get_status(self, tag: str) -> TagStatus | None:
        """Get the status of a tag, or None if not tracked."""
        self._ensure_loaded()
        info = self._state.get(tag)
        return info.status if info else None

    def get_info(self, tag: str) -> TagInfo | None:
        """Get full info for a tag, or None if not tracked."""
        self._ensure_loaded()
        return self._state.get(tag)

    def set_status(self, tag: str, status: TagStatus) -> bool:
        """Set the status of a tag. Returns True if tag exists."""
        # pf:requires:set_status.tag_exists tag must exist in _state for status update to succeed
        # pf:ensures:set_status.persisted_on_success if returns True, new status is persisted to disk
        self._ensure_loaded()
        if tag not in self._state:
            logger.warning("tag_state_set_unknown", tag=tag, status=status.value)
            return False
        self._state[tag].status = status
        self._save()
        logger.debug("tag_state_updated", tag=tag, status=status.value)
        return True

    def upsert_tag(
        self,
        tag: str,
        patch_content: str,
        pf_line: str,
        file_path: str,
    ) -> TagStatus:
        """Insert or update a tag based on patch content changes.

        - If tag doesn't exist: create as UNSEEN
        - If tag exists with same patch_hash: keep current status
        - If tag exists with different patch_hash: reset to UNSEEN

        Returns the resulting status.
        """
        # pf:ensures:upsert_tag.new_tag_unseen if tag was new, returns UNSEEN
        # pf:ensures:upsert_tag.unchanged_preserves if tag exists and patch_hash unchanged, returns existing status
        # pf:ensures:upsert_tag.changed_resets if tag exists and patch_hash changed, returns UNSEEN
        self._ensure_loaded()
        new_hash = self._compute_hash(patch_content)

        existing = self._state.get(tag)
        if existing is None:
            # New tag
            self._state[tag] = TagInfo(
                status=TagStatus.UNSEEN,
                patch_hash=new_hash,
                pf_line=pf_line,
                file_path=file_path,
            )
            self._save()
            logger.debug("tag_state_created", tag=tag, file_path=file_path)
            return TagStatus.UNSEEN

        if existing.patch_hash != new_hash:
            # Content changed - reset to UNSEEN
            logger.info(
                "tag_state_content_changed",
                tag=tag,
                old_status=existing.status.value,
            )
            existing.status = TagStatus.UNSEEN
            existing.patch_hash = new_hash
            existing.pf_line = pf_line
            existing.file_path = file_path
            self._save()
            return TagStatus.UNSEEN

        # No change
        return existing.status

    def get_unseen_tags(self) -> list[str]:
        """Get all tags with UNSEEN status."""
        self._ensure_loaded()
        return [
            tag for tag, info in self._state.items() if info.status == TagStatus.UNSEEN
        ]

    def mark_sent(self, tags: list[str]) -> int:
        # pf:requires:mark_sent.only_unseen_transition only transitions UNSEEN tags to SENT, ignores non-UNSEEN
        # pf:ensures:mark_sent.returns_count returns the number of tags actually transitioned
        """Mark multiple tags as SENT. Returns count of tags updated."""
        self._ensure_loaded()
        count = 0
        for tag in tags:
            if tag in self._state and self._state[tag].status == TagStatus.UNSEEN:
                self._state[tag].status = TagStatus.SENT
                count += 1
        if count > 0:
            self._save()
            logger.debug("tag_state_marked_sent", count=count)
        return count

    def get_all_tags(self) -> dict[str, TagInfo]:
        """Get all tracked tags and their info."""
        self._ensure_loaded()
        return dict(self._state)

    def remove_tag(self, tag: str) -> bool:
        """Remove a tag from tracking. Returns True if tag existed."""
        self._ensure_loaded()
        if tag in self._state:
            del self._state[tag]
            self._save()
            logger.debug("tag_state_removed", tag=tag)
            return True
        return False

    def clear(self) -> None:
        """Clear all state."""
        self._state = {}
        self._loaded = True
        if self.state_file.exists():
            self.state_file.unlink()
        logger.debug("tag_state_cleared")

    @staticmethod
    def _compute_hash(content: str) -> str:
        """Compute a stable hash of patch content."""
        # pf:ensures:_compute_hash.deterministic same content always produces same hash
        # pf:ensures:_compute_hash.fixed_length returns exactly 16 hex characters
        return hashlib.sha256(content.encode()).hexdigest()[:16]


def extract_tag_from_pf_line(pf_line: str) -> tuple[str, str, str] | None:
    """Extract annotation type, tag, and text from a pf_line.

    Args:
        pf_line: Line content like "# pf:inv:my_invariant x > 0"
                 or "    # pf:requires:positive_amount amount > 0"

    Returns:
        Tuple of (type, tag, text) or None if not a valid pf line.
        Example: ("inv", "my_invariant", "x > 0")
    """
    # pf:ensures:extract_tag_from_pf_line.returns_tuple_or_none returns (type, tag, text) tuple or None
    # pf:ensures:extract_tag_from_pf_line.strips_text returned text has whitespace stripped
    match = PF_LINE_REGEX.search(pf_line)
    if not match:
        return None
    return (match.group(1), match.group(2), match.group(3).strip())


def extract_tag_from_accepted_line(line_content: str) -> str | None:
    """Extract just the tag from an accepted/rejected line.

    This is used when processing client feedback where we receive
    the full line content from acceptedLines/rejectedLines.

    Args:
        line_content: Full line content like "    # pf:inv:my_tag some text"

    Returns:
        The tag string (e.g., "my_tag") or None if not parseable.
    """
    parsed = extract_tag_from_pf_line(line_content)
    return parsed[1] if parsed else None
