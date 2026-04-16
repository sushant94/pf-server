"""Tests for TagStateStore and patch filtering logic.

Tests cover:
1. TagStateStore lifecycle (UNSEEN → SENT → ACCEPTED/REJECTED)
2. Duplicate filtering (only UNSEEN patches are returned)
3. Content change detection (modified patches reset to UNSEEN)
4. Invariant verification (_save_hunks registers tags before get_patch_contents)
5. Round-trip persistence (state survives load/save cycles)
"""

import pytest
from pathlib import Path

from pf_server.repo_manager.tag_state import (
    TagStateStore,
    TagStatus,
    TagInfo,
    extract_tag_from_pf_line,
    extract_tag_from_accepted_line,
)


@pytest.fixture
def pf_dir(tmp_path: Path) -> Path:
    """Create a temporary .pf directory."""
    pf = tmp_path / ".pf"
    pf.mkdir()
    return pf


@pytest.fixture
def tag_store(pf_dir: Path) -> TagStateStore:
    """Create a TagStateStore with a temporary directory."""
    return TagStateStore(pf_dir=pf_dir)


# --- TagStateStore Lifecycle Tests ---


class TestTagStateStoreLifecycle:
    """Test basic tag lifecycle: UNSEEN → SENT → ACCEPTED/REJECTED."""

    def test_new_tag_is_unseen(self, tag_store: TagStateStore):
        """New tags are created with UNSEEN status."""
        status = tag_store.upsert_tag(
            tag="my_invariant",
            patch_content="diff --git a/foo.py\n+# pf:inv:my_invariant x > 0",
            pf_line="# pf:inv:my_invariant x > 0",
            file_path="foo.py",
        )

        assert status == TagStatus.UNSEEN
        assert tag_store.get_status("my_invariant") == TagStatus.UNSEEN

    def test_mark_sent_transitions_unseen_to_sent(self, tag_store: TagStateStore):
        """mark_sent() transitions UNSEEN tags to SENT."""
        tag_store.upsert_tag(
            tag="tag1",
            patch_content="patch1",
            pf_line="# pf:inv:tag1 foo",
            file_path="foo.py",
        )

        count = tag_store.mark_sent(["tag1"])

        assert count == 1
        assert tag_store.get_status("tag1") == TagStatus.SENT

    def test_mark_sent_ignores_non_unseen_tags(self, tag_store: TagStateStore):
        """mark_sent() only transitions UNSEEN tags, ignores others."""
        tag_store.upsert_tag(
            tag="tag1",
            patch_content="patch1",
            pf_line="# pf:inv:tag1 foo",
            file_path="foo.py",
        )
        tag_store.mark_sent(["tag1"])  # Now SENT
        tag_store.set_status("tag1", TagStatus.ACCEPTED)  # Now ACCEPTED

        count = tag_store.mark_sent(["tag1"])

        assert count == 0
        assert tag_store.get_status("tag1") == TagStatus.ACCEPTED

    def test_set_status_to_accepted(self, tag_store: TagStateStore):
        """set_status() can transition to ACCEPTED."""
        tag_store.upsert_tag(
            tag="tag1",
            patch_content="patch1",
            pf_line="# pf:inv:tag1 foo",
            file_path="foo.py",
        )
        tag_store.mark_sent(["tag1"])

        result = tag_store.set_status("tag1", TagStatus.ACCEPTED)

        assert result is True
        assert tag_store.get_status("tag1") == TagStatus.ACCEPTED

    def test_set_status_to_rejected(self, tag_store: TagStateStore):
        """set_status() can transition to REJECTED."""
        tag_store.upsert_tag(
            tag="tag1",
            patch_content="patch1",
            pf_line="# pf:inv:tag1 foo",
            file_path="foo.py",
        )
        tag_store.mark_sent(["tag1"])

        result = tag_store.set_status("tag1", TagStatus.REJECTED)

        assert result is True
        assert tag_store.get_status("tag1") == TagStatus.REJECTED

    def test_set_status_unknown_tag_returns_false(self, tag_store: TagStateStore):
        """set_status() returns False for unknown tags."""
        result = tag_store.set_status("nonexistent", TagStatus.ACCEPTED)

        assert result is False

    def test_get_status_unknown_tag_returns_none(self, tag_store: TagStateStore):
        """get_status() returns None for unknown tags."""
        assert tag_store.get_status("nonexistent") is None


# --- Duplicate Filtering Tests ---


