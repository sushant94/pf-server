"""Tests for diff_parser module."""

import subprocess

import pytest

from pf_server.repo_manager.diff_parser import (
    _is_pf_line,
    filter_and_split_pf_hunks,
    parse_diff,
)


class TestParseDiff:
    """Tests for parse_diff function."""

    def test_empty_diff(self):
        """Empty diff returns empty list."""
        assert parse_diff("") == []
        assert parse_diff("   \n  ") == []

    def test_single_file_single_hunk(self):
        """Parse a simple single-file, single-hunk diff."""
        diff = """\
diff --git a/file.py b/file.py
index abc123..def456 100644
--- a/file.py
+++ b/file.py
@@ -10,3 +10,4 @@ def foo():
     pass
+    # added line
"""
        result = parse_diff(diff)

        assert len(result) == 1
        fd = result[0]
        assert fd.file_path == "file.py"
        assert fd.old_path == "a/file.py"
        assert fd.new_path == "b/file.py"
        assert len(fd.hunks) == 1

        hunk = fd.hunks[0]
        assert hunk.old_start == 10
        assert hunk.old_count == 3
        assert hunk.new_start == 10
        assert hunk.new_count == 4
        assert hunk.context == "def foo():"
        assert "+    # added line" in hunk.content
        assert hunk.file_path == "file.py"

        # Verify patch is independently applicable
        assert "--- a/file.py" in hunk.patch
        assert "+++ b/file.py" in hunk.patch
        assert "@@ -10,3 +10,4 @@" in hunk.patch

    def test_single_file_multiple_hunks(self):
        """Each hunk gets its own complete patch headers."""
        diff = """\
diff --git a/file.py b/file.py
index abc123..def456 100644
--- a/file.py
+++ b/file.py
@@ -10,3 +10,4 @@ def foo():
     pass
+    # hunk 1
@@ -50,3 +51,4 @@ def bar():
     pass
+    # hunk 2
"""
        result = parse_diff(diff)

        assert len(result) == 1
        fd = result[0]
        assert len(fd.hunks) == 2

        # Verify each hunk has complete, independent headers
        for hunk in fd.hunks:
            assert "--- a/file.py" in hunk.patch
            assert "+++ b/file.py" in hunk.patch

        # Verify hunks have correct content
        assert "# hunk 1" in fd.hunks[0].content
        assert "# hunk 2" in fd.hunks[1].content

        # Verify line numbers
        assert fd.hunks[0].new_start == 10
        assert fd.hunks[1].new_start == 51

    def test_multiple_files(self):
        """Parse diff with multiple files."""
        diff = """\
diff --git a/src/foo.py b/src/foo.py
index abc123..def456 100644
--- a/src/foo.py
+++ b/src/foo.py
@@ -1,3 +1,4 @@
 import os
+import sys
diff --git a/src/bar.py b/src/bar.py
index 111222..333444 100644
--- a/src/bar.py
+++ b/src/bar.py
@@ -5,3 +5,4 @@ class Bar:
     pass
+    # bar change
"""
        result = parse_diff(diff)

        assert len(result) == 2
        assert result[0].file_path == "src/foo.py"
        assert result[1].file_path == "src/bar.py"

        # Each file's hunks have correct file headers
        assert "--- a/src/foo.py" in result[0].hunks[0].patch
        assert "--- a/src/bar.py" in result[1].hunks[0].patch

    def test_new_file(self):
        """Parse diff for a newly created file (/dev/null as old)."""
        diff = """\
diff --git a/newfile.py b/newfile.py
new file mode 100644
index 0000000..abc1234
--- /dev/null
+++ b/newfile.py
@@ -0,0 +1,3 @@
+# New file
+def hello():
+    pass
"""
        result = parse_diff(diff)

        assert len(result) == 1
        fd = result[0]
        assert fd.file_path == "newfile.py"
        assert fd.old_path == "/dev/null"
        assert fd.new_path == "b/newfile.py"
        assert len(fd.hunks) == 1
        assert fd.total_additions == 3
        assert fd.total_deletions == 0

    def test_deleted_file(self):
        """Parse diff for a deleted file (/dev/null as new)."""
        diff = """\
diff --git a/oldfile.py b/oldfile.py
deleted file mode 100644
index abc1234..0000000
--- a/oldfile.py
+++ /dev/null
@@ -1,3 +0,0 @@
-# Old file
-def goodbye():
-    pass
"""
        result = parse_diff(diff)

        assert len(result) == 1
        fd = result[0]
        # For deleted files, we still extract from new_path but it's /dev/null
        assert fd.old_path == "a/oldfile.py"
        assert fd.new_path == "/dev/null"
        assert fd.total_additions == 0
        assert fd.total_deletions == 3

    def test_hunk_additions_deletions(self):
        """Test additions() and deletions() methods."""
        diff = """\
diff --git a/file.py b/file.py
--- a/file.py
+++ b/file.py
@@ -1,4 +1,4 @@
 line1
-old line
+new line
 line3
"""
        result = parse_diff(diff)
        hunk = result[0].hunks[0]

        assert hunk.additions() == ["new line"]
        assert hunk.deletions() == ["old line"]

    def test_hunk_without_count(self):
        """Handle hunks where count is omitted (implies 1)."""
        diff = """\
diff --git a/file.py b/file.py
--- a/file.py
+++ b/file.py
@@ -5 +5,2 @@ def single():
     original
+    added
"""
        result = parse_diff(diff)
        hunk = result[0].hunks[0]

        # When count is omitted, it defaults to 1
        assert hunk.old_start == 5
        assert hunk.old_count == 1
        assert hunk.new_start == 5
        assert hunk.new_count == 2

    def test_file_diff_totals(self):
        """Test FileDiff total_additions and total_deletions."""
        diff = """\
diff --git a/file.py b/file.py
--- a/file.py
+++ b/file.py
@@ -1,3 +1,4 @@
 a
+b
@@ -10,4 +11,3 @@
 x
-y
-z
+w
"""
        result = parse_diff(diff)
        fd = result[0]

        # First hunk: +1, Second hunk: +1 -2
        assert fd.total_additions == 2
        assert fd.total_deletions == 2


