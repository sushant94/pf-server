"""
MCP server for ProofFactory annotations.

Exposes tools for querying annotations and verification results
to coding agents via the Model Context Protocol.

Two server factories are provided:
- create_server(project_root): For stdio transport with fixed project root
- create_authenticated_server(): For HTTP transport with JWT auth and per-user projects
"""

from pathlib import Path
from typing import Optional

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers

from proofactory.mining.verifier import AnnotationResult, AnnotationStatus
from proofactory.paths import PFPaths
from proofactory.storage.yaml_backend import YamlBackend

from pf_server.logging_config import get_logger
from pf_server.guess import ask_question_analysis


logger = get_logger(__name__)

# Cache YamlBackend instances by project root to avoid recreating them
_store_cache: dict[Path, YamlBackend] = {}


def _get_store_for_path(project_root: Path) -> YamlBackend:
    """Get or create a YamlBackend for the given project root."""
    if project_root not in _store_cache:
        paths = PFPaths(project_root=project_root)
        _store_cache[project_root] = YamlBackend(paths)
    return _store_cache[project_root]


def _get_project_name_from_header() -> Optional[str]:
    """Extract project name from X-Project-Name header if present."""
    try:
        headers = get_http_headers()
        return headers.get("x-project-name")
    except Exception:
        # Not in HTTP context or headers unavailable
        return None


def _get_project_name() -> str:
    """Get project name from X-Project-Name header, raising ToolError if not set."""
    project_name = _get_project_name_from_header()
    if project_name:
        return project_name
    raise ToolError(
        "X-Project-Name header is required but was not set. "
        "Configure it in your .mcp.json file."
    )


def _get_store() -> YamlBackend:
    """Get YamlBackend for the current authenticated user's project."""
    from pf_server.user_context import get_current_user

    user = get_current_user()
    user.set_project_name(_get_project_name())
    return _get_store_for_path(user.host_user_shadow_dir)


def create_authenticated_server() -> FastMCP:
    """
    Create MCP server for HTTP transport with JWT authentication.

    This server uses the UserContext (set by PFTokenVerifier) to determine
    which user's project to access. Each authenticated request will have
    access to that user's project root.

    Returns:
        Configured FastMCP server instance with authentication
    """
    from .auth import PFTokenVerifier

    mcp = FastMCP(
        "Proofactory",
        instructions=(
            "Proofactory MCP server for querying code annotations and specifications. "
            "Use read_annotations to query existing specifications. "
            "Use list_annotated_files to discover files with pflang annotations."
            "Use ask_question_lite to get answers *grounded in truth* about the codebase."
        ),
        auth=PFTokenVerifier(),
    )

    @mcp.tool()
    def read_annotations(
        file_paths: Optional[list[str]] = None,
        status_filter: Optional[str] = None,
    ) -> list[AnnotationResult]:
        """Query pflang code annotations and their verification status.

        Call this tool to retrieve formal specifications attached to source code.
        Each annotation contains a code property (invariant, precondition, etc.)
        and its current verification status.

        When to use each status_filter value:
        - "passed": Get verified invariants you must preserve when modifying code
        - "stale": Check what specifications were invalidated by recent changes
        - "failed": Investigate specifications that failed verification
        - None: Get all annotations regardless of status

        Args:
            file_paths: Specific files to query. Omit to query all annotated files.
            status_filter: One of "passed", "failed", "stale", or omit for all.

        Returns:
            List of AnnotationResult with fields: file_path, line_number,
            annotation_text, status, and verification_message.
        """
        project_name = _get_project_name()
        logger.debug(
            "read_annotations_called",
            project_name=project_name,
            file_paths=file_paths,
            status_filter=status_filter,
        )
        store = _get_store()
        annotations = store.list_annotations(
            file_paths=file_paths,
            status=AnnotationStatus(status_filter) if status_filter else None,
        )
        logger.info("read_annotations_returning", count=len(annotations))
        return annotations

    @mcp.tool()
    def list_annotated_files() -> list[str]:
        """Discover which files have pflang annotations.

        Call this tool first to identify files with formal specifications before
        using read_annotations to retrieve their details. Useful for understanding
        which parts of the codebase have formal verification coverage.

        Returns:
            Sorted list of relative file paths containing annotations.
        """
        project_name = _get_project_name()
        store = _get_store()
        all_locations = [
            annot.annotation.location for annot in store.list_annotations()
        ]
        file_set = {loc.file for loc in all_locations if loc and loc.file}
        logger.info(
            "list_annotated_files", project_name=project_name, file_count=len(file_set)
        )
        return sorted(file_set)

    @mcp.tool()
    async def ask_question_lite(question: str) -> dict:
        """Ask a yes/no question about code behavior using formal specifications.

        Call this tool to reason about code correctness, invariants, or behavior
        based on the project's pflang annotations. The tool analyzes formal
        specifications to determine if a property holds.

        Best for questions like:
        - "Does function X always return a positive value?"
        - "Can variable Y ever be null after initialization?"
        - "Is the loop invariant maintained across iterations?"

        Args:
            question: A yes/no question about code behavior or properties

        Returns:
            Dict with 'steps' (list of reasoning steps referencing annotation tags)
            and 'synthesis' (final Yes/No/Inconclusive answer with justification).
        """
        from pf_server.user_context import get_current_user

        project_name = _get_project_name()
        logger.debug(
            "ask_question_lite_called", project_name=project_name, question=question
        )

        user = get_current_user()
        user.set_project_name(project_name)

        answer = await ask_question_analysis(question, mark_sent=False)

        logger.info(
            "ask_question_lite_answered",
            project_name=project_name,
            answer=answer,
        )

        return {
            "steps": answer.get("steps", []),
            "synthesis": answer.get("synthesis", "Inconclusive"),
        }

    return mcp