class TestDuplicateFiltering:
    """Test that only UNSEEN patches are returned, SENT/ACCEPTED/REJECTED are filtered."""

    def test_unseen_tags_are_returned(self, tag_store: TagStateStore):
        """get_unseen_tags() returns tags with UNSEEN status."""
        tag_store.upsert_tag("tag1", "patch1", "# pf:inv:tag1 foo", "foo.py")
        tag_store.upsert_tag("tag2", "patch2", "# pf:inv:tag2 bar", "bar.py")

        unseen = tag_store.get_unseen_tags()

        assert set(unseen) == {"tag1", "tag2"}

    def test_sent_tags_not_in_unseen(self, tag_store: TagStateStore):
        """SENT tags are not returned by get_unseen_tags()."""
        tag_store.upsert_tag("tag1", "patch1", "# pf:inv:tag1 foo", "foo.py")
        tag_store.upsert_tag("tag2", "patch2", "# pf:inv:tag2 bar", "bar.py")
        tag_store.mark_sent(["tag1"])

        unseen = tag_store.get_unseen_tags()

        assert unseen == ["tag2"]

    def test_accepted_tags_not_in_unseen(self, tag_store: TagStateStore):
        """ACCEPTED tags are not returned by get_unseen_tags()."""
        tag_store.upsert_tag("tag1", "patch1", "# pf:inv:tag1 foo", "foo.py")
        tag_store.mark_sent(["tag1"])
        tag_store.set_status("tag1", TagStatus.ACCEPTED)

        unseen = tag_store.get_unseen_tags()

        assert unseen == []

    def test_rejected_tags_not_in_unseen(self, tag_store: TagStateStore):
        """REJECTED tags are not returned by get_unseen_tags()."""
        tag_store.upsert_tag("tag1", "patch1", "# pf:inv:tag1 foo", "foo.py")
        tag_store.mark_sent(["tag1"])
        tag_store.set_status("tag1", TagStatus.REJECTED)

        unseen = tag_store.get_unseen_tags()

        assert unseen == []

    def test_mixed_statuses_only_unseen_returned(self, tag_store: TagStateStore):
        """Only UNSEEN tags returned when store has mixed statuses."""
        # Create tags with different statuses
        tag_store.upsert_tag("unseen1", "p1", "# pf:inv:unseen1 a", "a.py")
        tag_store.upsert_tag("unseen2", "p2", "# pf:inv:unseen2 b", "b.py")
        tag_store.upsert_tag("sent1", "p3", "# pf:inv:sent1 c", "c.py")
        tag_store.upsert_tag("accepted1", "p4", "# pf:inv:accepted1 d", "d.py")
        tag_store.upsert_tag("rejected1", "p5", "# pf:inv:rejected1 e", "e.py")

        tag_store.mark_sent(["sent1", "accepted1", "rejected1"])
        tag_store.set_status("accepted1", TagStatus.ACCEPTED)
        tag_store.set_status("rejected1", TagStatus.REJECTED)

        unseen = tag_store.get_unseen_tags()

        assert set(unseen) == {"unseen1", "unseen2"}


# --- Content Change Detection Tests ---


class TestContentChangeDetection:
    """Test that modified patch content resets status to UNSEEN."""

    def test_same_content_preserves_status(self, tag_store: TagStateStore):
        """Same patch content preserves existing status."""
        tag_store.upsert_tag("tag1", "patch_content_v1", "# pf:inv:tag1 foo", "foo.py")
        tag_store.mark_sent(["tag1"])
        tag_store.set_status("tag1", TagStatus.ACCEPTED)

        # Upsert with same content
        status = tag_store.upsert_tag(
            "tag1", "patch_content_v1", "# pf:inv:tag1 foo", "foo.py"
        )

        assert status == TagStatus.ACCEPTED
        assert tag_store.get_status("tag1") == TagStatus.ACCEPTED

    def test_changed_content_resets_to_unseen(self, tag_store: TagStateStore):
        """Changed patch content resets status to UNSEEN."""
        tag_store.upsert_tag("tag1", "patch_content_v1", "# pf:inv:tag1 foo", "foo.py")
        tag_store.mark_sent(["tag1"])
        tag_store.set_status("tag1", TagStatus.ACCEPTED)

        # Upsert with different content
        status = tag_store.upsert_tag(
            "tag1", "patch_content_v2_modified", "# pf:inv:tag1 foo > 0", "foo.py"
        )

        assert status == TagStatus.UNSEEN
        assert tag_store.get_status("tag1") == TagStatus.UNSEEN

    def test_changed_content_resets_rejected_to_unseen(self, tag_store: TagStateStore):
        """Changed content resets REJECTED status to UNSEEN (allows re-review)."""
        tag_store.upsert_tag("tag1", "patch_content_v1", "# pf:inv:tag1 foo", "foo.py")
        tag_store.mark_sent(["tag1"])
        tag_store.set_status("tag1", TagStatus.REJECTED)

        # Upsert with different content
        status = tag_store.upsert_tag(
            "tag1", "patch_content_v2_improved", "# pf:inv:tag1 foo >= 0", "foo.py"
        )

        assert status == TagStatus.UNSEEN

    def test_changed_content_resets_sent_to_unseen(self, tag_store: TagStateStore):
        """Changed content resets SENT status to UNSEEN."""
        tag_store.upsert_tag("tag1", "patch_content_v1", "# pf:inv:tag1 foo", "foo.py")
        tag_store.mark_sent(["tag1"])

        status = tag_store.upsert_tag(
            "tag1", "patch_content_v2", "# pf:inv:tag1 bar", "foo.py"
        )

        assert status == TagStatus.UNSEEN


