from datetime import datetime, timedelta, timezone

import httpx
from jose import jwt

from .config import settings

GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"
ALGORITHM = "HS256"


async def exchange_github_code(code: str) -> str:
    """Exchange OAuth code for GitHub access token."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            GITHUB_TOKEN_URL,
            data={
                "client_id": settings.github_client_id,
                "client_secret": settings.github_client_secret,
                "code": code,
            },
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise ValueError(data.get("error_description", data["error"]))
        return data["access_token"]


async def get_github_user(token: str) -> dict:
    """Get GitHub user info from access token."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            GITHUB_USER_URL,
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        return resp.json()


def create_jwt(payload: dict) -> str:
    """Create a signed JWT with expiry."""
    expire = datetime.now(timezone.utc) + timedelta(hours=settings.jwt_expiry_hours)
    to_encode = {**payload, "exp": expire}
    return jwt.encode(to_encode, settings.jwt_secret, algorithm=ALGORITHM)


def verify_jwt(token: str) -> dict:
    """Verify and decode JWT. Raises JWTError on failure."""
    return jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM])