class TestHunk:
    """Tests for Hunk dataclass."""

    def test_write_patch(self, tmp_path):
        """Test writing hunk to a patch file."""
        diff = """\
diff --git a/file.py b/file.py
--- a/file.py
+++ b/file.py
@@ -1,3 +1,4 @@
 line
+added
"""
        result = parse_diff(diff)
        hunk = result[0].hunks[0]

        patch_file = tmp_path / "test.patch"
        hunk.write_patch(patch_file)

        content = patch_file.read_text()
        assert "--- a/file.py" in content
        assert "+++ b/file.py" in content
        assert "+added" in content


@pytest.fixture
def git_repo(tmp_path):
    """Create a temporary git repo with initial commit."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def run(cmd: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            cmd, shell=True, cwd=repo, capture_output=True, text=True, check=True
        )

    # Initialize repo
    run("git init")
    run("git config user.email 'test@test.com'")
    run("git config user.name 'Test'")

    # Create initial files
    (repo / "foo.py").write_text("def foo():\n    pass\n")
    (repo / "bar.py").write_text("def bar():\n    return 1\n    # end\n")
    (repo / "src").mkdir()
    (repo / "src" / "module.py").write_text("import os\n\nclass Module:\n    pass\n")

    run("git add .")
    run("git commit -m 'initial'")

    return repo


class TestGitRoundtrip:
    """Integration tests using real git commands."""

    def test_single_file_modification_roundtrip(self, git_repo):
        """Modify a file -> diff -> parse -> restore -> apply -> verify."""

        def run(cmd: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                cmd,
                shell=True,
                cwd=git_repo,
                capture_output=True,
                text=True,
                check=True,
            )

        # Make a change
        (git_repo / "foo.py").write_text("def foo():\n    print('hello')\n    pass\n")

        # Get diff (same command as RepoMgr)
        result = run("git diff HEAD")
        diff_output = result.stdout

        # Parse into hunks
        file_diffs = parse_diff(diff_output)
        assert len(file_diffs) == 1
        assert file_diffs[0].file_path == "foo.py"
        assert len(file_diffs[0].hunks) == 1

        hunk = file_diffs[0].hunks[0]
        assert "print('hello')" in hunk.content

        # Restore the repo
        run("git restore .")

        # Verify restored
        content = (git_repo / "foo.py").read_text()
        assert "print('hello')" not in content

        # Apply the patch
        apply_result = subprocess.run(
            ["git", "apply", "-"],
            input=hunk.patch,
            cwd=git_repo,
            capture_output=True,
            text=True,
        )
        assert apply_result.returncode == 0, f"git apply failed: {apply_result.stderr}"

        # Verify applied
        content = (git_repo / "foo.py").read_text()
        assert "print('hello')" in content

    def test_multiple_hunks_same_file_roundtrip(self, git_repo):
        """Multiple hunks in same file can be applied independently."""

        def run(cmd: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                cmd,
                shell=True,
                cwd=git_repo,
                capture_output=True,
                text=True,
                check=True,
            )

        # Create a file with multiple sections
        original = "# header\n\ndef func1():\n    pass\n\n\ndef func2():\n    pass\n"
        (git_repo / "multi.py").write_text(original)
        run("git add multi.py && git commit -m 'add multi.py'")

        # Make changes in two different places
        modified = "# header\n# added comment\n\ndef func1():\n    print('func1')\n    pass\n\n\ndef func2():\n    print('func2')\n    pass\n"
        (git_repo / "multi.py").write_text(modified)

        # Get diff
        result = run("git diff HEAD")
        file_diffs = parse_diff(result.stdout)

        assert len(file_diffs) == 1
        fd = file_diffs[0]
        # Should have multiple hunks (depends on git's context algorithm)
        assert len(fd.hunks) >= 1

        # Each hunk should have independent headers
        for hunk in fd.hunks:
            assert "--- a/multi.py" in hunk.patch
            assert "+++ b/multi.py" in hunk.patch

        # Restore
        run("git restore .")

        # Apply each hunk independently and verify
        for i, hunk in enumerate(fd.hunks):
            apply_result = subprocess.run(
                ["git", "apply", "-"],
                input=hunk.patch,
                cwd=git_repo,
                capture_output=True,
                text=True,
            )
            assert apply_result.returncode == 0, (
                f"Hunk {i} failed to apply: {apply_result.stderr}"
            )

        # Final content should match the modified version
        content = (git_repo / "multi.py").read_text()
        assert "print('func1')" in content or "print('func2')" in content

    def test_multiple_files_roundtrip(self, git_repo):
        """Changes to multiple files can all be applied."""

        def run(cmd: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                cmd,
                shell=True,
                cwd=git_repo,
                capture_output=True,
                text=True,
                check=True,
            )

        # Modify multiple files
        (git_repo / "foo.py").write_text("def foo():\n    return 'foo'\n")
        (git_repo / "bar.py").write_text("def bar():\n    return 'bar'\n    # end\n")

        # Get diff
        result = run("git diff HEAD")
        file_diffs = parse_diff(result.stdout)

        assert len(file_diffs) == 2
        paths = {fd.file_path for fd in file_diffs}
        assert paths == {"foo.py", "bar.py"}

        # Collect all hunks
        all_hunks = []
        for fd in file_diffs:
            all_hunks.extend(fd.hunks)

        # Restore
        run("git restore .")

        # Apply all hunks
        for hunk in all_hunks:
            apply_result = subprocess.run(
                ["git", "apply", "-"],
                input=hunk.patch,
                cwd=git_repo,
                capture_output=True,
                text=True,
            )
            assert apply_result.returncode == 0, (
                f"Hunk for {hunk.file_path} failed: {apply_result.stderr}"
            )

        # Verify both files updated
        assert "return 'foo'" in (git_repo / "foo.py").read_text()
        assert "return 'bar'" in (git_repo / "bar.py").read_text()

    def test_new_file_roundtrip(self, git_repo):
        """New file can be created via patch."""

        def run(cmd: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                cmd,
                shell=True,
                cwd=git_repo,
                capture_output=True,
                text=True,
                check=True,
            )

        # Create a new file (untracked, then staged to get proper diff)
        (git_repo / "newfile.py").write_text("# brand new\ndef new():\n    pass\n")

        # For new files, we need --cached after staging, or use diff against empty tree
        run("git add newfile.py")
        result = run("git diff --cached HEAD")
        file_diffs = parse_diff(result.stdout)

        assert len(file_diffs) == 1
        fd = file_diffs[0]
        assert fd.file_path == "newfile.py"
        assert fd.old_path == "/dev/null"

        hunk = fd.hunks[0]

        # Reset (unstage and delete)
        run("git reset HEAD newfile.py")
        (git_repo / "newfile.py").unlink()
        assert not (git_repo / "newfile.py").exists()

        # Apply the patch - should recreate the file
        apply_result = subprocess.run(
            ["git", "apply", "-"],
            input=hunk.patch,
            cwd=git_repo,
            capture_output=True,
            text=True,
        )
        assert apply_result.returncode == 0, f"git apply failed: {apply_result.stderr}"

        # Verify file was created
        assert (git_repo / "newfile.py").exists()
        content = (git_repo / "newfile.py").read_text()
        assert "brand new" in content

    def test_subdirectory_file_roundtrip(self, git_repo):
        """Files in subdirectories work correctly."""

        def run(cmd: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                cmd,
                shell=True,
                cwd=git_repo,
                capture_output=True,
                text=True,
                check=True,
            )

        # Modify file in subdirectory
        (git_repo / "src" / "module.py").write_text(
            "import os\nimport sys\n\nclass Module:\n    value = 42\n"
        )

        # Get diff
        result = run("git diff HEAD")
        file_diffs = parse_diff(result.stdout)

        assert len(file_diffs) == 1
        fd = file_diffs[0]
        assert fd.file_path == "src/module.py"

        # Restore and apply
        run("git restore .")

        for hunk in fd.hunks:
            apply_result = subprocess.run(
                ["git", "apply", "-"],
                input=hunk.patch,
                cwd=git_repo,
                capture_output=True,
                text=True,
            )
            assert apply_result.returncode == 0

        content = (git_repo / "src" / "module.py").read_text()
        assert "import sys" in content
        assert "value = 42" in content

    def test_all_hunks_apply_after_full_restore(self, git_repo):
        """Full roundtrip: make changes, save all hunks, restore, apply all."""

        def run(cmd: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                cmd,
                shell=True,
                cwd=git_repo,
                capture_output=True,
                text=True,
                check=True,
            )

        # Make changes to all files
        (git_repo / "foo.py").write_text("def foo():\n    # modified\n    pass\n")
        (git_repo / "bar.py").write_text(
            "def bar():\n    return 2\n    # modified end\n"
        )
        (git_repo / "src" / "module.py").write_text(
            "import os\nimport sys\n\nclass Module:\n    modified = True\n"
        )

        # Capture original modified state for verification
        expected_foo = (git_repo / "foo.py").read_text()
        expected_bar = (git_repo / "bar.py").read_text()
        expected_module = (git_repo / "src" / "module.py").read_text()

        # Get diff (exact command from RepoMgr)
        result = run("git diff HEAD")
        file_diffs = parse_diff(result.stdout)

        # Collect all hunks
        all_hunks = []
        for fd in file_diffs:
            for hunk in fd.hunks:
                all_hunks.append(hunk)

        assert len(all_hunks) >= 3  # At least one hunk per file

        # Restore everything (exact command from RepoMgr)
        run("git restore .")

        # Verify clean state
        assert "modified" not in (git_repo / "foo.py").read_text()

        # Apply all hunks
        failed = []
        for hunk in all_hunks:
            apply_result = subprocess.run(
                ["git", "apply", "-"],
                input=hunk.patch,
                cwd=git_repo,
                capture_output=True,
                text=True,
            )
            if apply_result.returncode != 0:
                failed.append((hunk.file_path, apply_result.stderr))

        assert not failed, f"Failed hunks: {failed}"

        # Verify final state matches original modifications
        assert (git_repo / "foo.py").read_text() == expected_foo
        assert (git_repo / "bar.py").read_text() == expected_bar
        assert (git_repo / "src" / "module.py").read_text() == expected_module


class TestIsPfLine:
    """Tests for _is_pf_line helper function."""

    def test_addition_with_pf(self):
        """Addition line with # pf: is detected."""
        assert _is_pf_line("+# pf: some annotation") is True
        assert _is_pf_line("+    # pf: indented") is True
        assert _is_pf_line("+        # pf: more indent") is True

    def test_deletion_with_pf(self):
        """Deletion line with # pf: is detected."""
        assert _is_pf_line("-# pf: deleted annotation") is True
        assert _is_pf_line("-    # pf: indented deletion") is True

    def test_context_lines_not_matched(self):
        """Context lines (space prefix) are not matched."""
        assert _is_pf_line(" # pf: context") is False
        assert _is_pf_line("     # pf: indented context") is False

    def test_regular_code_not_matched(self):
        """Regular code without # pf: is not matched."""
        assert _is_pf_line("+ regular code") is False
        assert _is_pf_line("+# not pf comment") is False
        assert _is_pf_line("-def foo():") is False

    def test_empty_and_edge_cases(self):
        """Empty strings and edge cases handled correctly."""
        assert _is_pf_line("") is False
        assert _is_pf_line("+") is False
        assert _is_pf_line("-") is False
        assert _is_pf_line("x# pf:") is False


