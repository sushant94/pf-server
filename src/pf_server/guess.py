"""Debounced analysis (guessing) infrastructure for WebSocket connections."""

import asyncio
import copy
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

from docker.models.containers import Container
from fastapi import WebSocket
from proofactory.config.models import ModelConfig
from proofactory.mining.llm_utils import call_completion, render_template
from proofactory.mining.verifier import AnnotationResult, AnnotationStatus
from proofactory.paths import PFPaths
from proofactory.storage.yaml_backend import YamlBackend

from pf_server.config import settings
from pf_server.containers import get_container_opencode_port, get_or_create_container
from pf_server.feedback_constants import LATEST_FEEDBACK_FILENAME
from pf_server.guess_configs import (
    AnalysisConfig,
    AnalysisContext,
    OpenCodeAnalysisConfig,
    get_ask_analysis_config,
)
from pf_server.logging_config import get_logger
from pf_server.models import AnalysisResponseData, ServerResponse, to_camel_dict
from pf_server.opencode_client import OpenCodeClient
from pf_server.session_manager import SessionInfo, SessionManager
from pf_server.user_context import get_current_user

logger = get_logger(__name__)

ASK_PROMPT_PATH = Path("src/pf_mcp/prompts/ask.jinja2")


def _compile_patch_review(payload: list[dict]) -> list[str]:
    lines: list[str] = []
    for decision in payload:
        file_path = decision.get("filePath", "unknown")
        accepted_lines = decision.get("acceptedLines", [])
        rejected_lines = decision.get("rejectedLines", [])
        if accepted_lines:
            lines.append(f"File: {file_path}")
            lines.append("Accepted lines:")
            for line_content in accepted_lines:
                lines.append(f"  {line_content}")
        if rejected_lines:
            lines.append(f"File: {file_path}")
            lines.append("Rejected lines:")
            for line_content in rejected_lines:
                lines.append(f"  {line_content}")
        if accepted_lines or rejected_lines:
            lines.append("")
    return lines


def _compile_annotations_deleted(payload: list[dict]) -> list[str]:
    lines: list[str] = []
    for item in payload:
        file_path = item.get("filePath", "unknown")
        annotation_type = item.get("annotationType", "unknown")
        annotation_name = item.get("annotationName", "unknown")
        line_no = item.get("line", "?")
        description = item.get("description", "")
        lines.append(
            f"{file_path}:{line_no} {annotation_type}:{annotation_name} {description}".strip()
        )
    if lines:
        lines.append("")
    return lines


def _compile_feedback_entries(entries: list[dict]) -> str:
    """Compile feedback JSONL entries into a plain-text summary."""
    lines = ["# Feedback Summary", ""]
    for entry in entries:
        feedback_data = entry.get("feedback", {})
        feedback_type = feedback_data.get("type", "unknown")
        payload = feedback_data.get("payload", [])
        lines.append(f"## Feedback Type: {feedback_type}")

        if feedback_type == "patch_review":
            lines.extend(_compile_patch_review(payload))
        elif feedback_type == "annotations_deleted":
            lines.extend(_compile_annotations_deleted(payload))
        else:
            lines.append(json.dumps(payload, ensure_ascii=False))
            lines.append("")

    return "\n".join(lines).strip()


