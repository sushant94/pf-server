import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from urllib.parse import urlencode
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket
from fastapi.responses import JSONResponse
from jose import JWTError
from pydantic import BaseModel

from .auth import create_jwt, exchange_github_code, get_github_user, verify_jwt
from .config import settings
from .containers import ensure_opencode_ready, get_or_create_container
from .guess import build_dummy_annotation_results, run_initial_analysis
from .guess_configs import get_lite_analysis_config
from .logging_config import bind_request_context, configure_logging, get_logger
from .models import (
    ServerResponse,
    TarSyncPayload,
    TarSyncResponseData,
    SyncErrorDetail,
    AnalysisResponseData,
    SpecPatch,
)
from .session_manager import SessionManager
from .tar_utils import (
    extract_tar_in_docker,
    extract_tar_to_mounted_volume,
    validate_archive_security,
)
from .user_context import UserContext, set_current_user, set_project_name
from .ws_proxy import ws_event_loop

# Create MCP server and get ASGI app
from pf_mcp import create_authenticated_server

mcp_server = create_authenticated_server()
mcp_app = mcp_server.http_app(path="/mcp")

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan - must include MCP app's lifespan for proper session management."""
    configure_logging(json_output=settings.log_json, log_level=settings.log_level)
    logger.info(
        "server_starting", log_level=settings.log_level, log_json=settings.log_json
    )
    async with mcp_app.lifespan(mcp_app):
        yield
    logger.info("server_stopping")


app = FastAPI(title="PF Server", lifespan=lifespan)

# Mount MCP server at /mcp
app.mount("/mcp", mcp_app)


def authenticate(token: str) -> UserContext | None:
    """Validate JWT and return UserContext, or None if invalid."""
    try:
        payload = verify_jwt(token)
        # Note: project_name will be set later when we have the actual request payload
        user = UserContext(user_id=payload["sub"], login=payload.get("login"))
        set_current_user(user)
        bind_request_context(user_id=user.user_id, login=user.login)
        return user
    except JWTError as e:
        logger.debug("jwt_verification_failed", error=str(e))
        return None
    except KeyError as e:
        logger.warning("jwt_payload_missing_field", field=str(e))
        return None


async def require_auth(token: str = Query(...)) -> UserContext:
    """FastAPI dependency for HTTP routes."""
    user = authenticate(token)
    if user is None:
        raise HTTPException(401, "Unauthorized")
    return user


GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"


class TokenRequest(BaseModel):
    code: str


class TokenResponse(BaseModel):
    token: str


@app.get("/auth/login")
def auth_login(redirect_uri: str = "http://localhost:9876/callback"):
    """Return GitHub OAuth URL for client to open in browser."""
    params = urlencode(
        {
            "client_id": settings.github_client_id,
            "redirect_uri": redirect_uri,
            "scope": "read:user",
        }
    )
    return {"url": f"{GITHUB_AUTHORIZE_URL}?{params}"}


@app.post("/auth/token", response_model=TokenResponse)
async def auth_token(request: TokenRequest):
    """Exchange GitHub OAuth code for our JWT."""
    logger.info("oauth_exchange_started")

    # 1. Exchange code for GitHub token
    try:
        github_token = await exchange_github_code(request.code)
    except Exception as e:
        logger.warning("oauth_code_exchange_failed", error=str(e))
        raise HTTPException(400, f"Failed to exchange code: {e}")

    # 2. Get user info from GitHub
    try:
        user = await get_github_user(github_token)
    except Exception as e:
        logger.warning("github_user_fetch_failed", error=str(e))
        raise HTTPException(400, f"Failed to get user info: {e}")

    user_id = str(user["id"])
    login = user["login"]

    # 3. Check whitelist
    logger.info("user_authentication_attempt", user_id=user_id, login=login)
    if user["id"] not in settings.allowed_github_ids:
        logger.warning("user_not_whitelisted", user_id=user_id, login=login)
        raise HTTPException(403, "Not in beta whitelist")
    logger.info("user_authenticated", user_id=user_id, login=login)

    # 4. Issue our JWT (sub must be string per RFC 7519)
    token = create_jwt({"sub": user_id, "login": login})
    authenticate(token)

    # 5. Ensure container exists for user
    # get_or_create_container()

    logger.debug("oauth_exchange_completed", user_id=user_id, login=login)
    return TokenResponse(token=token)