class TestFilterAndSplitPfHunks:
    """Tests for filter_and_split_pf_hunks function."""

    def test_no_pf_lines_returns_empty(self):
        """Diff without # pf: lines returns empty list."""
        diff = """\
diff --git a/file.py b/file.py
--- a/file.py
+++ b/file.py
@@ -1,3 +1,4 @@
 line1
 line2
+regular addition
"""
        file_diffs = parse_diff(diff)
        result = filter_and_split_pf_hunks(file_diffs)
        assert result == []

    def test_single_pf_line(self):
        """Single # pf: line creates one patch."""
        diff = """\
diff --git a/file.py b/file.py
--- a/file.py
+++ b/file.py
@@ -1,2 +1,3 @@
 existing1
 existing2
+    # pf: annotation
"""
        file_diffs = parse_diff(diff)
        result = filter_and_split_pf_hunks(file_diffs)

        assert len(result) == 1
        fp = result[0]
        assert fp.sequence_number == 1
        assert fp.file_path == "file.py"
        assert "# pf: annotation" in fp.pf_line
        assert "+    # pf: annotation" in fp.patch

    def test_multiple_consecutive_pf_lines_split(self):
        """Consecutive # pf: lines are split into separate patches."""
        diff = """\
diff --git a/file.py b/file.py
--- a/file.py
+++ b/file.py
@@ -10,3 +10,6 @@ def foo():
     existing1
     existing2
     existing3
+    # pf: first
+    # pf: second
+    # pf: third
"""
        file_diffs = parse_diff(diff)
        result = filter_and_split_pf_hunks(file_diffs)

        assert len(result) == 3
        assert result[0].sequence_number == 1
        assert result[1].sequence_number == 2
        assert result[2].sequence_number == 3

        assert "first" in result[0].pf_line
        assert "second" in result[1].pf_line
        assert "third" in result[2].pf_line

    def test_cumulative_context_in_patches(self):
        """Later patches include earlier PF lines as context."""
        diff = """\
diff --git a/file.py b/file.py
--- a/file.py
+++ b/file.py
@@ -1,2 +1,4 @@
 context
 more
+    # pf: first
+    # pf: second
"""
        file_diffs = parse_diff(diff)
        result = filter_and_split_pf_hunks(file_diffs)

        assert len(result) == 2

        # First patch should have the first pf line as addition
        assert "+    # pf: first" in result[0].patch
        assert "# pf: second" not in result[0].patch

        # Second patch should have first as context (space prefix)
        # and second as addition (+ prefix)
        assert " " + "   # pf: first" in result[1].patch  # context
        assert "+    # pf: second" in result[1].patch  # addition

    def test_indentation_preserved(self):
        """Indentation of # pf: lines is preserved."""
        diff = """\
diff --git a/file.py b/file.py
--- a/file.py
+++ b/file.py
@@ -1,1 +1,2 @@
 base
+        # pf: eight spaces
"""
        file_diffs = parse_diff(diff)
        result = filter_and_split_pf_hunks(file_diffs)

        assert len(result) == 1
        # 8 spaces of indentation should be preserved
        assert "        # pf: eight spaces" in result[0].pf_line

    def test_deletion_pf_line(self):
        """Deletion of # pf: line creates a deletion patch."""
        diff = """\
diff --git a/file.py b/file.py
--- a/file.py
+++ b/file.py
@@ -1,3 +1,2 @@
 context
-    # pf: old annotation
 more
"""
        file_diffs = parse_diff(diff)
        result = filter_and_split_pf_hunks(file_diffs)

        assert len(result) == 1
        assert "-    # pf: old annotation" in result[0].patch

    def test_sequence_numbers_across_files(self):
        """Sequence numbers continue across multiple files."""
        diff = """\
diff --git a/first.py b/first.py
--- a/first.py
+++ b/first.py
@@ -1,1 +1,2 @@
 a
+# pf: in first
diff --git a/second.py b/second.py
--- a/second.py
+++ b/second.py
@@ -1,1 +1,2 @@
 b
+# pf: in second
"""
        file_diffs = parse_diff(diff)
        result = filter_and_split_pf_hunks(file_diffs)

        assert len(result) == 2
        assert result[0].sequence_number == 1
        assert result[0].file_path == "first.py"
        assert result[1].sequence_number == 2
        assert result[1].file_path == "second.py"

    def test_non_pf_additions_excluded(self):
        """Non-PF additions are not included in filtered patches."""
        diff = """\
diff --git a/file.py b/file.py
--- a/file.py
+++ b/file.py
@@ -1,2 +1,5 @@
 context
+regular line
+    # pf: annotation
+another regular
 end
"""
        file_diffs = parse_diff(diff)
        result = filter_and_split_pf_hunks(file_diffs)

        assert len(result) == 1
        # Only the pf line should be in the patch, not the regular additions
        assert "+    # pf: annotation" in result[0].patch
        assert "regular line" not in result[0].patch
        assert "another regular" not in result[0].patch


