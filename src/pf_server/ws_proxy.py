import asyncio
import copy
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from docker.models.containers import Container
from fastapi import WebSocket
from pydantic import ValidationError

from pf_server.feedback_constants import (
    LATEST_FEEDBACK_FILENAME,
    SESSION_FEEDBACK_FILENAME_TEMPLATE,
)
from pf_server.guess import (
    AnalysisState,
    ask_question_analysis,
    run_isolated_analysis,
    trigger_analysis,
)
from pf_server.guess_configs import (
    get_lite_analysis_config,
    get_trigger_analysis_config,
)
from pf_server.logging_config import get_logger
from pf_server.models import (
    AnalysisPayload,
    AnalysisResponseData,
    AnswerStep,
    ClientFeedbackPayload,
    ClientRequest,
    FileChange,
    FileChangeType,
    PlanConfirmationPayload,
    PlanRequestPayload,
    PlanResponseData,
    PlanStatus,
    QuestionAnswer,
    QuestionInfo,
    QuestionOption,
    QuestionPayload,
    ServerResponse,
    SyncErrorDetail,
    SyncResponseData,
    to_camel_dict,
)
from pf_server.opencode_manager import get_manager as get_opencode_manager
from pf_server.plan_generator import (
    continue_plan_execution,
    generate_implementation_plan,
)
from pf_server.plan_manager import PlanManager
from pf_server.session_manager import SessionManager
from pf_server.sse_listener import SSEEventListener
from pf_server.repo_manager.tag_state import (
    TagStateStore,
    TagStatus,
    extract_tag_from_accepted_line,
)
from pf_server.user_context import UserContext, get_current_user

logger = get_logger(__name__)


def _update_tag_state_from_feedback(payload: ClientFeedbackPayload) -> None:
    """Update tag state based on client feedback.

    Processes patch_review and annotations_deleted feedback to mark
    tags as ACCEPTED or REJECTED.
    """
    user = get_current_user()
    tag_state = TagStateStore(pf_dir=user.host_pf_dir)
    feedback = payload.feedback

    if feedback.type == "patch_review":
        # Process patch review decisions
        accepted_count = 0
        rejected_count = 0

        for decision in feedback.payload:
            # decision is PatchReviewDecision with acceptedLines and rejectedLines
            for line_content in decision.accepted_lines:
                tag = extract_tag_from_accepted_line(line_content)
                if tag:
                    if tag_state.set_status(tag, TagStatus.ACCEPTED):
                        accepted_count += 1

            for line_content in decision.rejected_lines:
                tag = extract_tag_from_accepted_line(line_content)
                if tag:
                    if tag_state.set_status(tag, TagStatus.REJECTED):
                        rejected_count += 1

        logger.info(
            "tag_state_feedback_processed",
            feedback_type="patch_review",
            accepted=accepted_count,
            rejected=rejected_count,
        )

    elif feedback.type == "annotations_deleted":
        # Process annotation deletions - mark as REJECTED
        deleted_count = 0

        for deletion in feedback.payload:
            # deletion is AnnotationDeletionFeedback with annotation_name as tag
            tag = deletion.annotation_name
            if tag_state.set_status(tag, TagStatus.REJECTED):
                deleted_count += 1

        logger.info(
            "tag_state_feedback_processed",
            feedback_type="annotations_deleted",
            deleted=deleted_count,
        )


def _append_feedback(payload: ClientFeedbackPayload) -> None:
    """Append client feedback to a session-scoped JSONL file.

    Stores the raw feedback payload for offline inspection.
    Also updates tag state based on feedback.
    """
    user = get_current_user()
    feedback_path = user.host_feedback_dir
    feedback_path.mkdir(parents=True, exist_ok=True)

    # Update tag state from feedback
    try:
        _update_tag_state_from_feedback(payload)
    except Exception as exc:
        logger.warning("tag_state_update_failed", error=str(exc))

    feedback_file = feedback_path / SESSION_FEEDBACK_FILENAME_TEMPLATE.format(
        session_id=payload.session_id
    )
    latest_file = feedback_path / LATEST_FEEDBACK_FILENAME
    entry = {
        "received_at": datetime.now(timezone.utc).isoformat(),
        "session_id": payload.session_id,
        "generation": payload.generation,
        "origin": payload.origin,
        "feedback": payload.feedback.model_dump(by_alias=True),
    }

    try:
        serialized = json.dumps(entry)
        for target in (feedback_file, latest_file):
            with target.open("a", encoding="utf-8") as f:
                f.write(serialized)
                f.write("\n")
    except Exception as exc:
        logger.warning("feedback_persist_failed", error=str(exc))


