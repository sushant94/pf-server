# PF Server

Server + CLI for authenticating users via GitHub OAuth and proxying WebSocket connections to per-user Docker containers.

## Architecture

```
CLI                         Server                      GitHub
 │                            │                            │
 │  ┌──────────────────────────────────────────────────────────────┐
 │  │ PHASE 1: OAuth (one-time login)                              │
 │  └──────────────────────────────────────────────────────────────┘
 │── open browser ──────────────────────────────────────────►│
 │◄── redirect localhost:9876/callback?code=xxx ────────────│
 │                            │                            │
 │── POST /auth/token {code} ─►│                            │
 │                            │── exchange code ───────────►│
 │                            │◄── github_token ───────────│
 │                            │── GET /user ───────────────►│
 │                            │◄── {id, login} ────────────│
 │                            │                            │
 │                            │ ✓ check whitelist          │
 │                            │ ✓ ensure container         │
 │                            │ ✓ issue JWT                │
 │◄── {jwt} ──────────────────│                            │
 │                            │                            │
 │ save ~/.config/pf/token    │                            │
 │                            │                            │
 │  ┌──────────────────────────────────────────────────────────────┐
 │  │ PHASE 2: WebSocket (all subsequent communication)            │
 │  └──────────────────────────────────────────────────────────────┘
 │── WS /connect?token={jwt} ─►│                            │
 │                            │ ✓ verify JWT               │
 │                            │ ✓ get user's container     │
 │◄═══════════ WS bidirectional proxy ══════════════►│ Container
```

## Project Structure

```
pf-server/
├── src/pf_server/
│   ├── main.py        # FastAPI app + routes
│   ├── config.py      # Pydantic settings
│   ├── auth.py        # JWT + GitHub OAuth
│   ├── containers.py  # Docker management
│   └── ws_proxy.py    # WebSocket proxy
├── cli/
│   └── pf_cli.py      # login + connect commands
└── tests/
    └── test_core.py   # Unit tests
```

## Setup

### 1. Create GitHub OAuth App

Go to: https://github.com/settings/applications/new

| Field | Value |
|-------|-------|
| Application name | `PF Server` |
| Homepage URL | `http://localhost:8000` |
| Authorization callback URL | `http://localhost:9876/callback` |

### 2. Configure Environment

```bash
cp .env.example .env
```

Edit `.env`:
```
GITHUB_CLIENT_ID=<from GitHub>
GITHUB_CLIENT_SECRET=<from GitHub>
JWT_SECRET=<generate with: openssl rand -hex 32>
ALLOWED_GITHUB_IDS=<your GitHub user ID>
```

Find your GitHub user ID:
```bash
curl https://api.github.com/users/YOUR_USERNAME | grep '"id"'
```

### 3. Create Docker Network

```bash
docker network create pf-internal
```

### 4. Install Dependencies

```bash
uv sync
uv pip install -e ".[dev]"
```

## Running

### Start Server

```bash
uv run uvicorn pf_server.main:app --reload
```

### CLI Commands

```bash
# Login (opens browser for GitHub OAuth)
uv run python cli/pf_cli.py login

# Connect to your container via WebSocket
uv run python cli/pf_cli.py connect
```

## Testing

```bash
uv run pytest tests/ -v
```

## API Endpoints

| Endpoint | Type | Purpose |
|----------|------|---------|
| `GET /auth/login` | HTTP | Returns GitHub OAuth URL |
| `POST /auth/token` | HTTP | Exchange code for JWT |
| `WS /connect?token=` | WebSocket | Proxy to user's container |