class TestPfFilteredRoundtrip:
    """Integration tests for PF-filtered patches using real git."""

    def test_single_pf_line_applies_cleanly(self, git_repo):
        """Single # pf: line patch applies correctly."""

        def run(cmd: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                cmd,
                shell=True,
                cwd=git_repo,
                capture_output=True,
                text=True,
                check=True,
            )

        # Add a # pf: annotation
        original = "def foo():\n    pass\n"
        modified = "def foo():\n    # pf: requires auth\n    pass\n"
        (git_repo / "foo.py").write_text(modified)

        # Get diff and filter
        result = run("git diff HEAD")
        file_diffs = parse_diff(result.stdout)
        filtered = filter_and_split_pf_hunks(file_diffs)

        assert len(filtered) == 1

        # Restore
        run("git restore .")
        assert (git_repo / "foo.py").read_text() == original

        # Apply the filtered patch
        apply_result = subprocess.run(
            ["git", "apply", "-"],
            input=filtered[0].patch,
            cwd=git_repo,
            capture_output=True,
            text=True,
        )
        assert apply_result.returncode == 0, f"Apply failed: {apply_result.stderr}"

        # Verify the annotation was added
        content = (git_repo / "foo.py").read_text()
        assert "# pf: requires auth" in content

    def test_consecutive_pf_lines_apply_in_sequence(self, git_repo):
        """Multiple consecutive # pf: lines apply correctly in sequence."""

        def run(cmd: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                cmd,
                shell=True,
                cwd=git_repo,
                capture_output=True,
                text=True,
                check=True,
            )

        # Create file with base content
        base = "def func():\n    x = 1\n    return x\n"
        (git_repo / "test.py").write_text(base)
        run("git add test.py && git commit -m 'add test.py'")

        # Add multiple # pf: annotations
        modified = "def func():\n    # pf: first\n    # pf: second\n    # pf: third\n    x = 1\n    return x\n"
        (git_repo / "test.py").write_text(modified)

        # Get diff and filter
        result = run("git diff HEAD")
        file_diffs = parse_diff(result.stdout)
        filtered = filter_and_split_pf_hunks(file_diffs)

        assert len(filtered) == 3

        # Restore to clean state
        run("git restore .")

        # Apply each patch in sequence
        for i, fp in enumerate(filtered):
            apply_result = subprocess.run(
                ["git", "apply", "-"],
                input=fp.patch,
                cwd=git_repo,
                capture_output=True,
                text=True,
            )
            assert apply_result.returncode == 0, (
                f"Patch {i + 1} failed: {apply_result.stderr}\n"
                f"Patch content:\n{fp.patch}"
            )

        # Verify final state has all three annotations
        content = (git_repo / "test.py").read_text()
        assert "# pf: first" in content
        assert "# pf: second" in content
        assert "# pf: third" in content

    def test_mixed_pf_and_regular_changes(self, git_repo):
        """Only # pf: lines are extracted when mixed with regular changes."""

        def run(cmd: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                cmd,
                shell=True,
                cwd=git_repo,
                capture_output=True,
                text=True,
                check=True,
            )

        # Modify foo.py with both regular changes and # pf: annotation
        modified = "def foo():\n    print('hello')\n    # pf: logged\n    pass\n"
        (git_repo / "foo.py").write_text(modified)

        # Get diff and filter
        result = run("git diff HEAD")
        file_diffs = parse_diff(result.stdout)
        filtered = filter_and_split_pf_hunks(file_diffs)

        # Should only have the # pf: line
        assert len(filtered) == 1
        assert "# pf: logged" in filtered[0].pf_line

        # The patch should not contain the print line
        assert "print('hello')" not in filtered[0].patch