# --- Persistence Tests ---


class TestPersistence:
    """Test that state survives load/save cycles."""

    def test_state_persists_across_instances(self, pf_dir: Path):
        """State is preserved when creating a new TagStateStore instance."""
        # First instance - create and modify state
        store1 = TagStateStore(pf_dir=pf_dir)
        store1.upsert_tag("tag1", "patch1", "# pf:inv:tag1 foo", "foo.py")
        store1.mark_sent(["tag1"])
        store1.set_status("tag1", TagStatus.ACCEPTED)

        # Second instance - should load persisted state
        store2 = TagStateStore(pf_dir=pf_dir)

        assert store2.get_status("tag1") == TagStatus.ACCEPTED

    def test_multiple_tags_persist(self, pf_dir: Path):
        """Multiple tags with different statuses persist correctly."""
        store1 = TagStateStore(pf_dir=pf_dir)
        store1.upsert_tag("unseen", "p1", "# pf:inv:unseen a", "a.py")
        store1.upsert_tag("sent", "p2", "# pf:inv:sent b", "b.py")
        store1.upsert_tag("accepted", "p3", "# pf:inv:accepted c", "c.py")
        store1.upsert_tag("rejected", "p4", "# pf:inv:rejected d", "d.py")

        store1.mark_sent(["sent", "accepted", "rejected"])
        store1.set_status("accepted", TagStatus.ACCEPTED)
        store1.set_status("rejected", TagStatus.REJECTED)

        # New instance
        store2 = TagStateStore(pf_dir=pf_dir)

        assert store2.get_status("unseen") == TagStatus.UNSEEN
        assert store2.get_status("sent") == TagStatus.SENT
        assert store2.get_status("accepted") == TagStatus.ACCEPTED
        assert store2.get_status("rejected") == TagStatus.REJECTED

    def test_tag_info_persists(self, pf_dir: Path):
        """Full TagInfo (including pf_line and file_path) persists."""
        store1 = TagStateStore(pf_dir=pf_dir)
        store1.upsert_tag(
            tag="my_tag",
            patch_content="diff content here",
            pf_line="# pf:requires:my_tag x > 0",
            file_path="src/module.py",
        )

        store2 = TagStateStore(pf_dir=pf_dir)
        info = store2.get_info("my_tag")

        assert info is not None
        assert info.status == TagStatus.UNSEEN
        assert info.pf_line == "# pf:requires:my_tag x > 0"
        assert info.file_path == "src/module.py"

    def test_empty_state_file_handled(self, pf_dir: Path):
        """Empty or missing state file is handled gracefully."""
        store = TagStateStore(pf_dir=pf_dir)

        # Should not raise, should return None for unknown tags
        assert store.get_status("nonexistent") is None
        assert store.get_unseen_tags() == []

    def test_corrupted_state_file_handled(self, pf_dir: Path):
        """Corrupted state file is handled gracefully."""
        # Write invalid JSON
        state_file = pf_dir / "tag_state.json"
        state_file.write_text("{ invalid json }")

        store = TagStateStore(pf_dir=pf_dir)

        # Should recover with empty state
        assert store.get_status("any") is None


# --- Tag Extraction Tests ---