def _handle_file_changes(change: FileChange, container: Container) -> bool:
    """
    Applies file changes to the files in the docker container.

    Returns `True` on success and `False` otherwise
    """
    user = get_current_user()
    command = []

    if change.type in (FileChangeType.ADD, FileChangeType.MODIFY):
        # Get the directory corresponding to change.path
        dir_path = Path(change.path).parent
        # Both utf8 and base64 content is base64-encoded, always decode
        command = [
            "sh",
            "-c",
            f"mkdir -p {dir_path} && echo {change.content_base64} | base64 -d > {change.path}",
        ]
    # elif change.type == FileChangeType.MODIFY:
    #     command.extend(["sh", "-c"])
    #     if change.diff:
    #         # Apply a unified diff patch
    #         diff_content = change.diff.replace("'", "'\\''")  # Escape single quotes
    #         command.append(f"echo '{diff_content}' | patch -p1 -i -")
    #     elif change.is_binary:
    #         command.append(f"echo {change.content_base64} | base64 -d > {change.path}")
    elif change.type == FileChangeType.DELETE:
        command = ["sh", "-c", f"rm -f {change.path}"]
    else:
        raise ValueError(f"Unknown change type: {change.type}")

    result = subprocess.run(
        command,
        cwd=user.host_user_repo_dir,
        capture_output=True,
        text=True,
    )

    if result.returncode == 0:
        logger.debug(
            "file_change_applied", path=change.path, change_type=change.type.value
        )
    else:
        logger.debug(
            "file_change_subprocess_error",
            path=change.path,
            stdout=result.stdout if result.stdout else None,
            stderr=result.stderr if result.stderr else None,
        )

    return result.returncode == 0


async def handle_analysis(
    payload: AnalysisPayload,
    client_ws: WebSocket,
    state: AnalysisState,
) -> None:
    """Handle an explicit analysis request from the client.

    Runs analysis in an isolated worktree and sends results via WebSocket.
    """
    logger.info(
        "analysis_request_handling",
        request_id=payload.request_id,
        file_name=payload.file_name,
    )

    # Create analysis config with the file_name as scope
    analysis_config = copy.copy(get_trigger_analysis_config())
    analysis_config.scope = payload.file_name

    # Run analysis in isolated worktree
    result, patches, exit_code = await run_isolated_analysis(
        config=analysis_config,
        mark_sent=True,
    )

    # Build response
    annotation_output = (
        [to_camel_dict(r.model_dump()) for r in result]
        if isinstance(result, list)
        else result
    )
    analysis_data = AnalysisResponseData(
        type="analysis_lite",
        generation=state.generation,
        output=annotation_output,
        patches=patches,
    )
    response = ServerResponse(
        request_id=payload.request_id,
        status="success" if exit_code == 0 else "error",
        message="Analysis complete",
        data=analysis_data.model_dump(by_alias=True),
        generation=state.generation,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )

    await client_ws.send_json(response.model_dump(by_alias=True))

    logger.info(
        "analysis_results_sent",
        request_id=payload.request_id,
        file_name=payload.file_name,
        annotation_count=len(result) if isinstance(result, list) else 0,
        patch_count=len(patches),
        exit_code=exit_code,
    )