@app.websocket("/connect")
async def websocket_connect(
    ws: WebSocket, token: str = Query(...), project_name: str = Query(...)
):
    """Authenticated WebSocket proxy to user's container."""
    # Authenticate before accepting (can't use Depends - need ws.close not HTTPException)
    user = authenticate(token)
    if user is None:
        await ws.close(code=4001, reason=" Unauthorized")
        return
    if not set_project_name(project_name):
        await ws.close(
            code=4002,
            reason="Project name is already set in this session. "
            "Currently a user can only work on one project at a time.",
        )
        return

    await ws.accept()
    logger.info("websocket_connected")
    start_time = time.perf_counter()

    container = get_or_create_container()
    # Ensure OpenCode server is running and registered with the manager.
    # This must happen before ws_event_loop, which may call plan generation
    # that requires the OpenCode client.
    await ensure_opencode_ready(container)
    try:
        await ws_event_loop(ws, container)
    finally:
        duration_ms = int((time.perf_counter() - start_time) * 1000)
        logger.info("websocket_disconnected", duration_ms=duration_ms)


@app.websocket("/connect/dummy")
async def websocket_connect_dummy(ws: WebSocket, token: str = Query(...)):
    """WebSocket dummy endpoint for client-side dev without a container."""
    user = authenticate(token)
    if user is None:
        await ws.close(code=4001, reason="Unauthorized")
        return

    await ws.accept()
    logger.info("dummy_websocket_connected")
    start_time = time.perf_counter()

    dummy_results = [res.model_dump() for res in build_dummy_annotation_results()]
    dummy_patches = ["THIS IS A DUMMY PATCH"]

    async def send_dummy(message: str = "Dummy analysis results", generation: int = 0):
        analysis_data = AnalysisResponseData(
            type="analysis_dummy",
            generation=generation,
            output=dummy_results,
            patches=[
                SpecPatch(id=f"dummy-patch-{i + 1}", patch=dummy_patches[i])
                for i in range(len(dummy_patches))
            ],
        )
        response = ServerResponse(
            request_id=None,  # Dummy responses are broadcasts
            status="success",
            message=message,
            data=analysis_data.model_dump(by_alias=True),
            generation=generation,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        await ws.send_json(response.model_dump(by_alias=True))

    # Send an initial dummy response on connect
    await send_dummy()

    # Echo dummy responses for any incoming client messages to keep protocol flowing
    try:
        async for _ in ws.iter_json():
            await send_dummy("Dummy ack")
    except Exception as e:
        duration_ms = int((time.perf_counter() - start_time) * 1000)
        logger.debug(
            "dummy_websocket_disconnected", duration_ms=duration_ms, reason=str(e)
        )


def find_unique_project_name(requested_name: str, user_dir: Path) -> str:
    """Find a unique project name by checking if repository already exists.

    If a repository with the requested name already exists (has a repo directory),
    generates a unique name by appending a number (e.g., "project-name-1", "project-name-2", etc.).

    Args:
        requested_name: The original project name requested by the client.
        user_dir: The user's base directory (settings.host_users_data_directory / user_id).

    Returns:
        The unique project name to use.
    """
    project_dir = user_dir / requested_name
    repo_dir = project_dir / "repo"

    # Check if repository already exists (has a repo directory with content)
    repo_exists = repo_dir.exists() and repo_dir.is_dir() and any(repo_dir.iterdir())

    # If no existing repository, we can use the requested name
    if not repo_exists:
        return requested_name

    # Otherwise, find the first available name with a numeric suffix
    counter = 1
    while True:
        candidate_name = f"{requested_name}-{counter}"
        candidate_dir = user_dir / candidate_name
        candidate_repo_dir = candidate_dir / "repo"
        candidate_repo_exists = (
            candidate_repo_dir.exists()
            and candidate_repo_dir.is_dir()
            and any(candidate_repo_dir.iterdir())
        )

        if not candidate_repo_exists:
            logger.info(
                "project_name_conflict_resolved",
                requested_name=requested_name,
                actual_name=candidate_name,
            )
            return candidate_name
        counter += 1


@app.post("/sync/tar")
async def sync_tar(
    payload: TarSyncPayload,
    user: UserContext = Depends(require_auth),
):
    """
    Receive tar.gz archive and extract to user's project-specific workspace.

    Supports two extraction modes:
    - docker: Extract via docker cp + exec (works with any container)
    - mounted: Extract directly to mounted volume (faster, requires bind mounts)

    If a repository with the requested project name already exists, a unique
    name will be generated (e.g., "project-name-1") and returned in the response.
    """
    # Validate archive
    is_valid, error_msg = validate_archive_security(
        payload.archive_base64, settings.max_tar_size_mb
    )
    if not is_valid:
        response_data = TarSyncResponseData(
            synced_files=[],
            file_count=0,
            errors=[SyncErrorDetail(path="", error=error_msg)],
            actual_project_name=None,
        )
        return JSONResponse(
            status_code=400,  # Bad request - validation failed
            content=ServerResponse(
                request_id=payload.request_id,
                status="error",
                message=error_msg,
                data=response_data.model_dump(by_alias=True),
                generation=payload.generation,
                timestamp=datetime.now(timezone.utc).isoformat(),
            ).model_dump(by_alias=True),
        )

    # Check for name conflicts and find a unique project name
    actual_project_name = find_unique_project_name(
        payload.project_name, user.host_mount_dir
    )

    # Set the actual project name in user context for path resolution
    set_project_name(actual_project_name)
    container = get_or_create_container()

    # Track if we had to use a different name
    name_changed = actual_project_name != payload.project_name

    try:
        # Choose extraction method based on configuration
        if settings.tar_extraction_mode == "mounted":
            # Extract directly to mounted volume
            mount_path = user.host_user_repo_dir
            success, files, error = extract_tar_to_mounted_volume(
                payload.archive_base64, mount_path, target_subdir=""
            )
        else:
            # Extract via docker API (default)
            success, files, error = extract_tar_in_docker(
                payload.archive_base64,
                container,
                target_path=str(user.docker_workdir_dir),
            )

        if not success:
            response_data = TarSyncResponseData(
                synced_files=[],
                file_count=0,
                errors=[SyncErrorDetail(path="", error=error)],
                actual_project_name=None,
            )
            return JSONResponse(
                status_code=500,
                content=ServerResponse(
                    request_id=payload.request_id,
                    status="error",
                    message=f"Extraction failed: {error}",
                    data=response_data.model_dump(by_alias=True),
                    generation=payload.generation,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                ).model_dump(by_alias=True),
            )

        await user.repo.do_init()

        # Start initial OpenCode analysis in background (fire-and-forget)
        config = get_lite_analysis_config()
        # Only start initial analysis for OpenCode configs
        from .guess_configs import OpenCodeAnalysisConfig

        if isinstance(config, OpenCodeAnalysisConfig):
            session_mgr = SessionManager.get_instance()
            session_info = session_mgr.get_or_create(
                user.user_id,
                actual_project_name,
                str(user.host_user_repo_dir),
            )

            # Only start if not already running
            if not session_info.is_initial_running():
                task = asyncio.create_task(
                    run_initial_analysis(
                        container=container,
                        project_path=str(user.docker_shadow_dir),
                        config=config,
                        session_info=session_info,
                        user_id=user.user_id,
                        project_name=actual_project_name,
                    )
                )
                session_info.initial_analysis_task = task
                logger.info(
                    "initial_analysis_triggered",
                    user_id=user.user_id,
                    project_name=actual_project_name,
                )

        # Include actual project name in response if it differs from requested
        response_data = TarSyncResponseData(
            synced_files=files,
            file_count=len(files),
            errors=[],
            actual_project_name=actual_project_name if name_changed else None,
        )

        message = f"Extracted {len(files)} files ({payload.file_count} expected)"
        if name_changed:
            message += f" (project name changed from '{payload.project_name}' to '{actual_project_name}')"

        return ServerResponse(
            request_id=payload.request_id,
            status="success",
            message=message,
            data=response_data.model_dump(by_alias=True),
            generation=payload.generation,
            timestamp=datetime.now(timezone.utc).isoformat(),
        ).model_dump(by_alias=True)

    except Exception as e:
        logger.error("tar_sync_failed", error=str(e))
        response_data = TarSyncResponseData(
            synced_files=[],
            file_count=0,
            errors=[SyncErrorDetail(path="", error=str(e))],
            actual_project_name=None,
        )
        return JSONResponse(
            status_code=500,
            content=ServerResponse(
                request_id=payload.request_id,
                status="error",
                message=str(e),
                data=response_data.model_dump(by_alias=True),
                generation=payload.generation,
                timestamp=datetime.now(timezone.utc).isoformat(),
            ).model_dump(by_alias=True),
        )
