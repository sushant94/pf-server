"""Lightweight hunk-level diff parser.

Parses unified diffs into individual, independently-applicable patches.
Each hunk can be applied, rejected, or retried without affecting others.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

# Regex for @@ -old_start,old_count +new_start,new_count @@ context
HUNK_HEADER_RE = re.compile(r"^@@ -(\d+),?(\d*) \+(\d+),?(\d*) @@(.*)$")

# Pattern used to filter lines for PF-specific patches
PF_LINE_FILTER_PAT = "# pf:"


def _is_pf_line(line: str) -> bool:
    """Check if a diff line (with +/- prefix) contains the PF filter pattern.

    Args:
        line: A single diff line including the +/- prefix

    Returns:
        True if the line is an addition or deletion containing "# pf:"
    """
    # pf:ensures:_is_pf_line.only_changes only returns True for +/- prefixed lines (not context)
    # pf:ensures:_is_pf_line.pattern_match returns True only if "# pf:" present in line content
    if not line or len(line) < 1:
        return False
    if line[0] not in ("+", "-"):
        return False
    content = line[1:]  # Remove +/- prefix
    return PF_LINE_FILTER_PAT in content


@dataclass
class Hunk:
    """A single contiguous block of changes, independently applicable.

    The `patch` field contains a complete unified diff that can be applied
    with `git apply` without any other context.
    """

    # pf:invariant:Hunk.patch_applies patch field is a complete unified diff applicable via git apply
    # pf:invariant:Hunk.file_path_set file_path is always set to the source file being modified

    # Location in the OLD file (before changes)
    old_start: int
    old_count: int

    # Location in the NEW file (after changes)
    new_start: int
    new_count: int

    # The @@ header line
    header: str

    # Function/class context from @@ line (e.g., "def merge():")
    context: str

    # Raw diff content (the +/- lines)
    content: str

    # COMPLETE independently-applicable patch
    # Includes --- a/path, +++ b/path, and the single @@ block
    patch: str

    # Source file path (extracted from --- line)
    file_path: str

    def write_patch(self, output_path: Path) -> Path:
        """Write this hunk as an applicable patch file."""
        output_path.write_text(self.patch)
        return output_path

    def additions(self) -> list[str]:
        """Lines added (without + prefix)."""
        return [line[1:] for line in self.content.splitlines() if line.startswith("+")]

    def deletions(self) -> list[str]:
        """Lines deleted (without - prefix)."""
        return [line[1:] for line in self.content.splitlines() if line.startswith("-")]


@dataclass
class HunkApplyResult:
    """Result of attempting to apply a hunk."""

    hunk: Hunk
    success: bool
    error: str | None = None


@dataclass
class FilteredPatch:
    """A single PF-filtered patch, ready for ordered application.

    These patches are cumulative: patch N assumes patches 1..N-1 have been applied.
    """

    # pf:invariant:FilteredPatch.cumulative patches are cumulative - N assumes 1..N-1 applied
    # pf:invariant:FilteredPatch.sequence_positive sequence_number is 1-indexed positive integer

    sequence_number: int  # 1-indexed sequence number
    file_path: str  # Clean file path (e.g., "src/module.py")
    patch: str  # Complete, independently-applicable unified diff
    pf_line: str  # The actual "# pf:" line content (without +/- prefix)


@dataclass
class FileDiff:
    """All hunks for a single file."""

    file_path: str
    old_path: str  # e.g., "a/src/module.py"
    new_path: str  # e.g., "b/src/module.py"
    hunks: list[Hunk] = field(default_factory=list)

    @property
    def total_additions(self) -> int:
        return sum(len(h.additions()) for h in self.hunks)

    @property
    def total_deletions(self) -> int:
        return sum(len(h.deletions()) for h in self.hunks)


def parse_diff(diff_text: str) -> list[FileDiff]:
    """Parse a unified diff (potentially multi-file) into FileDiff objects.

    Each hunk within each FileDiff has an independently-applicable `patch` field.

    Args:
        diff_text: Raw output from `git diff`

    Returns:
        List of FileDiff objects, one per file
    """
    # pf:requires:parse_diff.unified_format diff_text must be unified diff format (from git diff)
    # pf:ensures:parse_diff.empty_returns_empty empty diff_text returns empty list
    # pf:ensures:parse_diff.one_per_file returns one FileDiff per file in diff
    # pf:ensures:parse_diff.hunks_independent each hunk.patch is independently applicable
    if not diff_text.strip():
        return []

    file_diffs = []
    current_file_lines: list[str] = []

    for line in diff_text.splitlines(keepends=True):
        # New file starts with "diff --git"
        if line.startswith("diff --git "):
            if current_file_lines:
                fd = _parse_single_file_diff("".join(current_file_lines))
                if fd:
                    file_diffs.append(fd)
            current_file_lines = [line]
        else:
            current_file_lines.append(line)

    # Don't forget last file
    if current_file_lines:
        fd = _parse_single_file_diff("".join(current_file_lines))
        if fd:
            file_diffs.append(fd)

    return file_diffs


def _parse_single_file_diff(file_diff_text: str) -> FileDiff | None:
    """Parse diff text for a single file into a FileDiff with independent hunks."""
    lines = file_diff_text.splitlines(keepends=True)

    old_path = ""
    new_path = ""
    header_lines: list[str] = []
    content_start = 0

    for i, line in enumerate(lines):
        if line.startswith("--- "):
            old_path = line[4:].strip()
            header_lines.append(line)
        elif line.startswith("+++ "):
            new_path = line[4:].strip()
            header_lines.append(line)
        elif line.startswith("@@"):
            content_start = i
            break

    if not old_path or not new_path:
        return None

    # The file header that each hunk needs
    file_header = "".join(header_lines)

    # Extract clean file path (remove a/ or b/ prefix)
    clean_path = new_path
    if clean_path.startswith("b/"):
        clean_path = clean_path[2:]
    elif clean_path.startswith("a/"):
        clean_path = clean_path[2:]

    # Parse hunks
    hunks: list[Hunk] = []
    current_hunk_header: str | None = None
    current_hunk_lines: list[str] = []

    for line in lines[content_start:]:
        if line.startswith("@@"):
            # Save previous hunk
            if current_hunk_header is not None:
                hunk = _build_hunk(
                    current_hunk_header, current_hunk_lines, file_header, clean_path
                )
                hunks.append(hunk)
            current_hunk_header = line
            current_hunk_lines = []
        elif current_hunk_header is not None:
            current_hunk_lines.append(line)

    # Last hunk
    if current_hunk_header is not None:
        hunk = _build_hunk(
            current_hunk_header, current_hunk_lines, file_header, clean_path
        )
        hunks.append(hunk)

    return FileDiff(
        file_path=clean_path,
        old_path=old_path,
        new_path=new_path,
        hunks=hunks,
    )


def _build_hunk(
    header_line: str,
    content_lines: list[str],
    file_header: str,
    file_path: str,
) -> Hunk:
    """Build a Hunk with an independently-applicable patch.

    The patch includes:
    - --- a/path
    - +++ b/path
    - @@ header
    - content lines

    This can be directly applied with `git apply`.
    """
    match = HUNK_HEADER_RE.match(header_line.strip())
    if not match:
        raise ValueError(f"Invalid hunk header: {header_line}")

    content = "".join(content_lines)

    # Build complete, independent patch
    patch = file_header + header_line + content

    return Hunk(
        old_start=int(match.group(1)),
        old_count=int(match.group(2)) if match.group(2) else 1,
        new_start=int(match.group(3)),
        new_count=int(match.group(4)) if match.group(4) else 1,
        header=header_line.strip(),
        context=match.group(5).strip(),
        content=content,
        patch=patch,
        file_path=file_path,
    )


def filter_and_split_pf_hunks(file_diffs: list[FileDiff]) -> list[FilteredPatch]:
    """Filter and split hunks to only include lines containing '# pf:'.

    Each '# pf:' line becomes its own patch. Patches are cumulative:
    patch N assumes patches 1..N-1 have been applied.

    Args:
        file_diffs: Parsed file diffs from parse_diff()

    Returns:
        List of FilteredPatch objects with strict sequence numbers (1, 2, 3, ...)
    """
    # pf:ensures:filter_and_split_pf_hunks.only_pf_lines returned patches contain only lines with "# pf:" pattern
    # pf:ensures:filter_and_split_pf_hunks.sequential sequence_numbers are 1, 2, 3... with no gaps
    # pf:ensures:filter_and_split_pf_hunks.cumulative later patches assume earlier ones applied
    # pf:ensures:filter_and_split_pf_hunks.empty_if_no_pf returns empty list if no pf: lines in input
    result: list[FilteredPatch] = []
    sequence = 0

    for fd in file_diffs:
        for hunk in fd.hunks:
            patches = _split_hunk_by_pf_lines(hunk, fd.old_path, fd.new_path)
            for patch, pf_line in patches:
                sequence += 1
                result.append(
                    FilteredPatch(
                        sequence_number=sequence,
                        file_path=fd.file_path,
                        patch=patch,
                        pf_line=pf_line,
                    )
                )

    return result


def _split_hunk_by_pf_lines(
    hunk: Hunk, old_path: str, new_path: str
) -> list[tuple[str, str]]:
    """Split a single hunk into multiple patches, one per '# pf:' line.

    Returns list of (patch_text, pf_line_content) tuples.
    Each patch is cumulative - it assumes previous patches have been applied.
    """
    # Parse hunk content into lines (without trailing newlines for processing)
    raw_lines = hunk.content.splitlines()

    # Find all PF lines with their indices
    pf_indices: list[int] = []
    for i, line in enumerate(raw_lines):
        if _is_pf_line(line):
            pf_indices.append(i)

    if not pf_indices:
        return []

    result: list[tuple[str, str]] = []

    # Build the file header
    file_header = f"--- {old_path}\n+++ {new_path}\n"

    # For each PF line, build a cumulative patch
    for pf_idx, current_pf_line_idx in enumerate(pf_indices):
        patch_lines: list[str] = []

        # Track line counts for the @@ header
        # old_count: lines in old file (context + deletions)
        # new_count: lines in new file (context + additions)
        old_count = 0
        new_count = 0

        for i, line in enumerate(raw_lines):
            if not line:
                continue

            prefix = line[0]
            content = line[1:] if len(line) > 1 else ""

            # Is this a PF line?
            is_pf = i in pf_indices

            if is_pf:
                # Which PF line is this?
                this_pf_idx = pf_indices.index(i)

                if this_pf_idx < pf_idx:
                    # Previous PF line - becomes context (assume already applied)
                    if prefix == "+":
                        # Was added, now exists as context
                        patch_lines.append(" " + content)
                        old_count += 1
                        new_count += 1
                    elif prefix == "-":
                        # Was deleted, already removed - skip entirely
                        pass
                elif this_pf_idx == pf_idx:
                    # Current PF line - this is the change we're making
                    patch_lines.append(line)
                    if prefix == "+":
                        new_count += 1
                    elif prefix == "-":
                        old_count += 1
                else:
                    # Future PF line - skip (not yet applied)
                    pass
            else:
                # Non-PF line
                if prefix == " ":
                    # Context line - always include
                    patch_lines.append(line)
                    old_count += 1
                    new_count += 1
                elif prefix == "+":
                    # Non-PF addition - skip (we only want PF lines)
                    pass
                elif prefix == "-":
                    # Non-PF deletion - skip
                    pass

        if not patch_lines:
            continue

        # Calculate the old_start and new_start
        # For cumulative patches, we need to adjust based on previous PF additions
        # The old_start is the hunk's original old_start
        # The new_start may differ based on previous additions
        old_start = hunk.old_start
        new_start = hunk.new_start

        # Adjust new_start for previous additions within this hunk
        # (Each previously applied addition shifts lines down by 1)
        additions_before = sum(
            1 for idx in pf_indices[:pf_idx] if raw_lines[idx].startswith("+")
        )
        deletions_before = sum(
            1 for idx in pf_indices[:pf_idx] if raw_lines[idx].startswith("-")
        )
        new_start = hunk.new_start + additions_before - deletions_before

        # Build the @@ header
        hunk_header = f"@@ -{old_start},{old_count} +{new_start},{new_count} @@"
        if hunk.context:
            hunk_header += " " + hunk.context
        hunk_header += "\n"

        # Assemble the complete patch
        patch_content = "\n".join(patch_lines) + "\n"
        full_patch = file_header + hunk_header + patch_content

        # Extract the pf_line content (without prefix)
        current_line = raw_lines[current_pf_line_idx]
        pf_line_content = current_line[1:] if len(current_line) > 1 else ""

        result.append((full_patch, pf_line_content))

    return result