async def handle_question(
    payload: QuestionPayload, client_ws: WebSocket, container: Container
) -> None:
    """Handle a question request from the client."""
    try:
        result = await ask_question_analysis(
            payload.question, context=payload.format_context()
        )

        # Parse steps from the LLM response
        steps = [AnswerStep.model_validate(step) for step in result.get("steps", [])]

        response_data = QuestionAnswer(
            question_id=payload.question_id,
            question=payload.question,
            steps=steps,
            synthesis=result.get("synthesis", ""),
            new_annotation_patches=result.get("patches"),
        )
        response = ServerResponse(
            request_id=payload.request_id,
            status="success",
            message="Question answered",
            data=response_data.model_dump(by_alias=True),
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
    except Exception as e:
        logger.error("question_handling_failed", error=str(e))
        response = ServerResponse(
            request_id=payload.request_id,
            status="error",
            message=f"Failed to answer question: {e}",
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    logger.debug("handle_question_response", response=response)

    await client_ws.send_json(response.model_dump(by_alias=True))


# =============================================================================
# SSE Event Handlers for OpenCode events
# =============================================================================


async def handle_opencode_question(
    event: dict,
    client_ws: WebSocket,
    plan_manager: PlanManager,
) -> None:
    """Handle question.asked SSE event from OpenCode.

    When OpenCode asks a question, forward it to the client as a plan update
    with the structured question/options.

    Args:
        event: SSE event dict from OpenCode
        client_ws: WebSocket connection to client
        plan_manager: PlanManager instance
    """
    properties = event.get("properties", {})
    request_id = properties.get("id")
    session_id = properties.get("sessionID")
    questions = properties.get("questions", [])

    if not questions:
        logger.warning("question_asked_no_questions", request_id=request_id)
        return

    logger.info(
        "opencode_question_received",
        request_id=request_id,
        session_id=session_id,
        question_count=len(questions),
    )

    # Convert OpenCode questions to QuestionInfo format
    # Using proper model instances ensures camelCase serialization works correctly
    question_infos: list[QuestionInfo] = []
    for q in questions:
        # Convert options to QuestionOption instances
        options: list[QuestionOption] = []
        for opt in q.get("options", []):
            if isinstance(opt, dict):
                options.append(
                    QuestionOption(
                        label=opt.get("label", ""),
                        description=opt.get("description"),
                    )
                )
            else:
                # Simple string option
                options.append(QuestionOption(label=str(opt), description=None))

        question_infos.append(
            QuestionInfo(
                question=q.get("question", ""),
                header=q.get("header", "Question"),
                options=options,
                multiple=q.get("multiple", False),
                custom=q.get("custom", True),
            )
        )

    # Get user context to find plan session
    user = get_current_user()
    session_mgr = SessionManager.get_instance()

    # Store pending question ID in session
    session_mgr.set_pending_question(user.user_id, user.project_name or "", request_id)

    # Find corresponding plan session (any active status)
    sessions = plan_manager.list_sessions()
    plan_session = None
    for session in sessions:
        if session.status in (
            PlanStatus.DRAFT,
            PlanStatus.CONFIRMED,
            PlanStatus.NEEDS_CONFIRMATION,
        ):
            plan_session = session
            break

    # Build response with questions
    # Only send if we have a corresponding plan session to update
    if plan_session:
        # Update plan session to indicate questions are pending
        # Increment revision so it matches what we send to client
        new_revision = plan_session.revision + 1
        plan_manager.update_session(
            plan_session.plan_id,
            status=PlanStatus.NEEDS_CONFIRMATION,
            revision=new_revision,
        )

        response_data = PlanResponseData(
            plan_id=plan_session.plan_id,
            status=PlanStatus.NEEDS_CONFIRMATION,
            content=plan_session.content,
            questions=question_infos,
            revision=new_revision,
        )
        plan_id_for_response = plan_session.plan_id
    else:
        # No local plan session (e.g., after server restart)
        # Don't send orphaned questions - client can't do anything useful with them
        # since it has no plan context to update. These stale questions will be
        # cleaned up when a new plan is started or rejected via OpenCode API.
        logger.warning("no_plan_session_for_question", request_id=request_id)
        return

    # Note: request_id should be the plan_id (client's original request ID)
    # NOT the OpenCode question ID (which is stored in question_id fields)
    response = ServerResponse(
        request_id=plan_id_for_response,
        status="partial",
        message="OpenCode needs clarification",
        data=response_data.model_dump(by_alias=True),
        timestamp=datetime.now(timezone.utc).isoformat(),
    )

    logger.info(
        "sending_question_to_client",
        plan_id=plan_id_for_response,
        question_count=len(question_infos),
    )

    await client_ws.send_json(response.model_dump(by_alias=True))


async def handle_session_idle(
    event: dict,
    client_ws: WebSocket,
    plan_manager: PlanManager,
    container_id: str,
) -> None:
    """Handle session.idle SSE event from OpenCode.

    When OpenCode session becomes idle (completed processing), fetch the final
    content and notify the client.

    Args:
        event: SSE event dict from OpenCode
        client_ws: WebSocket connection to client
        plan_manager: PlanManager instance
        container_id: Docker container ID for getting the OpenCode client
    """
    properties = event.get("properties", {})
    session_id = properties.get("sessionID")

    if not session_id:
        logger.warning("session_idle_no_session_id")
        return

    # Get user context
    user = get_current_user()
    session_mgr = SessionManager.get_instance()
    session_info = session_mgr.get(user.user_id, user.project_name or "")

    if not session_info or session_info.session_id != session_id:
        # Not for our session, ignore
        return

    # Find corresponding plan session
    sessions = plan_manager.list_sessions()
    plan_session = None
    for session in sessions:
        if session.status in (
            PlanStatus.DRAFT,
            PlanStatus.CONFIRMED,
            PlanStatus.NEEDS_CONFIRMATION,
        ):
            plan_session = session
            break

    if not plan_session:
        return

    logger.info(
        "session_idle_received",
        plan_id=plan_session.plan_id,
        session_id=session_id,
    )

    # Fetch final content from OpenCode
    try:
        manager = get_opencode_manager()
        opencode = await manager.get_client(container_id)

        messages = await opencode.get_messages(
            session_id=session_id,
            project_path=str(user.host_user_repo_dir),
        )

        # Extract agent output from latest assistant message
        content = ""
        for msg in reversed(messages):
            info = msg.get("info", {})
            if info.get("role") == "assistant":
                parts = msg.get("parts", [])
                text_parts = [
                    p.get("text", "")
                    for p in parts
                    if p.get("type") == "text" and p.get("text")
                ]
                content = "\n".join(text_parts).strip()
                if content:
                    break

        # Update plan session with final content
        plan_manager.update_session(
            plan_session.plan_id,
            status=PlanStatus.COMPLETED,
            content=content or plan_session.content,
        )

        # Build response
        response_data = PlanResponseData(
            plan_id=plan_session.plan_id,
            status=PlanStatus.COMPLETED,
            content=content or plan_session.content,
            questions=None,
            revision=plan_session.revision + 1,
        )

        response = ServerResponse(
            request_id=plan_session.plan_id,
            status="success",
            message="Plan generation completed",
            data=response_data.model_dump(by_alias=True),
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        logger.info(
            "plan_completed",
            plan_id=plan_session.plan_id,
            content_length=len(content),
        )

        await client_ws.send_json(response.model_dump(by_alias=True))

    except Exception as e:
        logger.error(
            "session_idle_fetch_error",
            plan_id=plan_session.plan_id,
            error=str(e),
            exc_info=True,
        )


async def handle_plan_request(
    payload: PlanRequestPayload,
    client_ws: WebSocket,
    container: Container,
    plan_manager: PlanManager,
) -> None:
    """Handle a plan request from the client.

    Creates a new planning session, starts SSE listener for events,
    and initiates plan generation via OpenCode.
    """
    user = get_current_user()
    project_path = str(user.host_user_repo_dir)
    container_id: str = container.id  # type: ignore[assignment]

    logger.info(
        "plan_request_received",
        request_id=payload.request_id,
        plan_id=payload.plan_id,
        description_preview=payload.description[:50] if payload.description else "",
    )

    try:
        # Create plan session using client-provided plan_id
        # request_id is for response correlation, plan_id is for session tracking
        plan_id = payload.plan_id
        context = payload.context.model_dump() if payload.context else None
        plan_manager.create_session(plan_id, payload.description, context)

        # Start SSE listener for this session
        # Always create a new listener since each WebSocket needs its own listener
        session_mgr = SessionManager.get_instance()
        session_info = session_mgr.get(user.user_id, user.project_name or "")

        # Stop old listener if it exists (it's pointing to old/closed WebSocket)
        if session_info and session_info.listener:
            logger.debug("stopping_old_sse_listener", plan_id=plan_id)
            try:
                await session_info.listener.stop()
            except Exception as e:
                logger.warning("old_sse_listener_stop_failed", error=str(e))

        # Always create a new listener for the current WebSocket
        if True:  # Always create new listener
            # Get OpenCode client from manager
            manager = get_opencode_manager()
            opencode = await manager.get_client(container_id)

            # Create SSE listener
            listener = SSEEventListener(opencode, project_path, client_ws)

            # Register event handlers
            async def on_question(event: dict) -> None:
                await handle_opencode_question(event, client_ws, plan_manager)

            async def on_session_idle(event: dict) -> None:
                await handle_session_idle(event, client_ws, plan_manager, container_id)

            listener.on("question.asked", on_question)
            listener.on("session.idle", on_session_idle)

            # Start listening for events
            await listener.start()

            # Update session with listener (session will be created by plan_generator)
            # We'll update after generate_implementation_plan sets the session
            logger.info(
                "sse_listener_started_for_plan",
                plan_id=plan_id,
                project_path=project_path,
            )

        # Generate plan (sends prompt asynchronously)
        result = await generate_implementation_plan(
            description=payload.description,
            context=context,
            container_id=container_id,
        )

        # Update session with the new listener
        session_mgr.update_listener(user.user_id, user.project_name or "", listener)

        # Update plan session status
        plan_manager.update_session(
            plan_id,
            status=PlanStatus.DRAFT,
            content=result.get("plan_content", ""),
        )

        # Build response
        response_data = PlanResponseData(
            plan_id=plan_id,
            status=PlanStatus.DRAFT,
            content=result.get("plan_content", ""),
            questions=None,  # Questions come via SSE
            revision=0,
        )
        response = ServerResponse(
            request_id=payload.request_id,
            status="success",
            message="Plan generation initiated",
            data=response_data.model_dump(by_alias=True),
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    except Exception as e:
        logger.error("plan_request_failed", error=str(e), exc_info=True)
        response = ServerResponse(
            request_id=payload.request_id,
            status="error",
            message=f"Failed to create plan: {e}",
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    await client_ws.send_json(response.model_dump(by_alias=True))


async def handle_plan_confirmation(
    payload: PlanConfirmationPayload,
    client_ws: WebSocket,
    container: Container,
    plan_manager: PlanManager,
) -> None:
    """Handle a plan confirmation or continuation from the client.

    If there's a pending question from OpenCode, uses reply_to_question API.
    Otherwise continues plan execution with a new prompt.
    """
    logger.info(
        "plan_confirmation_received",
        request_id=payload.request_id,
        plan_id=payload.plan_id,
        choice=payload.choice,
        has_feedback=payload.feedback is not None,
    )

    try:
        # Get existing session
        session = plan_manager.get_session(payload.plan_id)
        if not session:
            raise RuntimeError(f"Plan session not found: {payload.plan_id}")

        # Verify revision matches to prevent stale updates
        if session.revision != payload.revision:
            logger.warning(
                "plan_revision_mismatch",
                plan_id=payload.plan_id,
                expected=session.revision,
                received=payload.revision,
            )
            response = ServerResponse(
                request_id=payload.request_id,
                status="error",
                message=f"Revision mismatch: expected {session.revision}, got {payload.revision}",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
            await client_ws.send_json(response.model_dump(by_alias=True))
            return

        # Check if user rejected the plan
        if not payload.confirmed:
            logger.info("plan_rejected", plan_id=payload.plan_id)
            plan_manager.update_session(payload.plan_id, status=PlanStatus.REJECTED)

            response = ServerResponse(
                request_id=payload.request_id,
                status="success",
                message="Plan rejected",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
            await client_ws.send_json(response.model_dump(by_alias=True))
            return

        # Get container_id and user context
        container_id: str = container.id  # type: ignore[assignment]
        user = get_current_user()
        project_path = str(user.host_user_repo_dir)
        session_mgr = SessionManager.get_instance()
        session_info = session_mgr.get(user.user_id, user.project_name or "")

        # Check if there's a pending OpenCode question to answer
        pending_question_id = session_info.pending_question_id if session_info else None

        if pending_question_id:
            # Answer the pending question using reply_to_question API
            logger.info(
                "answering_opencode_question",
                plan_id=payload.plan_id,
                question_id=pending_question_id,
                choice=payload.choice,
                has_feedback=payload.feedback is not None,
                has_answers=payload.answers is not None,
            )

            # Get OpenCode client
            manager = get_opencode_manager()
            opencode = await manager.get_client(container_id)

            # Build answers from payload
            answers = payload.answers
            if not answers:
                # Backward compatibility: convert old single-answer format
                if payload.choice:
                    answer_text = payload.choice
                    if payload.feedback:
                        answer_text = f"{payload.choice}\n\nAdditional context: {payload.feedback}"
                    answers = [[answer_text]]
                elif payload.feedback:
                    answers = [[payload.feedback]]
                else:
                    answers = [["Yes, proceed."]]

            logger.debug(
                "sending_question_answers",
                question_id=pending_question_id,
                answer_count=len(answers),
            )

            # Reply to question with answers
            await opencode.reply_to_question(
                request_id=pending_question_id,
                answers=answers,
                project_path=project_path,
            )

            # Clear pending question
            session_mgr.set_pending_question(
                user.user_id, user.project_name or "", None
            )

            # Update plan session status to CONFIRMED so next question can find it
            plan_manager.update_session(
                payload.plan_id,
                status=PlanStatus.CONFIRMED,
            )

            logger.info(
                "question_answered",
                question_id=pending_question_id,
                plan_id=payload.plan_id,
            )

            # Send acknowledgment - agent will continue via SSE events
            response_data = PlanResponseData(
                plan_id=payload.plan_id,
                status=PlanStatus.CONFIRMED,
                content=session.content,
                questions=None,
                revision=session.revision,
            )
            response = ServerResponse(
                request_id=payload.request_id,
                status="success",
                message="Answer sent, waiting for agent response",
                data=response_data.model_dump(by_alias=True),
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        else:
            # No pending question - continue with regular prompt
            result = await continue_plan_execution(
                session=session,
                feedback=payload.feedback,
                choice=payload.choice,
                container_id=container_id,
            )

            # Update plan session
            new_status = result.get("status", PlanStatus.CONFIRMED)
            plan_manager.update_session(
                payload.plan_id,
                status=new_status,
                content=result.get("content", session.content),
                revision=session.revision + 1,
            )

            # Build response
            response_data = PlanResponseData(
                plan_id=payload.plan_id,
                status=new_status,
                content=result.get("content", session.content),
                questions=None,  # Questions come via SSE
                revision=session.revision + 1,
            )
            response = ServerResponse(
                request_id=payload.request_id,
                status="success",
                message="Plan continuation initiated",
                data=response_data.model_dump(by_alias=True),
                timestamp=datetime.now(timezone.utc).isoformat(),
            )

    except Exception as e:
        logger.error("plan_confirmation_failed", error=str(e), exc_info=True)
        response = ServerResponse(
            request_id=payload.request_id,
            status="error",
            message=f"Failed to continue plan: {e}",
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    await client_ws.send_json(response.model_dump(by_alias=True))


# Polling interval for checking UNSEEN patches when idle
UNSEEN_POLL_INTERVAL_SECONDS = 5.0


async def _send_unseen_patches(
    client_ws: WebSocket,
    state: AnalysisState,
) -> bool:
    """Check for and send any UNSEEN patches to the client.

    Args:
        client_ws: WebSocket connection to client
        state: Analysis state with has_unseen_hint flag

    Returns:
        True if patches were sent, False otherwise
    """
    user = get_current_user()

    # Get UNSEEN patches (this also marks them as SENT)
    patches = user.repo.get_patch_contents(mark_sent=True)

    if not patches:
        # Self-correcting: clear hint if no UNSEEN patches exist
        state.has_unseen_hint = False
        return False

    # Send patches to client
    analysis_data = AnalysisResponseData(
        type="analysis_lite",
        generation=state.generation,
        output=[],  # No annotations, just patches
        patches=patches,
    )
    response = ServerResponse(
        request_id=None,  # Broadcast - no specific request
        status="success",
        message=f"Pushing {len(patches)} unseen patches",
        data=analysis_data.model_dump(by_alias=True),
        generation=state.generation,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )
    await client_ws.send_json(response.model_dump(by_alias=True))

    logger.info(
        "unseen_patches_pushed",
        patch_count=len(patches),
        generation=state.generation,
    )

    return True


async def _handle_reconnect_state(
    client_ws: WebSocket,
    container: Container,
    user: "UserContext",
    plan_manager: PlanManager,
    state: AnalysisState,
) -> None:
    """Push all pending state to client on WebSocket connect/reconnect.

    This handles the case where user reconnects after a disconnect while:
    - OpenCode was waiting for a question answer
    - There are unseen patches to push
    - Any other pending state needs to be synced

    This is separate from plan_request which starts NEW plans.
    """
    container_id: str = container.id  # type: ignore[assignment]
    project_path = str(user.host_user_repo_dir)

    logger.info("reconnect_state_check_started", project_path=project_path)

    # 1. Check for pending questions from OpenCode
    try:
        manager = get_opencode_manager()
        # Only check if server is registered (don't start it just for reconnect check)
        if manager.has_server(container_id):
            opencode = await manager.get_client(container_id)
            pending_questions = await opencode.list_questions(project_path)

            if pending_questions:
                logger.info(
                    "reconnect_pending_questions_found",
                    count=len(pending_questions),
                )
                # Push each pending question to the client
                for q in pending_questions:
                    event = {"type": "question.asked", "properties": q}
                    await handle_opencode_question(event, client_ws, plan_manager)
    except Exception as e:
        logger.warning("reconnect_questions_check_failed", error=str(e))

    # 2. Push unseen patches immediately (instead of waiting for poll timeout)
    try:
        pushed = await _send_unseen_patches(client_ws, state)
        if pushed:
            logger.info("reconnect_unseen_patches_pushed")
    except Exception as e:
        logger.warning("reconnect_unseen_patches_failed", error=str(e))

    logger.debug("reconnect_state_check_completed")


async def ws_event_loop(client_ws: WebSocket, container: Container):
    """
    Event loop to handle WebSocket communication between client and container.
    Includes debounced analysis after file sync.

    Note: Requires UserContext to be set before calling (via set_current_user).
    """
    state = AnalysisState()
    user = get_current_user()

    # Plan manager for tracking active planning sessions
    plan_manager = PlanManager()

    # Start with hint=True to pick up any leftover UNSEEN patches from previous session
    state.has_unseen_hint = True
    # Track if we're processing a client request to prevent polling interference
    processing_request = False

    # === ON CONNECT: Push all pending state to client ===
    await _handle_reconnect_state(client_ws, container, user, plan_manager, state)

    try:
        while True:
            # Use timeout-based receive to allow periodic polling
            try:
                msg = await asyncio.wait_for(
                    client_ws.receive_json(),
                    timeout=UNSEEN_POLL_INTERVAL_SECONDS,
                )
            except asyncio.TimeoutError:
                # Timeout - check for UNSEEN patches if no analysis is running AND not processing a request
                if not processing_request and (
                    state.pending_task is None or state.pending_task.done()
                ):
                    if state.has_unseen_hint:
                        try:
                            await _send_unseen_patches(client_ws, state)
                        except Exception as e:
                            logger.warning("unseen_patches_send_failed", error=str(e))
                continue

            # Mark that we're processing a request to prevent polling interference
            processing_request = True
            changes = set()
            # Validate incoming payload
            try:
                request = ClientRequest.model_validate(msg).root
            except ValidationError as e:
                logger.warning("sync_payload_invalid", error=str(e))
                error_response = ServerResponse(
                    request_id=None,  # Cannot extract requestId from invalid payload
                    status="error",
                    message=f"Invalid payload: {e}",
                    timestamp=datetime.now(timezone.utc).isoformat(),
                )
                await client_ws.send_json(error_response.model_dump(by_alias=True))
                processing_request = False
                continue

            if request.type == "feedback":
                feedbackpayload = request.payload
                feedback = feedbackpayload.feedback
                logger.info("feedback_received", feedback_type=feedback.type)
                _append_feedback(feedbackpayload)
                processing_request = False
                continue

            elif request.type == "question":
                logger.info(
                    "question_received", question_id=request.payload.question_id
                )
                await handle_question(request.payload, client_ws, container)
                processing_request = False
                continue

            elif request.type == "analysis":
                logger.info(
                    "analysis_received",
                    request_id=request.payload.request_id,
                    file_name=request.payload.file_name,
                )
                await handle_analysis(request.payload, client_ws, state)
                processing_request = False
                continue

            elif request.type == "plan_request":
                logger.info(
                    "plan_request_received",
                    request_id=request.payload.request_id,
                )
                await handle_plan_request(
                    request.payload, client_ws, container, plan_manager
                )
                processing_request = False
                continue

            elif request.type == "plan_confirmation":
                logger.info(
                    "plan_confirmation_received",
                    request_id=request.payload.request_id,
                    plan_id=request.payload.plan_id,
                )
                await handle_plan_confirmation(
                    request.payload, client_ws, container, plan_manager
                )
                processing_request = False
                continue

            elif request.type == "sync":
                parsed = request.payload

            else:
                logger.warning("unknown_request_type", request_type=request.type)
                # Try to extract requestId from payload
                req_id = (
                    getattr(request.payload, "request_id", None)
                    if hasattr(request, "payload")
                    else None
                )
                error_response = ServerResponse(
                    request_id=req_id,
                    status="error",
                    message=f"Unknown request type: {request.type}",
                    timestamp=datetime.now(timezone.utc).isoformat(),
                )
                await client_ws.send_json(error_response.model_dump(by_alias=True))
                processing_request = False
                continue

            logger.debug("sync_received", file_count=len(parsed.changes))

            # Process each file change
            errors: list[SyncErrorDetail] = []

            for change in parsed.changes:
                # Run blocking Docker exec in thread pool
                success = await asyncio.to_thread(
                    _handle_file_changes, change, container
                )

                if not success:
                    errors.append(
                        SyncErrorDetail(path=change.path, error="Failed to apply")
                    )
                    logger.error("file_change_failed", path=change.path)
                else:
                    changes.add(change.path)

            # Determine status based on results
            if not errors:
                status = "success"
            elif changes:
                status = "partial"
            else:
                status = "error"
            if changes:
                await user.repo.commit_changes()
                logger.info(
                    "sync_committed",
                    files_changed=len(changes),
                    total_files=len(parsed.changes),
                    status=status,
                )

            # Send response back to client using unified response structure
            response_data = SyncResponseData(
                synced_files=list(changes),
                file_count=len(parsed.changes),
                errors=errors,
            )
            response = ServerResponse(
                request_id=parsed.request_id,
                status=status,
                message=f"Processed {len(changes)}/{len(parsed.changes)} files",
                data=response_data.model_dump(by_alias=True),
                generation=parsed.generation,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
            await client_ws.send_json(response.model_dump(by_alias=True))

            # Trigger debounced analysis after successful sync
            if changes:
                # Update state with session_id for feedback tracking
                state.session_id = parsed.session_id
                analysis_config = copy.copy(get_lite_analysis_config())

                # Check if initial analysis is running for this project
                session_mgr = SessionManager.get_instance()
                session_info = session_mgr.get(user.user_id, user.project_name or "")

                if session_info and session_info.is_initial_running():
                    # Initial analysis still running - accumulate changes
                    # These will be drained and queued when initial completes
                    change_summary = f"Files changed: {', '.join(changes)}"
                    await session_info.accumulate_change(change_summary)
                    logger.info(
                        "sync_changes_accumulated",
                        change_count=len(changes),
                        reason="initial_analysis_running",
                    )
                else:
                    # Initial analysis complete or not started - use debounced analysis
                    # This handles the full flow: run analysis → collect results → send to client
                    await trigger_analysis(container, client_ws, state, analysis_config)

            # Always clear processing flag after handling request
            processing_request = False

    finally:
        # Cleanup: cancel pending analysis on disconnect
        if state.pending_task and not state.pending_task.done():
            logger.debug("analysis_cleanup_on_disconnect", generation=state.generation)
            state.pending_task.cancel()
            try:
                await state.pending_task
            except asyncio.CancelledError:
                pass

        # Cleanup: stop SSE listener on disconnect
        try:
            session_mgr = SessionManager.get_instance()
            session_info = session_mgr.get(user.user_id, user.project_name or "")
            if session_info and session_info.listener:
                logger.debug("sse_listener_cleanup_on_disconnect")
                await session_info.listener.stop()
                session_mgr.update_listener(user.user_id, user.project_name or "", None)  # type: ignore[arg-type]
        except Exception as e:
            logger.warning("sse_listener_cleanup_failed", error=str(e))

        # Cleanup: remove session feedback files on disconnect
        # if state.session_id:
        #     try:
        #         feedback_path = user.host_feedback_dir
        #         jsonl_file = feedback_path / f"patch-review-{state.session_id}.jsonl"
        #         compiled_file = feedback_path / f"session-{state.session_id}-feedback.txt"

        #         if jsonl_file.exists():
        #             jsonl_file.unlink()
        #             logger.debug(
        #                 "feedback_jsonl_cleaned",
        #                 session_id=state.session_id,
        #                 file=jsonl_file.name,
        #             )

        #         if compiled_file.exists():
        #             compiled_file.unlink()
        #             logger.debug(
        #                 "feedback_compiled_cleaned",
        #                 session_id=state.session_id,
        #                 file=compiled_file.name,
        #             )
        #     except Exception as exc:
        #         logger.warning(
        #             "feedback_cleanup_failed",
        #             session_id=state.session_id,
        #             error=str(exc),
        #         )