def _consume_latest_feedback(feedback_dir: Path) -> str | None:
    """Atomically read and clear the latest feedback file.

    Returns compiled feedback text or None if no entries.
    """
    latest_path = feedback_dir / LATEST_FEEDBACK_FILENAME
    if not latest_path.exists():
        return None

    temp_path = feedback_dir / f"{LATEST_FEEDBACK_FILENAME}.processing"
    try:
        latest_path.rename(temp_path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("feedback_latest_rename_failed", error=str(exc))
        return None

    entries: list[dict] = []
    try:
        with temp_path.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    entries.append(json.loads(line))
    except Exception as exc:
        logger.warning("feedback_latest_read_failed", error=str(exc))
    finally:
        try:
            temp_path.unlink()
        except OSError:
            pass

    if not entries:
        return None
    return _compile_feedback_entries(entries)


@dataclass
class AnalysisState:
    """Tracks analysis state per WebSocket connection."""

    generation: int = 0
    pending_task: Optional[asyncio.Task] = None
    session_id: Optional[str] = None
    has_unseen_hint: bool = False  # Hint that UNSEEN patches may exist


# =============================================================================
# Initial Analysis (Tar Sync Auto-Start)
# =============================================================================


async def run_initial_analysis(
    container: Container,
    project_path: str,
    config: OpenCodeAnalysisConfig,
    session_info: SessionInfo,
    user_id: str,
    project_name: str,
) -> None:
    """Run initial analysis triggered by tar sync upload.

    This analysis:
    - Runs immediately (no debounce)
    - Cannot be cancelled by subsequent sync events
    - Uses OpenCodeSession for proper SSE event handling
    - On completion, drains accumulated changes and queues them

    Args:
        container: Docker container
        project_path: Path to project inside container
        config: OpenCode analysis configuration
        session_info: SessionInfo tracking initial analysis state
        user_id: User identifier (for logging/state updates)
        project_name: Project name (for logging/state updates)
    """
    from pf_server.opencode_client import MCPConfig
    from pf_server.opencode_session import OpenCodeSession
    from pf_server.user_context import UserContext

    logger.info(
        "initial_analysis_starting",
        project_path=project_path,
        user_id=user_id,
        project_name=project_name,
    )

    # Reconstruct user context for repo access
    user = UserContext(user_id=user_id)
    user.set_project_name(project_name)

    opencode_client: OpenCodeClient | None = None
    try:
        # Build OpenCode client
        container_id: str = container.id  # type: ignore[assignment]
        host_port = get_container_opencode_port(container_id)
        if host_port is None:
            logger.error("initial_analysis_no_port", container_id=container_id[:12])
            return

        opencode_client = OpenCodeClient(f"http://localhost:{host_port}")

        # Health check before proceeding
        is_healthy = await opencode_client.health_check()
        if not is_healthy:
            logger.error(
                "initial_analysis_server_unhealthy",
                base_url=opencode_client._base_url,
            )
            return

        # Ensure MCP servers are configured
        try:
            await opencode_client.ensure_mcp(
                project_path,
                "language-server",
                MCPConfig(
                    command=[
                        "mcp-language-server",
                        "--workspace",
                        project_path,
                        "--lsp",
                        "pyright-langserver",
                        "--",
                        "--stdio",
                    ],
                ),
            )
            await opencode_client.ensure_mcp(
                project_path,
                "code-pathfinder",
                MCPConfig(
                    command=["pathfinder", "serve", "--project", project_path],
                ),
            )
        except Exception as e:
            logger.error("initial_analysis_mcp_setup_failed", error=str(e))
            return

        # Build initial analysis prompt
        prompt = (
            "Analyze this codebase and mine for specifications. "
            "Focus on discovering invariants, preconditions, postconditions, "
            "and temporal properties."
        )

        # Run analysis within repo context using OpenCodeSession for SSE handling
        async with user.repo.context():
            logger.info(
                "initial_analysis_repo_context_entered",
                user_id=user_id,
                project_name=project_name,
            )

            # Use OpenCodeSession for proper SSE event handling
            async with OpenCodeSession(
                opencode_client,
                project_path,
                client_ws=None,  # Fire-and-forget mode (no WebSocket)
            ) as session:
                await session.prompt(
                    prompt,
                    agent=config.agent,
                    model=config.model,
                    continue_session=config.continue_session,
                )

                # Wait for completion with generous timeout (10 min)
                success = await session.wait_for_completion(timeout=600.0)

                if not success:
                    if session.error:
                        logger.error(
                            "initial_analysis_session_error",
                            error=session.error,
                        )
                    else:
                        logger.warning("initial_analysis_timeout")

        logger.info(
            "initial_analysis_complete",
            session_id=session.session_id,
            success=success,
        )

    except Exception as e:
        logger.error("initial_analysis_failed", error=str(e), exc_info=True)

    finally:
        # Mark initial as complete via SessionManager
        session_mgr = SessionManager.get_instance()
        session_mgr.mark_initial_complete(user_id, project_name)

        # Drain accumulated changes and queue as single message
        changes = await session_info.drain_accumulated()
        if changes and opencode_client:
            combined_changes = "\n\n".join(changes)
            prompt = config.format_feedback(changes=combined_changes)

            try:
                # Queue accumulated changes using OpenCodeSession
                async with OpenCodeSession(
                    opencode_client, project_path, client_ws=None
                ) as session:
                    await session.prompt(
                        prompt,
                        agent=config.agent,
                        model=config.model,
                        continue_session=True,
                    )
                    # Wait for this followup to complete as well
                    await session.wait_for_completion(timeout=300.0)

                logger.info(
                    "initial_analysis_accumulated_changes_processed",
                    change_count=len(changes),
                )
            except Exception as e:
                logger.error(
                    "initial_analysis_accumulated_changes_failed",
                    error=str(e),
                )


async def queue_changes_to_session(
    container: Container,
    project_path: str,
    changes: str,
    config: OpenCodeAnalysisConfig,
) -> None:
    """Queue changes to OpenCode session without waiting for completion.

    Used after initial analysis completes for individual sync events.
    No debounce, no cancellation - just queue the message.

    Args:
        container: Docker container
        project_path: Path to project inside container
        changes: Change description to queue
        config: OpenCode analysis configuration
    """
    container_id: str = container.id  # type: ignore[assignment]
    host_port = get_container_opencode_port(container_id)
    if host_port is None:
        logger.error("queue_changes_no_port", container_id=container_id[:12])
        return

    client = OpenCodeClient(f"http://localhost:{host_port}")
    prompt = config.format_feedback(changes=changes)

    try:
        await client.prompt_async(
            project_path=project_path,
            text=prompt,
            agent=config.agent,
            model=config.model,
            continue_session=True,
        )
        logger.info("changes_queued_to_session", changes_length=len(changes))
    except Exception as e:
        logger.error("changes_queue_failed", error=str(e))


async def run_isolated_analysis(
    config: AnalysisConfig,
    mark_sent: bool = True,
    question: str | None = None,
) -> tuple[list[AnnotationResult] | str, list, int]:
    """
    Run analysis in an isolated worktree.

    This is a shared helper used by both explicit analysis requests and
    ask_question_analysis. It handles worktree creation, analysis execution,
    and patch collection.

    Requires UserContext to be set (via FastAPI dependency or MCP auth).

    Args:
        config: Analysis configuration to use
        mark_sent: Whether to mark patches as SENT after filtering
        question: Optional question for ask analysis (OpenCode configs)

    Returns:
    # pf:invariant:AnalysisState.single_pending at most one pending_task can be running at a time
    # pf:invariant:AnalysisState.generation_monotonic generation only increases (never decreases)
        Tuple of (annotations_or_error, patches, exit_code)
    """
    user = get_current_user()

    # Get container
    container = await asyncio.to_thread(get_or_create_container)

    # Run analysis in isolated worktree
    async with user.repo.worktree_context(container=container) as wt:
        result, exit_code = await run_analysis_in_worktree(
            container=container,
            config=config,
            worktree_docker_dir=wt.docker_dir,
            worktree_host_dir=wt.host_dir,
            question=question,
        )

    # Drain worktree patches (captured when context exited)
    patches = user.repo.drain_worktree_patches()
    patches = user.repo.filter_unseen_spec_patches(patches, mark_sent=mark_sent)

    return result, patches, exit_code


async def ask_question_analysis(
    question: str,
    mark_sent: bool = True,
    context: str | None = None,
) -> dict:
    """
    Core logic for asking questions about code using formal specifications.

    Runs analysis in an isolated worktree, gathers annotations, and uses
    an LLM to answer the question based on the formal specifications.

    Requires UserContext to be set (via FastAPI dependency or MCP auth).

    Args:
        question: A yes/no question about code behavior or properties
        mark_sent: Whether to mark patches as SENT after filtering
        context: Optional context string to include in the prompt

    Returns:
        Dict with 'steps' (list of AnswerStep-compatible dicts) and 'synthesis' (str)
    """
    user = get_current_user()

    # Configure analysis for ask question
    analysis_config = copy.copy(get_ask_analysis_config())
    # For PF configs, set template_vars; for OpenCode, question is passed to run()
    if hasattr(analysis_config, "template_vars"):
        # Escape quotes and special chars for JSON embedding in command
        analysis_config.template_vars["question"] = json.dumps(question)[1:-1]

    # Run isolated analysis (pass question for OpenCode configs)
    result, patches, exit_code = await run_isolated_analysis(
        config=analysis_config,
        mark_sent=mark_sent,
        question=question,
    )

    # Process results - fallback to stored annotations if analysis fails
    if exit_code != 0 or isinstance(result, str):
        logger.warning("ask_analysis_failed_fallback", exit_code=exit_code)
        paths = PFPaths(user.host_user_shadow_dir)
        store = YamlBackend(paths)
        annotations = [
            json.dumps(annot.model_dump(), indent=2)
            for annot in store.list_annotations()
        ]
    else:
        annotations = [json.dumps(annot.model_dump(), indent=2) for annot in result]

    # Make LLM completion call
    prompt_template = ASK_PROMPT_PATH.read_text()
    prompt_str = render_template(
        prompt_template,
        {"question": question, "properties": annotations, "context": context},
    )

    messages = [
        {"role": "system", "content": "You are an expert code analysis assistant."},
        {"role": "user", "content": prompt_str},
    ]

    config = ModelConfig(name=settings.model_name)

    logger.info("ask_question_synthesizing_response", model=config)

    answer = await asyncio.to_thread(call_completion, messages, config)
    # Parse JSON response from LLM
    content = answer["content"].strip()

    logger.debug("ask_question_synthesis_response", answer=content)

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        # Fallback: try to extract JSON from markdown code fence
        import re

        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
        if match:
            parsed = json.loads(match.group(1))
        else:
            logger.warning("ask_question_json_parse_failed", content=content[:200])
            # Return a fallback structure with the raw content as synthesis
            return {"steps": [], "synthesis": content, "patches": patches}

    return {
        "steps": parsed.get("steps", []),
        "synthesis": parsed.get("synthesis", ""),
        "patches": patches,
    }


def build_dummy_annotation_results() -> list[AnnotationResult]:
    """Return a deterministic annotation result for dev-only analysis."""

    now = datetime.now(timezone.utc).isoformat()
    dummy_annotation = {
        "file": "DEV_DUMMY",
        "expr": "true",
        "note": "Dummy annotation generated by dev analysis shim",
    }

    return [
        AnnotationResult.model_construct(
            annotation=dummy_annotation,
            status=AnnotationStatus.UNVERIFIED,
            error_message=None,
            counterexample=None,
            metadata={
                "source": "dev_dummy_analysis",
                "generated_at": now,
            },
        )
    ]


async def run_analysis(
    container: Container,
    config: AnalysisConfig,
    marker: str,
    generation: int,
    session_id: Optional[str] = None,
) -> tuple[list[AnnotationResult] | str, int]:
    """
    Run analysis command in container using unified config interface.

    Returns the exec result. Handles cancellation via config.cancel().
    """
    user = get_current_user()
    start_time = time.perf_counter()

    # Build analysis context
    opencode_client = None
    if isinstance(config, OpenCodeAnalysisConfig):
        container_id: str = container.id  # type: ignore[assignment]
        host_port = get_container_opencode_port(container_id)
        logger.info(
            "analysis_opencode_port",
            generation=generation,
            container_id=container_id[:12],
            host_port=host_port,
        )
        if host_port is None:
            logger.error(
                "analysis_no_opencode_port",
                generation=generation,
                container_id=container_id[:12],
            )
            raise RuntimeError("OpenCode port not configured for container")
        opencode_client = OpenCodeClient(f"http://localhost:{host_port}")

    ctx = AnalysisContext(
        container=container,
        project_path=str(user.docker_shadow_dir),
        opencode_client=opencode_client,
    )

    try:
        logger.info(
            "analysis_starting",
            generation=generation,
            analysis_type=config.name,
            marker=marker,
        )

        # Collect feedback content
        async with user.repo.context() as mgr:
            feedback_path = user.host_feedback_dir
            feedback_path.mkdir(parents=True, exist_ok=True)

            latest_feedback = _consume_latest_feedback(feedback_path)
            if latest_feedback:
                changes = latest_feedback
                logger.info(
                    "analysis_using_latest_feedback",
                    generation=generation,
                    session_id=session_id,
                )
            else:
                changes = mgr.changes
                logger.info(
                    "analysis_no_latest_feedback",
                    generation=generation,
                    session_id=session_id,
                )

            # Run analysis using unified interface
            exit_code = 0
            output = ""
            async for event in config.run(ctx, feedback_content=changes):
                if event.type == "progress":
                    logger.debug(
                        "analysis_progress",
                        generation=generation,
                        event_data=event.data,
                    )
                elif event.type == "error":
                    exit_code = event.data.get("exit_code", 1)
                    output = event.data.get(
                        "output", event.data.get("message", "Error")
                    )
                elif event.type == "complete":
                    exit_code = event.data.get("exit_code", 0)

            duration_ms = int((time.perf_counter() - start_time) * 1000)

        # Load results
        if exit_code == 0:
            paths = PFPaths(user.host_user_shadow_dir)
            backend = YamlBackend(paths)
            # TODO: Make this use the public facing API on next pf-tools release
            annotations = backend._load_annotations(paths.spec_file())
            logger.info(
                "analysis_completed",
                generation=generation,
                analysis_type=config.name,
                exit_code=exit_code,
                annotation_count=len(annotations),
                duration_ms=duration_ms,
            )
            return annotations, exit_code
        else:
            logger.error(
                "analysis_failed",
                generation=generation,
                analysis_type=config.name,
                exit_code=exit_code,
                duration_ms=duration_ms,
                error=output[:500] if output else "No output",
            )
            return output or "Analysis failed", exit_code

    except asyncio.CancelledError:
        duration_ms = int((time.perf_counter() - start_time) * 1000)
        await config.cancel(ctx)
        logger.warning(
            "analysis_process_terminated",
            generation=generation,
            analysis_type=config.name,
            marker=marker,
            duration_ms=duration_ms,
        )
        raise


async def run_analysis_in_worktree(
    container: Container,
    config: AnalysisConfig,
    worktree_docker_dir: Path,
    worktree_host_dir: Path,
    question: str | None = None,
) -> tuple[list[AnnotationResult] | str, int]:
    """Run analysis in a specific worktree directory (no feedback, no context manager)."""
    marker = f"pf_{config.name}_{uuid4().hex[:8]}"
    start_time = time.perf_counter()

    logger.info("worktree_analysis_starting", analysis_type=config.name, marker=marker)

    # Build analysis context
    opencode_client = None
    if isinstance(config, OpenCodeAnalysisConfig):
        container_id: str = container.id  # type: ignore[assignment]
        host_port = get_container_opencode_port(container_id)
        logger.info(
            "worktree_analysis_opencode_port",
            container_id=container_id[:12],
            host_port=host_port,
        )
        if host_port is None:
            logger.error(
                "worktree_analysis_no_port",
                container_id=container_id[:12],
            )
            raise RuntimeError("OpenCode port not configured for container")
        opencode_client = OpenCodeClient(f"http://localhost:{host_port}")
        logger.debug(
            "worktree_analysis_client_created",
            base_url=f"http://localhost:{host_port}",
        )

    ctx = AnalysisContext(
        container=container,
        project_path=str(worktree_docker_dir),
        opencode_client=opencode_client,
    )

    try:
        # Run analysis using unified interface
        exit_code = 0
        output = ""
        async for event in config.run(ctx, question=question):
            if event.type == "progress":
                logger.debug("worktree_analysis_progress", event_data=event.data)
            elif event.type == "error":
                exit_code = event.data.get("exit_code", 1)
                output = event.data.get("output", event.data.get("message", "Error"))
            elif event.type == "complete":
                exit_code = event.data.get("exit_code", 0)

        duration_ms = int((time.perf_counter() - start_time) * 1000)

        if exit_code == 0:
            # Load results from worktree's .pf directory using HOST path
            paths = PFPaths(worktree_host_dir)
            backend = YamlBackend(paths)
            annotations = backend._load_annotations(paths.spec_file())
            logger.info(
                "worktree_analysis_completed",
                annotation_count=len(annotations),
                duration_ms=duration_ms,
            )
            return annotations, exit_code
        else:
            logger.error(
                "worktree_analysis_failed",
                exit_code=exit_code,
                error=output[:500] if output else "No output",
            )
            return output or "Analysis failed", exit_code

    except asyncio.CancelledError:
        await config.cancel(ctx)
        logger.warning("worktree_analysis_cancelled", marker=marker)
        raise


async def run_debounced_analysis(
    container: Container,
    client_ws: WebSocket,
    state: AnalysisState,
    generation: int,
    config: AnalysisConfig,
):
    """Wait for debounce period, then run analysis if still current."""
    user = get_current_user()
    marker = f"pf_analysis_{config.name}_{generation}"

    logger.debug(
        "analysis_debounce_started",
        generation=generation,
        analysis_type=config.name,
        debounce_ms=config.debounce_ms,
    )

    # Wait for quiet period
    await asyncio.sleep(config.debounce_ms / 1000)

    # Check if still current (no new changes arrived)
    if generation != state.generation:
        logger.debug(
            "analysis_stale_before_run",
            generation=generation,
            current_generation=state.generation,
            analysis_type=config.name,
        )
        return

    logger.debug(
        "analysis_debounce_completed",
        generation=generation,
        analysis_type=config.name,
    )

    result, exit_code = await run_analysis(
        container, config, marker, generation, session_id=state.session_id
    )

    # Patches were saved when repo context exited - set hint for polling
    state.has_unseen_hint = True

    # Check again after completion
    if generation != state.generation:
        logger.debug(
            # pf:requires:run_debounced_analysis.generation_matches generation should match state.generation when scheduled
            # pf:ensures:run_debounced_analysis.stale_skipped if generation != state.generation after debounce, analysis is skipped
            # pf:ensures:run_debounced_analysis.results_sent if generation still current, results are sent to client_ws
            "analysis_stale_after_run",
            generation=generation,
            current_generation=state.generation,
            analysis_type=config.name,
        )
        return

    # Send results to client using structured analysis response
    annotation_count = len(result) if isinstance(result, list) else 0
    analysis_data = AnalysisResponseData(
        type=f"analysis_{config.name}",
        generation=generation,
        output=[to_camel_dict(r.model_dump()) for r in result]
        if isinstance(result, list)
        else result,
        patches=user.repo.get_patch_contents(),
    )
    response = ServerResponse(
        request_id=None,  # Analysis results are broadcasts
        status="success" if exit_code == 0 else "error",
        message=f"{config.name.title()} analysis complete",
        data=analysis_data.model_dump(by_alias=True),
        generation=generation,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )
    await client_ws.send_json(response.model_dump(by_alias=True))

    patches = analysis_data.patches or []
    logger.info(
        "analysis_results_sent",
        generation=generation,
        analysis_type=config.name,
        annotation_count=annotation_count,
        patch_count=len(patches),
        exit_code=exit_code,
    )


def _make_task_exception_handler(
    client_ws: WebSocket,
    generation: int,
    config: AnalysisConfig,
) -> Callable[[asyncio.Task], None]:
    """Create a callback to handle unhandled exceptions in analysis tasks.

    This ensures that if the analysis task fails unexpectedly (not via cancellation),
    we log the error and attempt to notify the client.
    """

    def handler(task: asyncio.Task) -> None:
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return  # Task was cancelled, not an error

        if exc is None:
            return  # No exception

        logger.error(
            "analysis_task_failed",
            generation=generation,
            analysis_type=config.name,
            error=str(exc),
            exc_type=type(exc).__name__,
        )

        # Try to send error response to client
        # Schedule as a coroutine since callbacks are synchronous
        async def send_error():
            try:
                response = ServerResponse(
                    request_id=None,  # Error notification is a broadcast
                    status="error",
                    message=f"Analysis failed unexpectedly: {exc}",
                    data=AnalysisResponseData(
                        type=f"analysis_{config.name}",
                        generation=generation,
                        output=str(exc),
                        patches=[],
                    ).model_dump(by_alias=True),
                    generation=generation,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                )
                await client_ws.send_json(response.model_dump(by_alias=True))
            except Exception as send_err:
                logger.warning(
                    "analysis_error_notification_failed",
                    generation=generation,
                    error=str(send_err),
                )

        asyncio.create_task(send_error())

    return handler


async def trigger_analysis(
    container: Container,
    client_ws: WebSocket,
    state: AnalysisState,
    config: AnalysisConfig,
):
    """Cancel pending analysis and schedule new one."""
    old_generation = state.generation
    state.generation += 1
    new_generation = state.generation

    # Get scope if available (PF configs only)
    scope = getattr(config, "scope", None)

    logger.info(
        "analysis_triggered",
        generation=new_generation,
        analysis_type=config.name,
        scope=scope,
    )

    # Cancel previous pending analysis
    if state.pending_task and not state.pending_task.done():
        logger.debug(
            "analysis_cancelled_superseded",
            old_generation=old_generation,
            new_generation=new_generation,
            analysis_type=config.name,
            scope=scope,
        )
        state.pending_task.cancel()
        try:
            await state.pending_task
        except asyncio.CancelledError:
            pass  # Expected

    # Schedule new debounced analysis
    # NOTE: We should create the feedback right here and send it to capture the state at this moment.
    # Otherwise, if we generate feedback inside the task after the debounce, it may include changes that arrived during the debounce period.
    state.pending_task = asyncio.create_task(
        run_debounced_analysis(
            container,
            client_ws,
            state,
            state.generation,
            config,
            # pf:ensures:trigger_analysis.increments_generation state.generation is incremented by 1
            # pf:ensures:trigger_analysis.cancels_pending previous pending_task is cancelled if running
            # pf:ensures:trigger_analysis.new_task_scheduled state.pending_task holds the new analysis task
        )
    )
    # Add exception handler to log and notify client of unexpected failures
    state.pending_task.add_done_callback(
        _make_task_exception_handler(client_ws, state.generation, config)
    )
