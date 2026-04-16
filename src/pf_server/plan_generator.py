"""Plan generation logic for implementation planning.

This module integrates with OpenCode Serve API to provide real planning
functionality with native question/clarification handling via SSE events.
"""

import asyncio
from typing import Any

from pf_server.logging_config import get_logger
from pf_server.models import PlanStatus
from pf_server.opencode_manager import get_manager
from pf_server.plan_executor import PlanExecutor, build_plan_prompt
from pf_server.plan_manager import PlanSession
from pf_server.session_manager import SessionManager
from pf_server.user_context import get_current_user

logger = get_logger(__name__)


async def generate_implementation_plan(
    description: str,
    context: dict[str, Any] | None,
    container_id: str,
    codebase_summary: str | None = None,
) -> dict[str, Any]:
    """Generate an implementation plan using OpenCode Serve API.

    This creates or recovers an OpenCode session and sends the planning prompt.
    Returns immediately after sending the prompt asynchronously.
    Questions/clarifications are handled via SSE events (question.asked), not
    returned directly from this function.

    The SSE listener continues to handle subsequent events asynchronously.

    Args:
        description: User's plan request description
        context: Optional context (files, annotations)
        container_id: Docker container ID for getting the OpenCode client
        codebase_summary: Optional summary of the codebase for context

    Returns:
        dict with keys:
        - plan_content: str (markdown, empty initially)
        - needs_confirmation: bool (always False - questions via SSE)
        - confirmation_question: str | None (None - questions via SSE)
        - choices: list[str] | None (None - questions via SSE)
    """
    user = get_current_user()
    project_path = str(user.host_user_repo_dir)

    logger.info(
        "plan_generation_started",
        description_preview=description[:50] if description else "",
        has_context=context is not None,
        project_path=project_path,
    )

    # Get OpenCode client from manager
    manager = get_manager()
    opencode = await manager.get_client(container_id)

    try:
        # Always create a NEW session for plan_request
        # plan_request = user wants a NEW plan, not to resume an old one
        # (Reconnect/resume logic is handled separately in ws_proxy on connect)
        session_mgr = SessionManager.get_instance()

        # Create executor without completion event (we don't wait for completion)
        executor = PlanExecutor(opencode, project_path, asyncio.Event())

        # Always start fresh session for new plan
        session_id = await executor.start_session()

        # Track session (listener will be attached in ws_proxy)
        session_mgr.set(
            user.user_id,
            user.project_name or "",
            session_id,
            project_path,
        )

        # Build prompt
        prompt = build_plan_prompt(description, context, codebase_summary)

        logger.info(
            "sending_prompt_async",
            session_id=session_id,
            prompt_length=len(prompt),
        )

        # Send prompt asynchronously (non-blocking, returns immediately)
        # The SSE listener will handle all responses and questions
        await opencode.prompt_async(
            project_path=project_path,
            text=prompt,
            agent="plan",
            continue_session=True,
        )

        logger.info(
            "plan_generation_initiated",
            session_id=session_id,
        )

        # Return immediately with draft status
        # SSE listener will send updates as they arrive
        return {
            "plan_content": "",  # Empty initially, will be updated via SSE
            "needs_confirmation": False,  # Questions via SSE, not here
            "confirmation_question": None,
            "choices": None,
        }

    except Exception as e:
        logger.error(
            "plan_generation_error",
            error=str(e),
            exc_info=True,
        )
        raise


async def continue_plan_execution(
    session: PlanSession,
    feedback: str | None,
    choice: str | None,
    container_id: str,
) -> dict[str, Any]:
    """Continue plan execution after user provides feedback or answer.

    This is called when the user responds to a clarification or provides
    additional input. It sends a follow-up prompt to OpenCode.
    Returns immediately after sending the prompt.

    The SSE listener continues to handle subsequent events asynchronously.

    Args:
        session: The current planning session
        feedback: Optional user feedback or modifications
        choice: Selected choice if multiple options were provided
        container_id: Docker container ID for getting the OpenCode client

    Returns:
        dict with keys:
        - content: str (existing content, unchanged)
        - status: PlanStatus
        - confirmation_question: str | None
        - choices: list[str] | None
        - patches: list[dict] | None (None - plan mode doesn't generate patches)
    """
    user = get_current_user()
    project_path = str(user.host_user_repo_dir)

    logger.info(
        "plan_continuation_started",
        plan_id=session.plan_id,
        choice=choice,
        has_feedback=feedback is not None,
    )

    # Get session info
    session_mgr = SessionManager.get_instance()
    session_info = session_mgr.get(user.user_id, user.project_name or "")

    if not session_info:
        logger.error("no_session_for_continuation", plan_id=session.plan_id)
        raise RuntimeError("No active OpenCode session found")

    # Get OpenCode client from manager
    manager = get_manager()
    opencode = await manager.get_client(container_id)

    try:
        # Build follow-up prompt
        if choice:
            follow_up = f"User selected: {choice}"
            if feedback:
                follow_up += f"\n\nAdditional feedback: {feedback}"
        else:
            follow_up = feedback or "Please proceed with the plan."

        logger.info(
            "sending_continuation_prompt",
            plan_id=session.plan_id,
            follow_up_preview=follow_up[:100],
        )

        # Send follow-up asynchronously (non-blocking, returns immediately)
        # SSE listener will handle all responses and questions
        await opencode.prompt_async(
            project_path=project_path,
            text=follow_up,
            agent="plan",
            continue_session=True,
        )

        logger.info(
            "plan_continuation_initiated",
            plan_id=session.plan_id,
        )

        # Return immediately - SSE listener will send updates
        return {
            "content": session.content,  # Keep existing content
            "status": PlanStatus.CONFIRMED,  # Mark as confirmed, awaiting completion
            "confirmation_question": None,
            "choices": None,
            "patches": None,  # Plan mode doesn't generate patches
        }

    except Exception as e:
        logger.error(
            "plan_continuation_error",
            plan_id=session.plan_id,
            error=str(e),
            exc_info=True,
        )
        raise
