from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Query, WebSocket
from jose import JWTError
from pydantic import BaseModel

from .auth import create_jwt, exchange_github_code, get_github_user, verify_jwt
from .config import settings
from .containers import get_container_ws_url, get_or_create_container
from .ws_proxy import proxy_websocket

app = FastAPI(title="PF Server")

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
    # 1. Exchange code for GitHub token
    try:
        github_token = await exchange_github_code(request.code)
    except Exception as e:
        raise HTTPException(400, f"Failed to exchange code: {e}")

    # 2. Get user info from GitHub
    try:
        user = await get_github_user(github_token)
    except Exception as e:
        raise HTTPException(400, f"Failed to get user info: {e}")

    # 3. Check whitelist
    print(f"Got user info: {user}")
    if user["id"] not in settings.allowed_github_ids:
        raise HTTPException(403, "Not in beta whitelist")

    # 4. Ensure container exists for user
    get_or_create_container(user["id"])

    # 5. Issue our JWT (sub must be string per RFC 7519)
    token = create_jwt({"sub": str(user["id"]), "login": user["login"]})
    return TokenResponse(token=token)


@app.websocket("/connect")
async def websocket_connect(ws: WebSocket, token: str = Query(...)):
    """Authenticated WebSocket proxy to user's container."""
    # 1. Authenticate: verify JWT before accepting connection
    try:
        payload = verify_jwt(token)
    except JWTError:
        await ws.close(code=4001, reason="Unauthorized")
        return

    # 2. Accept connection only after successful auth
    await ws.accept()

    # 3. Get user's container WebSocket URL (sub is string, convert to int)
    container_url = get_container_ws_url(int(payload["sub"]))

    # 4. Proxy all messages bidirectionally
    await proxy_websocket(ws, container_url)