class TestTagExtraction:
    """Test pf_line parsing utilities."""

    def test_extract_tag_from_pf_line_invariant(self):
        """Extract tag from invariant pf_line."""
        result = extract_tag_from_pf_line("# pf:inv:my_invariant x > 0")

        assert result == ("inv", "my_invariant", "x > 0")

    def test_extract_tag_from_pf_line_requires(self):
        """Extract tag from requires pf_line."""
        result = extract_tag_from_pf_line("# pf:requires:positive_amount amount > 0")

        assert result == ("requires", "positive_amount", "amount > 0")

    def test_extract_tag_from_pf_line_with_leading_whitespace(self):
        """Extract tag from pf_line with leading whitespace."""
        result = extract_tag_from_pf_line("    # pf:inv:indented foo")

        assert result == ("inv", "indented", "foo")

    def test_extract_tag_from_pf_line_invalid(self):
        """Invalid pf_line returns None."""
        assert extract_tag_from_pf_line("# not a pf line") is None
        assert extract_tag_from_pf_line("regular code") is None
        assert extract_tag_from_pf_line("") is None

    def test_extract_tag_from_accepted_line(self):
        """Extract just the tag from an accepted line."""
        tag = extract_tag_from_accepted_line("    # pf:inv:my_tag some text here")

        assert tag == "my_tag"

    def test_extract_tag_from_accepted_line_invalid(self):
        """Invalid line returns None."""
        assert extract_tag_from_accepted_line("not a pf line") is None


# --- Remove Tag Tests ---


class TestRemoveTag:
    """Test tag removal functionality."""

    def test_remove_existing_tag(self, tag_store: TagStateStore):
        """remove_tag() removes an existing tag."""
        tag_store.upsert_tag("tag1", "patch1", "# pf:inv:tag1 foo", "foo.py")

        result = tag_store.remove_tag("tag1")

        assert result is True
        assert tag_store.get_status("tag1") is None

    def test_remove_nonexistent_tag(self, tag_store: TagStateStore):
        """remove_tag() returns False for nonexistent tag."""
        result = tag_store.remove_tag("nonexistent")

        assert result is False

    def test_clear_removes_all_tags(self, tag_store: TagStateStore):
        """clear() removes all tags."""
        tag_store.upsert_tag("tag1", "p1", "# pf:inv:tag1 a", "a.py")
        tag_store.upsert_tag("tag2", "p2", "# pf:inv:tag2 b", "b.py")

        tag_store.clear()

        assert tag_store.get_all_tags() == {}


# --- Edge Cases ---


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_mark_sent_with_empty_list(self, tag_store: TagStateStore):
        """mark_sent() with empty list returns 0."""
        count = tag_store.mark_sent([])

        assert count == 0

    def test_mark_sent_with_unknown_tags(self, tag_store: TagStateStore):
        """mark_sent() ignores unknown tags."""
        tag_store.upsert_tag("known", "patch", "# pf:inv:known foo", "foo.py")

        count = tag_store.mark_sent(["known", "unknown1", "unknown2"])

        assert count == 1  # Only "known" was marked

    def test_get_all_tags(self, tag_store: TagStateStore):
        """get_all_tags() returns all tracked tags."""
        tag_store.upsert_tag("tag1", "p1", "# pf:inv:tag1 a", "a.py")
        tag_store.upsert_tag("tag2", "p2", "# pf:inv:tag2 b", "b.py")

        all_tags = tag_store.get_all_tags()

        assert set(all_tags.keys()) == {"tag1", "tag2"}
        assert all(isinstance(v, TagInfo) for v in all_tags.values())

    def test_hash_stability(self, tag_store: TagStateStore):
        """Same content produces same hash (no status reset)."""
        content = "patch content with special chars: éàü 中文"

        tag_store.upsert_tag("tag1", content, "# pf:inv:tag1 foo", "foo.py")
        tag_store.mark_sent(["tag1"])
        tag_store.set_status("tag1", TagStatus.ACCEPTED)

        # Re-upsert with identical content
        status = tag_store.upsert_tag("tag1", content, "# pf:inv:tag1 foo", "foo.py")

        assert status == TagStatus.ACCEPTED


# --- Regression Tests for Patch Filtering ---


class TestPatchFilteringRegression:
    """Regression tests for patch filtering edge cases.

    These tests verify fixes for bugs where patches with failed tag extraction
    were returned repeatedly (infinite loop) because they couldn't be tracked.
    """

    @pytest.fixture
    def repo_mgr(self, tmp_path: Path):
        """Create a RepoMgr with temporary directories."""
        from pf_server.repo_manager.manager import RepoMgr

        repo_dir = tmp_path / "repo"
        shadow_dir = tmp_path / "shadow"
        patches_dir = tmp_path / "patches"
        pf_dir = tmp_path / ".pf"

        repo_dir.mkdir()
        shadow_dir.mkdir()
        patches_dir.mkdir()
        pf_dir.mkdir()

        return RepoMgr(
            repo_dir=repo_dir,
            shadow_dir=shadow_dir,
            patches_dir=patches_dir,
            pf_dir=pf_dir,
        )

    def test_get_patch_contents_skips_unextractable_tags(self, repo_mgr):
        """get_patch_contents skips patches when tag extraction fails.

        Regression test: Previously, patches with failed tag extraction were
        included in the return list but never marked as SENT, causing them
        to be returned on every subsequent call (infinite loop).
        """

        # Create a patch file WITHOUT a valid pf line (no # pf: pattern)
        bad_patch = """diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -1,1 +1,2 @@
 def foo():
+    return 42
"""
        patch_file = repo_mgr.patches_dir / "0001_bad.patch"
        patch_file.write_text(bad_patch)

        # First call - should return empty (patch skipped due to no tag)
        patches1 = repo_mgr.get_patch_contents(mark_sent=True)
        assert len(patches1) == 0

        # Second call - should still be empty (not an infinite loop)
        patches2 = repo_mgr.get_patch_contents(mark_sent=True)
        assert len(patches2) == 0

    def test_get_patch_contents_processes_valid_tags(self, repo_mgr):
        """get_patch_contents correctly processes patches with valid tags."""
        # Create a patch file WITH a valid pf line
        good_patch = """diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -1,1 +1,2 @@
 def foo():
+    # pf:inv:my_invariant x > 0
"""
        patch_file = repo_mgr.patches_dir / "0001_good.patch"
        patch_file.write_text(good_patch)

        # Register the tag first (simulating what _save_hunks does)
        repo_mgr.tag_state.upsert_tag(
            tag="my_invariant",
            patch_content=good_patch,
            pf_line="# pf:inv:my_invariant x > 0",
            file_path="foo.py",
        )

        # First call - should return the patch and mark as SENT
        patches1 = repo_mgr.get_patch_contents(mark_sent=True)
        assert len(patches1) == 1

        # Second call - should return empty (already SENT)
        patches2 = repo_mgr.get_patch_contents(mark_sent=True)
        assert len(patches2) == 0

    def test_filter_unseen_spec_patches_skips_unextractable_tags(self, repo_mgr):
        """filter_unseen_spec_patches skips patches when tag extraction fails.

        Regression test: Same issue as get_patch_contents - patches with
        failed tag extraction were returned but never tracked.
        """
        from pf_server.models import SpecPatch

        # Create in-memory patches without valid pf lines
        bad_patches = [
            SpecPatch(id="1", patch="no pf line here"),
            SpecPatch(id="2", patch="also no pf pattern"),
        ]

        # First call - should return empty (patches skipped)
        filtered1 = repo_mgr.filter_unseen_spec_patches(bad_patches, mark_sent=True)
        assert len(filtered1) == 0

        # Second call with same patches - still empty (not infinite loop)
        filtered2 = repo_mgr.filter_unseen_spec_patches(bad_patches, mark_sent=True)
        assert len(filtered2) == 0

    def test_filter_unseen_spec_patches_processes_valid_tags(self, repo_mgr):
        """filter_unseen_spec_patches correctly processes patches with valid tags."""
        from pf_server.models import SpecPatch

        good_patch_content = """diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -1,1 +1,2 @@
 def foo():
+    # pf:inv:test_tag some property
"""
        patches = [SpecPatch(id="1", patch=good_patch_content)]

        # First call - should return the patch and mark as SENT
        filtered1 = repo_mgr.filter_unseen_spec_patches(patches, mark_sent=True)
        assert len(filtered1) == 1

        # Second call - should return empty (already SENT)
        filtered2 = repo_mgr.filter_unseen_spec_patches(patches, mark_sent=True)
        assert len(filtered2) == 0

    def test_mixed_valid_and_invalid_patches(self, repo_mgr):
        """Mixed valid and invalid patches are handled correctly."""
        from pf_server.models import SpecPatch

        good_patch = """diff --git a/foo.py b/foo.py
+    # pf:inv:valid_tag x > 0
"""
        bad_patch = "no pf pattern here"

        patches = [
            SpecPatch(id="good", patch=good_patch),
            SpecPatch(id="bad", patch=bad_patch),
        ]

        # Should only return the valid patch
        filtered = repo_mgr.filter_unseen_spec_patches(patches, mark_sent=True)
        assert len(filtered) == 1
        assert filtered[0].id == "good"
